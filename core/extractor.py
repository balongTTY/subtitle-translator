"""字幕提取 — faster-whisper 语音识别（视频/音频 → 字幕条目/SRT）

基于开源 faster-whisper（MIT，CTranslate2 后端）：
- 日语/英语识别质量最好的开源方案（whisper large-v3 / large-v3-turbo）
- 日语可用 kotoba-whisper-v2.0-faster 专用优化模型
- 音频解码走 PyAV，无需系统安装 ffmpeg

线程模型与 translator.py 相同：QThread + Worker + 模块级线程注册表，
facade 被 GC 后运行中的线程仍由注册表兜底持有，避免 QThread 强杀崩溃。
"""

import logging
import os
import re
import sys
import threading
from pathlib import Path

# Windows 下 ctranslate2 与 Qt5 运行库有加载顺序冲突（Qt5Core 先加载会让
# whisper 模型构造段错误），必须在 PyQt5 之前预导入。main.py 已有同款守卫，
# 这里兜底覆盖「绕过 main.py 直接 import 本模块」的用法。未安装时跳过。
try:
    import ctranslate2  # noqa: F401
except Exception:
    pass

from PyQt5.QtCore import QObject, QThread, pyqtSignal

from core.subtitle_io import SubtitleFile
from core.translation_batch import SubtitleEntry

log = logging.getLogger("subtitle_translator")

# 模块级运行中线程注册表（作用见 translator.py 同名结构）
_live_threads: dict[int, tuple[QThread, "ExtractionWorker"]] = {}


def _cleanup_finished_thread(thread: QThread, worker: "ExtractionWorker") -> None:
    if _live_threads.pop(id(thread), None) is None:
        return
    try:
        worker.deleteLater()
    except RuntimeError:
        pass
    try:
        thread.deleteLater()
    except RuntimeError:
        pass


def check_whisper_available() -> str | None:
    """faster-whisper 是否已安装。返回 None 表示可用，否则返回 ImportError 描述。

    用 find_spec 探测而非直接 import：import faster_whisper 会连带加载
    huggingface_hub，其模块级常量在 import 时固化 HF_ENDPOINT——必须在它
    之前由 SubtitleExtractor.__init__ 设置镜像环境变量，否则镜像配置失效。
    """
    import importlib.util
    if "faster_whisper" in sys.modules:
        return None  # 已加载（真实安装或测试注入的假模块）
    try:
        if importlib.util.find_spec("faster_whisper") is not None:
            return None
    except (ImportError, ValueError):
        pass  # 注入的假模块无 __spec__ 时走兜底 import 验证
    try:
        import faster_whisper  # noqa: F401
        return None
    except ImportError as e:
        return str(e)


class ExtractionCancelled(Exception):
    """用户取消提取"""


class ExtractionSignals(QObject):
    """提取信号发射器"""

    extraction_started = pyqtSignal(str)                  # filepath
    model_loading = pyqtSignal(str)                       # 模型描述（首次加载/下载提示）
    extraction_progress = pyqtSignal(str, int, int, str)  # filepath, 当前ms, 总ms, 最新识别文本
    extraction_completed = pyqtSignal(str, str, object)   # filepath, srt_path, meta{language,...}
    extraction_failed = pyqtSignal(str, str)              # filepath, error_message
    all_completed = pyqtSignal(dict)                      # {filepath: srt_path}（失败为空串）


# Whisper 输出的日文/英文文本规整：全角空格、换行、首尾空白
_WS_RE = re.compile(r"[\u3000]+")


def normalize_segment_text(text: str) -> str:
    """规整 whisper 分段文本：去首尾空白、换行/全角空格折叠为半角空格。

    换行必须折叠——翻译提示词按「N. 译文」编号行解析，条目内嵌换行
    会被解析器误当成续行（prompt_builder 的未编号续行逻辑）。
    """
    text = _WS_RE.sub(" ", text or "")
    text = text.replace("\r", " ").replace("\n", " ")
    return text.strip()


