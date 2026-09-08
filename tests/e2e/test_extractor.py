"""字幕提取测试 — fake whisper 模型，不触网、不下载模型

覆盖：
- 文本规整 / segments→entries 转换（换行折叠、空段跳过、零时长修正、重编号）
- 输出路径与 .bak 备份策略
- SubtitleExtractor 提取 + 进度回调 + 语言自动检测 + 取消
- ExtractionFacade 端到端（QThread + 信号）：识别 → 落盘 SRT → pysubs2 回读校验
- 多文件提取中途取消（第一个完成后取消，第二个不产出）
"""
import os
import sys
import tempfile
import time
import types
from pathlib import Path
from types import SimpleNamespace

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(os.path.abspath(__file__)).parent.parent.parent))

from core.extractor import (
    SubtitleExtractor,
    ExtractionFacade,
    ExtractionCancelled,
    check_whisper_available,
    normalize_segment_text,
    segments_to_entries,
    resolve_output_path,
)

# ---- 假 faster_whisper 模块（注入 sys.modules，替换真实依赖）----

import threading

FILE2_GATE = threading.Event()  # 取消测试：第二个文件首个分段前的时序闸门

FAKE_SEGMENTS = [
    SimpleNamespace(start=0.0, end=2.5, text=" こんにちは みんな "),
    SimpleNamespace(start=2.5, end=4.0, text="配信はじめます\n今日はゲーム"),
    SimpleNamespace(start=4.0, end=4.0, text=""),          # 空文本 → 跳过
    SimpleNamespace(start=4.5, end=4.4, text="短い"),       # end<start → 补 200ms
    SimpleNamespace(start=5.0, end=6.0, text="  \u3000 "),  # 全空白 → 跳过
]
EXPECTED_TEXTS = ["こんにちは みんな", "配信はじめます 今日はゲーム", "短い"]


class FakeWhisperModel:
    instances = []

    def __init__(self, model_size_or_path, device="auto", compute_type="auto", **kw):
        self.model_size = model_size_or_path
        self.init_device = device
        self.init_compute = compute_type
        self.cpu_threads_kw = kw
        FakeWhisperModel.instances.append(self)
        self.transcribe_calls = []

    def transcribe(self, audio, language=None, task=None, beam_size=5,
                   vad_filter=True, condition_on_previous_text=True):
        self.transcribe_calls.append((audio, language, task, vad_filter))
        duration = FAKE_SEGMENTS[-1].end
        info = SimpleNamespace(
            language=language or "ja", language_probability=0.99, duration=duration,
        )

        # 取消测试的时序闸门：第二个文件在产出首个分段前阻塞，
        # 等主线程处理完「文件1完成」并调用 cancel 后再放行（消除竞态）
        gate = FILE2_GATE if str(audio).endswith("b.mp4") else None

        def gen():
            if gate is not None:
                gate.wait(timeout=10)
            for s in FAKE_SEGMENTS:
                yield s

        return gen(), info


def install_fake_whisper() -> None:
    fake_mod = types.ModuleType("faster_whisper")
    fake_mod.WhisperModel = FakeWhisperModel
    sys.modules["faster_whisper"] = fake_mod
    FakeWhisperModel.instances.clear()
    FILE2_GATE.clear()
    # 假模型环境视为「已缓存」：本套件测提取逻辑，预下载走真实网络会挂
    import core.model_download as MD
    MD.is_model_cached = lambda size: "/cache/fake"
    MD.download_model_with_progress = (
        lambda size, hf_mirror="", on_progress=None: "/cache/fake"
    )
    # 重置模型缓存，保证每个测试重新走 _get_model
    SubtitleExtractor._model = None
    SubtitleExtractor._model_key = None


_orig_whisper_mod = sys.modules.get("faster_whisper")

# QApplication 必须持模块级引用：函数内创建的局部 QApplication 在函数返回时被
# GC，连带销毁 QSettings 等全部 QObject（后续测试再访问设置即 RuntimeError）
_APP = None


