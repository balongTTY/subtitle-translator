"""whisper 模型下载 — 真实进度回调 + 本地缓存检测

首次提取时 faster-whisper 构造模型会静默下载（GUI 无感知，exe 无控制台
连 tqdm 都看不到）。这里用 huggingface_hub 的 tqdm_class 钩子拿到每个文件
的分块进度并转发给回调；下载仍由 hub 完成（走同一缓存，支持断点续传，
下载后 WhisperModel 直接命中本地缓存）。
"""

import logging
import os
from functools import partial
from pathlib import Path

from tqdm import tqdm as _RealTqdm

log = logging.getLogger("subtitle_translator")

# 常用模型的下载体积提示（展示用；自定义仓库不在此表）
MODEL_SIZE_HINTS = {
    "tiny": "约 75MB",
    "base": "约 140MB",
    "small": "约 460MB",
    "medium": "约 1.5GB",
    "large-v3": "约 3GB",
    "large-v3-turbo": "约 1.6GB",
}


def model_size_hint(model_size: str) -> str:
    return MODEL_SIZE_HINTS.get(model_size, "大小未知")


def apply_hf_mirror(hf_mirror: str) -> None:
    """应用 HF 镜像设置。必须在 huggingface_hub 首次 import 前调用
    （ENDPOINT 常量 import 时固化）。

    同时禁用 Xet 传输：hub ≥1.x 默认走 Xet CAS 协议（cas-server.xethub.hf.co），
    镜像站不代理该协议会 401 Unauthorized——禁用后回退普通 HTTP resolve
    下载，镜像可正常代理。
    """
    if hf_mirror:
        os.environ["HF_ENDPOINT"] = hf_mirror
        os.environ["HF_HUB_DISABLE_XET"] = "1"


def _ensure_mirror(hf_mirror: str) -> None:
    apply_hf_mirror(hf_mirror)


def _model_repo(model_size: str) -> str:
    """faster-whisper 的模型名 → HF 仓库名（自定义仓库/本地路径原样返回）"""
    try:
        from faster_whisper.utils import _MODELS
    except ImportError:
        # 非常规安装（或测试注入的模块）：按自定义仓库名处理
        return model_size
    return _MODELS.get(model_size, model_size)


def is_model_cached(model_size: str) -> str | None:
    """模型是否已在本地缓存。已缓存 → 返回快照路径；未缓存 → None。"""
    from huggingface_hub import snapshot_download
    try:
        return snapshot_download(_model_repo(model_size), local_files_only=True)
    except Exception:
        return None


class _SilentProgressTqdm(_RealTqdm):
    """hub 的 tqdm_class：禁用自身输出，把分块进度转发给聚合器。

    hub 以 tqdm_class(total=字节数, desc=文件名, ...) 实例化后逐块 update(n)、
    结束 close()。注意 tqdm 在 disable=True 时 __init__ 提前返回、不初始化
    desc/n 等属性——因此关键参数在 super().__init__ 之前自行捕获，进度也
    自行计数，不依赖 tqdm 内部状态。
    """

    def __init__(self, *args, _emit=None, _totals=None, **kwargs):
        # desc/total 可以来自位置参数（tqdm(iterable, desc, total,...)）
        self._key = kwargs.get("desc") or (args[1] if len(args) > 1 else "") or ""
        self._total = kwargs.get("total") or (args[2] if len(args) > 2 else 0) or 0
        self._n_local = 0
        self._emit = _emit
        self._totals = _totals
        kwargs["disable"] = True  # 静默：GUI 场景没有控制台可显示
        super().__init__(*args, **kwargs)
        if _totals is not None and self._key:
            # 同名文件（断点续传重开）保留原进度
            _totals.setdefault(self._key, [0, self._total])

    def update(self, n=1):
        self._n_local += n
        try:
            super().update(n)
        except Exception:
            pass
        if self._totals is not None and self._key:
            slot = self._totals.setdefault(self._key, [0, self._total])
            slot[0] = self._n_local
        self._notify()

    def close(self):
        try:
            super().close()
        except Exception:
            pass
        self._notify()

    def _notify(self):
        if self._emit:
            try:
                self._emit()
            except Exception:
                pass


def download_model_with_progress(
    model_size: str,
    hf_mirror: str = "",
    on_progress=None,
) -> str:
    """下载模型到 hub 缓存（与 faster-whisper 同一缓存，可断点续传）。

    on_progress(done_bytes, total_bytes) 在每个分块后触发。
    返回本地快照目录。

    实现说明：不用 snapshot_download——hub 1.x 的新实现对 Xet 存储仓库
    走 CAS 协议且进度条 total=0（拿不到字节数）；单文件 hf_hub_download
    遵守 HF_HUB_DISABLE_XET，回退普通 HTTP，进度条按真实字节逐块更新。
    逐文件下载写入同一缓存，之后 faster-whisper 的 snapshot_download 直接
    命中本地。Xet 统一禁用（官方/镜像端点一致处理，普通 HTTP 均可断点续传）。
    """
    apply_hf_mirror(hf_mirror)
    os.environ["HF_HUB_DISABLE_XET"] = "1"  # 无论是否镜像，都要字节级进度
    from huggingface_hub import HfApi, hf_hub_download

    repo = _model_repo(model_size)
    totals: dict[str, list[int]] = {}

    def emit() -> None:
        if on_progress and totals:
            done = sum(v[0] for v in totals.values())
            total = sum(v[1] for v in totals.values())
            if total > 0:
                on_progress(min(done, total), total)

    tqdm_factory = partial(_SilentProgressTqdm, _emit=emit, _totals=totals)
    api_kwargs = {"endpoint": hf_mirror} if hf_mirror else {}
    dl_kwargs = {"tqdm_class": tqdm_factory}
    if hf_mirror:
        dl_kwargs["endpoint"] = hf_mirror

    log.info("开始下载 whisper 模型 %s (%s)", model_size, repo)
    try:
        api = HfApi(**api_kwargs)
        files = api.list_repo_files(repo)
        if not files:
            raise RuntimeError("仓库为空或无法列出文件")
        last_path = None
        for filename in files:
            last_path = hf_hub_download(repo, filename, **dl_kwargs)
    except Exception as e:
        raise RuntimeError(
            f"模型下载失败（{model_size}）: {e}\n"
            f"建议检查网络；国内网络请在 设置 → 字幕提取 → 模型下载源 选择 hf-mirror.com 镜像后重试"
        ) from e

    snapshot_dir = str(Path(last_path).parent) if last_path else ""
    log.info("模型下载完成: %s", snapshot_dir)
    if on_progress and totals:
        total = sum(v[1] for v in totals.values())
        on_progress(total, total)
    return snapshot_dir