def segments_to_entries(segments) -> list[SubtitleEntry]:
    """whisper segments（start/end 秒）→ SubtitleEntry 列表（毫秒）。

    - 空白/纯标点分段跳过
    - end <= start 时补 200ms 最短显示时长
    - index 重新连续编号（跳过分段后不能留空洞，下游按 index 寻址）
    """
    entries: list[SubtitleEntry] = []
    for seg in segments:
        text = normalize_segment_text(getattr(seg, "text", ""))
        if not text:
            continue
        start_ms = int(round(seg.start * 1000))
        end_ms = int(round(seg.end * 1000))
        if end_ms <= start_ms:
            end_ms = start_ms + 200
        entries.append(
            SubtitleEntry(
                index=len(entries),
                start=start_ms,
                end=end_ms,
                text=text,
                original_text=text,
            )
        )
    return entries


def resolve_output_path(filepath: str) -> str:
    """提取输出路径：视频同目录 {stem}.srt。已存在时备份为 .bak（与翻译输出策略一致）。"""
    p = Path(filepath)
    out = p.parent / f"{p.stem}.srt"
    if out.exists():
        backup = out.with_suffix(out.suffix + ".bak")
        try:
            backup.unlink()
        except OSError:
            pass
        out.replace(backup)
        log.info("已备份原有字幕文件: %s -> %s", out, backup)
    return str(out)