def _ensure_app():
    global _APP
    from PyQt5.QtWidgets import QApplication
    _APP = QApplication.instance() or QApplication(sys.argv)
    return _APP


def restore_whisper() -> None:
    if _orig_whisper_mod is not None:
        sys.modules["faster_whisper"] = _orig_whisper_mod
    else:
        sys.modules.pop("faster_whisper", None)
    SubtitleExtractor._model = None
    SubtitleExtractor._model_key = None


def test_text_normalization():
    assert normalize_segment_text("  hello world  ") == "hello world"
    assert normalize_segment_text("一行\n二行") == "一行 二行"
    assert normalize_segment_text("全角\u3000空格") == "全角 空格"
    assert normalize_segment_text("") == ""
    assert normalize_segment_text(None) == ""
    print("文本规整（空白/换行/全角空格折叠）✓")


def test_segments_to_entries():
    entries = segments_to_entries(FAKE_SEGMENTS)
    assert len(entries) == 3, f"应跳过空段，实际 {len(entries)}"
    assert [e.text for e in entries] == EXPECTED_TEXTS
    assert [e.index for e in entries] == [0, 1, 2], "跳过后 index 必须重编号"
    assert entries[0].start == 0 and entries[0].end == 2500, "秒→毫秒转换错误"
    assert entries[2].end == entries[2].start + 200, "end<=start 应补 200ms 最短时长"
    assert all(e.original_text == e.text for e in entries)
    print("segments→entries（ms 转换/空段跳过/零时长修正/重编号）✓")


def test_resolve_output_path():
    with tempfile.TemporaryDirectory() as td:
        video = Path(td) / "录播.mp4"
        video.write_bytes(b"fake")
        out = resolve_output_path(str(video))
        assert out == str(Path(td) / "录播.srt")
        assert not Path(td, "录播.srt").exists(), "不应提前创建文件"

        # 已有同名 srt → 备份为 .bak
        old = Path(td) / "录播.srt"
        old.write_text("旧字幕", encoding="utf-8")
        out2 = resolve_output_path(str(video))
        assert out2 == str(old)
        assert Path(td, "录播.srt.bak").exists(), "旧文件应备份为 .bak"
        assert Path(td, "录播.srt.bak").read_text(encoding="utf-8") == "旧字幕"
    print("输出路径解析（无冲突/已有文件备份 .bak）✓")


def test_extractor_core():
    install_fake_whisper()
    try:
        ex = SubtitleExtractor(model_size="fake-model", device="cpu", compute_type="int8")
        loading = []
        ex.on_model_loading = loading.append
        progress = []
        entries, meta = ex.extract(
            "whatever.mp4", language=None, vad_filter=True,
            on_progress=lambda cur, total, text: progress.append((cur, total, text)),
        )
        assert loading == ["fake-model"], "模型加载回调未触发"
        assert [e.text for e in entries] == EXPECTED_TEXTS
        assert meta["language"] == "ja" and meta["language_probability"] == 0.99
        assert len(progress) == 3, f"非空分段应各触发一次进度，实际 {len(progress)}"
        assert progress[0] == (2500, 6000, "こんにちは みんな")

        # 语言指定透传
        model = FakeWhisperModel.instances[0]
        assert model.transcribe_calls[0][1] is None, "language=None 应透传自动检测"
        ex.extract("x.mp3", language="en")
        assert model.transcribe_calls[1][1] == "en", "指定语言应透传给模型"

        # 模型缓存复用（同 key 不重建）
        ex2 = SubtitleExtractor(model_size="fake-model", device="cpu", compute_type="int8")
        before = len(FakeWhisperModel.instances)
        ex2._get_model()
        assert len(FakeWhisperModel.instances) == before, "同配置应复用缓存模型"

        # 取消：cancel_event 置位后在分段边界抛 ExtractionCancelled
        import threading
        ev = threading.Event()

        def cancelled_gen():
            yield FAKE_SEGMENTS[0]
            ev.set()
            yield FAKE_SEGMENTS[1]

        model.transcribe = lambda *a, **k: (
            cancelled_gen(),
            SimpleNamespace(language="ja", language_probability=1.0, duration=6.0),
        )
        try:
            ex.extract("y.mp4", language="ja", cancel_event=ev)
            raise AssertionError("取消后应抛 ExtractionCancelled")
        except ExtractionCancelled:
            pass
    finally:
        restore_whisper()
    print("SubtitleExtractor（提取/进度/语言透传/模型缓存/取消）✓")


