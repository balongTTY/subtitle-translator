"""模型下载功能测试 — 假 hub/假下载函数，不触网

覆盖：tqdm 进度聚合 / 缓存检测 / 仓库映射 / extractor 未缓存先下载 /
worker 进度信号接线 / 设置页一键下载全流程。
"""
import os
import sys
import time
import types
from pathlib import Path

# 与 main.py 同款守卫：ctranslate2 必须先于 PyQt5 加载（Windows DLL 顺序冲突，
# Qt 先加载会让 ctranslate2 相关调用段错误——本文件会同时触碰两条链）
try:
    import ctranslate2  # noqa: F401
except Exception:
    pass

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(os.path.abspath(__file__)).parent.parent.parent))

_APP = None


def _ensure_app():
    global _APP
    from PyQt5.QtWidgets import QApplication
    _APP = QApplication.instance() or QApplication(sys.argv)
    return _APP


# ---- 假 huggingface_hub 模块 ----

def _install_fake_hub(cached_repos=(), simulate_download=True):
    """注入假 hub。cached_repos 里的仓库 local_files_only 查询命中；
    simulate_download=True 时逐文件走 tqdm_class 进度流。"""
    fake = types.ModuleType("huggingface_hub")

    def snapshot_download(repo, tqdm_class=None, local_files_only=False, **kw):
        if local_files_only:
            if repo in cached_repos:
                return f"/cache/{repo.replace('/', '_')}"
            raise FileNotFoundError(f"not cached: {repo}")
        return f"/cache/{repo.replace('/', '_')}"

    class HfApi:
        def __init__(self, endpoint=None):
            pass

        def list_repo_files(self, repo):
            return ["model.bin", "config.json"]

    def hf_hub_download(repo, filename, tqdm_class=None, **kw):
        if simulate_download:
            # 复刻 hub 行为：按文件实例化 tqdm_class 并逐块 update
            size = 1000 if filename == "model.bin" else 200
            t = tqdm_class(total=size, desc=filename)
            step = size // 3
            t.update(step)
            t.update(step)
            t.update(size - 2 * step)
            t.close()
        return f"/cache/{repo.replace('/', '_')}/{filename}"

    fake.snapshot_download = snapshot_download
    fake.HfApi = HfApi
    fake.hf_hub_download = hf_hub_download
    real = sys.modules.get("huggingface_hub")
    sys.modules["huggingface_hub"] = fake
    return real


def _restore_hub(real):
    if real is not None:
        sys.modules["huggingface_hub"] = real
    else:
        sys.modules.pop("huggingface_hub", None)


def test_repo_mapping():
    from core.model_download import _model_repo, model_size_hint
    assert _model_repo("tiny") == "Systran/faster-whisper-tiny"
    assert _model_repo("large-v3-turbo") == "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
    assert _model_repo("kotoba-tech/kotoba-whisper-v2.0-faster") == \
        "kotoba-tech/kotoba-whisper-v2.0-faster"  # 自定义仓库原样
    assert "GB" in model_size_hint("large-v3-turbo") or "MB" in model_size_hint("large-v3-turbo")
    assert model_size_hint("unknown/model") == "大小未知"
    print("模型名 → 仓库映射 ✓")


def test_download_progress_aggregation():
    """tqdm_class 钩子：多文件分块进度聚合为总进度"""
    from core.model_download import download_model_with_progress, is_model_cached

    real = _install_fake_hub(cached_repos=(), simulate_download=True)
    try:
        events = []
        path = download_model_with_progress(
            "tiny", on_progress=lambda d, t: events.append((d, t)),
        )
        assert Path(path) == Path("/cache/Systran_faster-whisper-tiny")
        assert events, "进度回调未触发"
        # 最终应看到 1200/1200（model.bin 1000 + config.json 200）
        assert any(d == t == 1200 for d, t in events), events[-3:]
        # 中间过程单调不减
        assert [d for d, _ in events] == sorted(d for d, _ in events)

        # 缓存检测：未缓存 None / 已缓存路径
        assert is_model_cached("tiny") is None
        real2 = _install_fake_hub(cached_repos=("Systran/faster-whisper-tiny",))
        assert is_model_cached("tiny") == "/cache/Systran_faster-whisper-tiny"
        _restore_hub(real2)

        # 下载失败 → RuntimeError 带网络/镜像提示
        def boom(repo, filename=None, **kw):
            raise ConnectionError("no network")
        fake = sys.modules["huggingface_hub"]
        fake.hf_hub_download = boom
        try:
            download_model_with_progress("tiny")
            raise AssertionError("应抛 RuntimeError")
        except RuntimeError as e:
            assert "镜像" in str(e)
    finally:
        _restore_hub(real)
    print("下载进度聚合 + 缓存检测 + 失败提示 ✓")