class SubtitleExtractor:
    """faster-whisper 封装。模型按 (模型, 设备, 精度) 缓存，跨实例复用（加载代价高）。

    线程安全：模型加载用全局锁；推理由调用方（单 worker）串行执行。
    """

    _lock = threading.Lock()
    _model = None            # 缓存的 WhisperModel 实例
    _model_key: tuple | None = None

    def __init__(
        self,
        model_size: str,
        device: str = "auto",
        compute_type: str = "auto",
        cpu_threads: int = 0,
        hf_mirror: str = "",
    ) -> None:
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.cpu_threads = cpu_threads
        # HF_ENDPOINT 必须在 huggingface_hub 首次使用前生效（faster_whisper 惰性导入前设置）
        if hf_mirror:
            from core.model_download import apply_hf_mirror
            apply_hf_mirror(hf_mirror)
        self.hf_mirror = hf_mirror
        self.on_model_loading = None       # 可选回调 (描述: str)
        self.on_download_progress = None   # 可选回调 (已下载字节, 总字节)

    @classmethod
    def from_settings(cls) -> "SubtitleExtractor":
        from config.settings import AppSettings
        s = AppSettings()
        return cls(
            model_size=s.whisper_model,
            device=s.whisper_device,
            compute_type=s.whisper_compute,
            cpu_threads=s.whisper_threads,
            hf_mirror=s.hf_mirror,
        )

    def _get_model(self):
        """惰性加载/复用缓存的 whisper 模型。

        未缓存时先走带真实进度的下载（hub tqdm 钩子转发分块进度到
        on_download_progress），再构造——旧实现模型构造内部静默下载，
        GUI 完全不知道在下载、也不知道要下多久。
        """
        key = (self.model_size, self.device, self.compute_type)
        with SubtitleExtractor._lock:
            if SubtitleExtractor._model is not None and SubtitleExtractor._model_key == key:
                return SubtitleExtractor._model
            if self.on_model_loading:
                self.on_model_loading(self.model_size)

            from core.model_download import download_model_with_progress, is_model_cached
            if is_model_cached(self.model_size) is None:
                try:
                    download_model_with_progress(
                        self.model_size,
                        hf_mirror=self.hf_mirror,
                        on_progress=self.on_download_progress,
                    )
                except Exception as e:
                    # 预下载失败不阻断：WhisperModel 构造时自带下载兜底
                    log.warning("带进度预下载失败，转由模型构造自行下载: %s", e)

            # 惰性导入：测试通过向 sys.modules 注入假的 faster_whisper 模块替换
            from faster_whisper import WhisperModel
            kwargs = {}
            if self.cpu_threads and self.cpu_threads > 0:
                kwargs["cpu_threads"] = self.cpu_threads
            try:
                model = WhisperModel(
                    self.model_size,
                    device=self.device,
                    compute_type=self.compute_type,
                    **kwargs,
                )
            except RuntimeError as e:
                # device=auto 时 ctranslate2 检测到 NVIDIA 驱动就选 CUDA，但驱动在而
                # CUDA 运行库（cublas/cudnn 等）缺失时直接抛错不会回退——降级 CPU 重试
                msg = str(e).lower()
                if self.device in ("auto", "cuda") and any(
                    k in msg for k in ("cuda", "cublas", "cudnn", "nvidia")
                ):
                    log.warning("GPU 推理不可用（%s），回退 CPU: %s", e, self.model_size)
                    model = WhisperModel(
                        self.model_size,
                        device="cpu",
                        compute_type=self.compute_type,
                        **kwargs,
                    )
                else:
                    raise
            SubtitleExtractor._model = model
            SubtitleExtractor._model_key = key
            return model

    def extract(
        self,
        filepath: str,
        language: str | None,
        cancel_event: threading.Event | None = None,
        vad_filter: bool = True,
        on_progress=None,
    ) -> tuple[list[SubtitleEntry], dict]:
        """识别一个媒体文件。返回 (entries, info)。

        language: None/"" = 自动检测；"ja"/"en" = 指定语言
        on_progress: (当前ms, 总ms, 最新文本) 增量回调
        cancel_event: 置位后在下一个分段边界抛 ExtractionCancelled
        """
        try:
            return self._extract_once(filepath, language, cancel_event, vad_filter, on_progress)
        except RuntimeError as e:
            # CUDA 是惰性加载：模型构造可能成功，到首次推理才报缺 cublas/cudnn。
            # 此时重建 CPU 模型整体重试一次（首个分段前失败，无重复进度回调）。
            msg = str(e).lower()
            if any(k in msg for k in ("cuda", "cublas", "cudnn", "nvidia")) and self.device != "cpu":
                log.warning("GPU 推理不可用（%s），回退 CPU 重试", e)
                with SubtitleExtractor._lock:
                    SubtitleExtractor._model = None
                    SubtitleExtractor._model_key = None
                self.device = "cpu"
                return self._extract_once(filepath, language, cancel_event, vad_filter, on_progress)
            raise

    def _extract_once(
        self,
        filepath: str,
        language: str | None,
        cancel_event: threading.Event | None,
        vad_filter: bool,
        on_progress,
    ) -> tuple[list[SubtitleEntry], dict]:
        model = self._get_model()
        segments, info = model.transcribe(
            filepath,
            language=language or None,
            task="transcribe",
            beam_size=5,
            vad_filter=vad_filter,
            condition_on_previous_text=True,
        )
        duration_ms = int(getattr(info, "duration", 0) * 1000)

        collected = []
        for seg in segments:  # 生成器：边识别边产出
            if cancel_event is not None and cancel_event.is_set():
                raise ExtractionCancelled(filepath)
            text = normalize_segment_text(getattr(seg, "text", ""))
            if not text:
                continue
            collected.append(seg)
            if on_progress:
                on_progress(int(seg.end * 1000), duration_ms, text)

        entries = segments_to_entries(collected)
        if not entries:
            raise RuntimeError("未识别到任何语音内容（文件可能是纯音乐/静音，或音轨损坏）")

        meta = {
            "language": getattr(info, "language", "") or "",
            "language_probability": float(getattr(info, "language_probability", 0.0)),
            "duration": float(getattr(info, "duration", 0.0)),
        }
        return entries, meta