def test_facade_end_to_end():
    app = _ensure_app()

    install_fake_whisper()
    from config.settings import AppSettings
    s = AppSettings()
    saved = (s.whisper_model, s.extract_lang, s.whisper_vad, s.hf_mirror)
    s.whisper_model, s.extract_lang, s.whisper_vad, s.hf_mirror = "fake-model", "auto", True, ""

    with tempfile.TemporaryDirectory() as td:
        video = Path(td) / "直播录像.mp4"
        video.write_bytes(b"fake media")
        received = {"started": [], "loading": [], "progress": 0,
                    "completed": [], "failed": [], "all": []}

        facade = ExtractionFacade()
        sig = facade.signals
        sig.extraction_started.connect(lambda fp: received["started"].append(fp))
        sig.model_loading.connect(lambda d: received["loading"].append(d))
        sig.extraction_progress.connect(lambda *a: received.__setitem__("progress", received["progress"] + 1))
        sig.extraction_completed.connect(
            lambda fp, out, lang: received["completed"].append((fp, out, lang)))
        sig.extraction_failed.connect(lambda fp, e: received["failed"].append((fp, e)))
        sig.all_completed.connect(lambda r: received["all"].append(r))

        facade.start_extraction([str(video)])

        deadline = time.time() + 15
        while not received["all"] and time.time() < deadline:
            app.processEvents()
            time.sleep(0.02)
        assert received["all"], "等待 all_completed 超时"

        srt = Path(td) / "直播录像.srt"
        assert srt.exists(), "SRT 未落盘"
        assert len(received["completed"]) == 1
        done_fp, done_out, done_meta = received["completed"][0]
        assert (done_fp, done_out) == (str(video), str(srt))
        assert done_meta["language"] == "ja"
        assert received["loading"] == ["fake-model"]
        assert received["progress"] == 3
        assert not received["failed"]

        # pysubs2 回读校验：条目数、文本、时间轴
        import pysubs2
        sub = pysubs2.SSAFile.load(str(srt))
        assert len(sub.events) == 3
        texts = [e.plaintext for e in sub.events]
        assert texts == EXPECTED_TEXTS, f"回读文本不符: {texts}"
        assert sub.events[0].start == 0 and sub.events[0].end == 2500

    s.whisper_model, s.extract_lang, s.whisper_vad, s.hf_mirror = saved
    restore_whisper()
    print("ExtractionFacade 端到端（识别→SRT 落盘→pysubs2 回读）✓")