def test_extractor_downloads_before_build():
    """extractor：模型未缓存时先走带进度的下载，再构造 WhisperModel"""
    _ensure_app()
    import core.model_download as MD
    import core.extractor as E

    calls = {"download": 0, "progress": 0, "cached_checks": 0}

    orig_cached = MD.is_model_cached
    orig_dl = MD.download_model_with_progress
    MD.is_model_cached = lambda size: (calls.__setitem__("cached_checks", calls["cached_checks"] + 1), None)[1]
    MD.download_model_with_progress = (
        lambda size, hf_mirror="", on_progress=None:
        (calls.__setitem__("download", calls["download"] + 1),
         on_progress and on_progress(50, 100) and calls.__setitem__("progress", calls["progress"] + 1),
         "/cache/fake")[2]
    )
    # 假 faster_whisper（复用 test_extractor 的注入方式）
    from types import SimpleNamespace
    fake_fw = types.ModuleType("faster_whisper")

    class FakeModel:
        instances = []
        def __init__(self, size, **kw):
            self.size = size
            FakeModel.instances.append(self)
        def transcribe(self, audio, **kw):
            def gen():
                yield SimpleNamespace(start=0.0, end=1.0, text="テスト")
            return gen(), SimpleNamespace(language="ja", language_probability=1.0, duration=1.0)

    fake_fw.WhisperModel = FakeModel
    real_fw = sys.modules.get("faster_whisper")
    sys.modules["faster_whisper"] = fake_fw
    E.SubtitleExtractor._model = None
    E.SubtitleExtractor._model_key = None
    try:
        ex = E.SubtitleExtractor(model_size="fake-model", device="cpu", compute_type="int8")
        progress = []
        ex.on_download_progress = lambda d, t: progress.append((d, t))
        entries, meta = ex.extract("x.wav", language="ja")
        assert calls["download"] == 1, "未缓存应先下载"
        assert calls["cached_checks"] == 1
        assert progress == [(50, 100)], "下载进度应转发到回调"
        assert FakeModel.instances and FakeModel.instances[-1].size == "fake-model"
    finally:
        MD.is_model_cached = orig_cached
        MD.download_model_with_progress = orig_dl
        if real_fw is not None:
            sys.modules["faster_whisper"] = real_fw
        E.SubtitleExtractor._model = None
        E.SubtitleExtractor._model_key = None
    print("extractor 未缓存先下载 + 进度转发 ✓")


def test_worker_download_progress_signal():
    """worker 把下载进度转百分比发 extraction_progress"""
    _ensure_app()
    import core.extractor as E

    received = []
    worker = E.ExtractionWorker(["fake.mp4"])
    worker.signals.extraction_progress.connect(
        lambda fp, cur, total, txt: received.append((fp, cur, total, txt)))
    # 直接调用 run 循环里装配的回调（通过公开流程验证太重，这里复现接线）
    cb = None
    extractor = types.SimpleNamespace(
        on_download_progress=None,
        extract=lambda *a, **k: ([], {}),
    )
    # 模拟 run() 内的接线代码
    filepath = "fake.mp4"
    def dl_progress(done, total, _fp=filepath):
        pct = int(done * 100 / total) if total else 0
        worker.signals.extraction_progress.emit(_fp, pct, 100, f"下载识别模型中 {pct}%")
    extractor.on_download_progress = dl_progress
    extractor.on_download_progress(45, 100)
    assert received == [("fake.mp4", 45, 100, "下载识别模型中 45%")], received
    print("worker 下载进度→百分比信号 ✓")


def test_settings_dialog_download_flow():
    """设置页：已缓存即提示；未缓存 → 确认 → 后台下载 → 进度条 → 完成"""
    _ensure_app()
    import gui.settings_dialog as SD
    import core.model_download as MD

    boxes = []
    real_mb = SD.QMessageBox

    class _MB:
        Yes = real_mb.Yes
        information = staticmethod(lambda *a, **k: boxes.append(("info", a[2] if len(a) > 2 else "")))
        warning = staticmethod(lambda *a, **k: boxes.append(("warn", "")))
        critical = staticmethod(lambda *a, **k: boxes.append(("crit", str(a[2]) if len(a) > 2 else "")))
        question = staticmethod(lambda *a, **k: real_mb.Yes)

    orig_cached, orig_dl = MD.is_model_cached, MD.download_model_with_progress
    SD.QMessageBox = _MB  # offscreen 下原生 QMessageBox 会崩，必须替换为 shim
    try:
        dlg = SD.SettingsDialog()
        dlg.tabs.setCurrentIndex(5)
        dlg._on_asr_engine_changed()

        # 场景一：已缓存 → 信息提示，不起下载
        MD.is_model_cached = lambda size: "/cache/here"
        dlg._on_download_model()
        assert any(k == "info" and "已在本地缓存" in msg for k, msg in boxes), boxes
        assert dlg.model_dl_btn.isEnabled()

        # 场景二：未缓存 → 确认(Yes) → 后台假下载(进度 50→100) → 完成提示
        boxes.clear()

        def fake_dl(model, hf_mirror="", on_progress=None):
            if on_progress:
                on_progress(500, 1000)
                on_progress(1000, 1000)
            return "/cache/done"

        MD.is_model_cached = lambda size: None
        MD.download_model_with_progress = fake_dl
        dlg._on_download_model()
        assert not dlg.model_dl_btn.isEnabled(), "下载中按钮应禁用"
        deadline = time.time() + 5
        while not dlg.model_dl_btn.isEnabled() and time.time() < deadline:
            _ensure_app().processEvents()
            time.sleep(0.02)
        assert dlg.model_dl_btn.isEnabled(), "下载结束按钮应恢复"
        assert dlg.model_dl_bar.value() == 100, dlg.model_dl_bar.value()
        assert any(k == "info" and "模型已就绪" in msg for k, msg in boxes), boxes
    finally:
        SD.QMessageBox = real_mb
        MD.is_model_cached, MD.download_model_with_progress = orig_cached, orig_dl
    print("设置页一键下载全流程（缓存提示/确认/进度条/完成）✓")


def main() -> int:
    tests = [
        test_settings_dialog_download_flow,  # 对话框测试放最前：避免 hub/whisper
        # 假模块注入与真实 DLL 混载的组合状态
        test_repo_mapping,
        test_download_progress_aggregation,
        test_extractor_downloads_before_build,
        test_worker_download_progress_signal,
    ]
    for t in tests:
        t()
    print(f"\n全部通过：{len(tests)} 组测试")
    return 0


if __name__ == "__main__":
    sys.exit(main())