class ExtractionWorker(QObject):
    """提取工作线程：逐文件识别并落盘为 SRT"""

    def __init__(self, filepaths: list[str]) -> None:
        super().__init__()
        self.filepaths = filepaths
        self.signals = ExtractionSignals()
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def run(self) -> None:
        try:
            settings_lang = ""
            vad = True
            engine = "local"
            extractor = None
            try:
                from config.settings import AppSettings
                s = AppSettings()
                settings_lang = s.extract_lang
                vad = s.whisper_vad
                engine = s.asr_engine
                if engine == "online":
                    # 在线识别（OpenAI 兼容 / MiMo chat_audio，双协议）
                    from core.asr_online import OnlineASR
                    extractor = OnlineASR.from_settings()
                else:
                    extractor = SubtitleExtractor.from_settings()
            except Exception as e:
                log.exception("读取提取设置失败: %s", e)
            if extractor is None:
                extractor = SubtitleExtractor(model_size="large-v3-turbo")

            # 模型加载提示仅本地引擎有意义（在线无本地模型加载）
            if hasattr(extractor, "on_model_loading"):
                extractor.on_model_loading = lambda desc: self.signals.model_loading.emit(desc)

            language = settings_lang if settings_lang in ("ja", "en") else None
            results: dict[str, str] = {}

            for filepath in self.filepaths:
                if self._cancel_event.is_set():
                    break
                self.signals.extraction_started.emit(filepath)
                # 模型下载进度 → 复用提取进度信号（百分比制，状态栏/文件行可见）
                if hasattr(extractor, "on_download_progress"):
                    def dl_progress(done: int, total: int, _fp=filepath) -> None:
                        pct = int(done * 100 / total) if total else 0
                        self.signals.extraction_progress.emit(
                            _fp, pct, 100, f"下载识别模型中 {pct}%"
                        )
                    extractor.on_download_progress = dl_progress
                try:
                    def progress(cur_ms: int, total_ms: int, text: str, _fp=filepath) -> None:
                        self.signals.extraction_progress.emit(_fp, cur_ms, total_ms, text)

                    entries, meta = extractor.extract(
                        filepath,
                        language=language,
                        cancel_event=self._cancel_event,
                        vad_filter=vad,
                        on_progress=progress,
                    )
                    out_path = resolve_output_path(filepath)
                    SubtitleFile().save(out_path, entries)
                    results[filepath] = out_path
                    self.signals.extraction_completed.emit(
                        filepath, out_path, meta or {},
                    )
                except ExtractionCancelled:
                    results[filepath] = ""
                    break
                except Exception as e:
                    log.exception("字幕提取失败: %s", filepath)
                    results[filepath] = ""
                    self.signals.extraction_failed.emit(filepath, str(e))

            self.signals.all_completed.emit(results)
        finally:
            # run() 作为 started 的槽执行，返回后线程进入 exec() 永久阻塞，必须 quit
            QThread.currentThread().quit()


class ExtractionFacade(QObject):
    """提取编排器 — 主线程侧接口（单 worker 串行识别，whisper 推理本身吃满资源）"""

    def __init__(self) -> None:
        super().__init__()
        self._signals = ExtractionSignals()
        self._thread: QThread | None = None
        self._worker: ExtractionWorker | None = None
        self._cancelled = False

    @property
    def signals(self) -> ExtractionSignals:
        return self._signals

    def prepare_extraction(self, filepaths: list[str]) -> None:
        """创建 worker 与线程（信号此刻即可连接，start 之前）"""
        self._worker = ExtractionWorker(list(filepaths))
        self._thread = QThread()
        self._worker.moveToThread(self._thread)

        # worker 信号 → 聚合信号转发
        self._worker.signals.extraction_started.connect(self._signals.extraction_started)
        self._worker.signals.model_loading.connect(self._signals.model_loading)
        self._worker.signals.extraction_progress.connect(self._signals.extraction_progress)
        self._worker.signals.extraction_completed.connect(self._signals.extraction_completed)
        self._worker.signals.extraction_failed.connect(self._signals.extraction_failed)
        self._worker.signals.all_completed.connect(self._signals.all_completed)

        self._thread.started.connect(self._worker.run)
        self._thread.finished.connect(
            lambda t=self._thread, w=self._worker: _cleanup_finished_thread(t, w)
        )
        # 注册表持强引用：facade 被 GC/替换时运行中的线程不会裸奔
        _live_threads[id(self._thread)] = (self._thread, self._worker)

    def start(self) -> None:
        if self._thread is not None:
            self._thread.start()

    def start_extraction(self, filepaths: list[str]) -> None:
        self.prepare_extraction(filepaths)
        self.start()

    def cancel(self) -> None:
        self._cancelled = True
        if self._worker is not None:
            self._worker.cancel()
        self._force_cleanup()

    def _force_cleanup(self) -> None:
        """请求线程退出并等待自然结束（引用由注册表持有，绝不提前释放）"""
        if self._thread is None:
            return
        thread, worker = self._thread, self._worker
        if worker is not None:
            worker.cancel()
        thread.requestInterruption()
        thread.quit()
        # 识别中的单次 transcribe 调用不可中断，等待宽限期
        thread.wait(3000)
        self._thread = None
        self._worker = None