def test_facade_cancel_between_files():
    app = _ensure_app()

    install_fake_whisper()
    from config.settings import AppSettings
    s = AppSettings()
    saved = (s.whisper_model, s.extract_lang, s.whisper_vad, s.hf_mirror)
    s.whisper_model, s.extract_lang, s.whisper_vad, s.hf_mirror = "fake-model", "auto", True, ""

    with tempfile.TemporaryDirectory() as td:
        v1, v2 = Path(td) / "a.mp4", Path(td) / "b.mp4"
        v1.write_bytes(b"x")
        v2.write_bytes(b"x")
        received = {"completed": 0, "all": []}

        facade = ExtractionFacade()
        sig = facade.signals
        sig.extraction_completed.connect(lambda *a: received.__setitem__("completed", received["completed"] + 1))
        # 第一个文件完成后立即取消
        sig.extraction_completed.connect(lambda *a: facade.cancel())
        sig.all_completed.connect(lambda r: received["all"].append(r))

        facade.start_extraction([str(v1), str(v2)])

        # 等第一个文件完成（b.mp4 在闸门处阻塞，保证取消先于第二个文件开始）
        deadline = time.time() + 15
        while received["completed"] < 1 and time.time() < deadline:
            app.processEvents()
            time.sleep(0.02)
        assert received["completed"] == 1, "第一个文件应在闸门前完成"
        FILE2_GATE.set()  # 放行第二个文件 → worker 在分段边界发现取消 → 中止

        deadline = time.time() + 15
        while not received["all"] and time.time() < deadline:
            app.processEvents()
            time.sleep(0.02)
        assert received["all"], "取消场景等待 all_completed 超时"
        assert received["completed"] == 1, "只应完成第一个文件"
        assert (Path(td) / "a.srt").exists(), "第一个文件应已产出"
        assert not (Path(td) / "b.srt").exists(), "取消后第二个文件不应产出"

    s.whisper_model, s.extract_lang, s.whisper_vad, s.hf_mirror = saved
    restore_whisper()
    print("多文件提取中途取消（仅第一个产出，第二个不产出）✓")


def test_cuda_inference_fallback():
    """device=auto 时 CUDA 惰性失败（推理期才报缺 cublas）应回退 CPU 重试"""
    install_fake_whisper()

    class CudaFailModel(FakeWhisperModel):
        def transcribe(self, audio, language=None, task=None, beam_size=5,
                       vad_filter=True, condition_on_previous_text=True):
            if self.init_device != "cpu":
                def gen():
                    raise RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")
                    yield  # 不可达，使 gen 成为生成器
                return gen(), SimpleNamespace(
                    language="ja", language_probability=1.0, duration=6.0,
                )
            return FakeWhisperModel.transcribe(
                self, audio, language=language, task=task, beam_size=beam_size,
                vad_filter=vad_filter, condition_on_previous_text=condition_on_previous_text,
            )

    sys.modules["faster_whisper"].WhisperModel = CudaFailModel
    try:
        ex = SubtitleExtractor(model_size="fake-model", device="auto", compute_type="auto")
        entries, meta = ex.extract("video.mp4", language="ja")
        assert [e.text for e in entries] == EXPECTED_TEXTS, "回退 CPU 后应正常识别"
        assert len(FakeWhisperModel.instances) == 2, "应构建两次模型（auto 失败 + cpu 重试）"
        assert FakeWhisperModel.instances[0].init_device == "auto"
        assert FakeWhisperModel.instances[1].init_device == "cpu", "第二次应以 cpu 重建"
        assert ex.device == "cpu", "回退后实例设备应更新为 cpu"
    finally:
        restore_whisper()
    print("CUDA 推理期失败自动回退 CPU（重建模型重试）✓")


def test_media_classification():
    from utils.constants import MEDIA_FORMATS, SUPPORTED_FORMATS
    assert ".mp4" in MEDIA_FORMATS and ".mkv" in MEDIA_FORMATS
    assert ".mp3" in MEDIA_FORMATS and ".wav" in MEDIA_FORMATS
    assert not (set(MEDIA_FORMATS) & set(SUPPORTED_FORMATS)), "媒体与字幕扩展名不应重叠"
    r = check_whisper_available()
    assert r is None or isinstance(r, str), "可用性检查应返回 None 或错误描述"
    print("媒体格式常量与依赖可用性检查 ✓")


def main() -> int:
    tests = [
        test_text_normalization,
        test_segments_to_entries,
        test_resolve_output_path,
        test_extractor_core,
        test_cuda_inference_fallback,
        test_facade_end_to_end,
        test_facade_cancel_between_files,
        test_media_classification,
    ]
    for t in tests:
        t()
    print(f"\n全部通过：{len(tests)} 组测试")
    return 0


if __name__ == "__main__":
    sys.exit(main())
