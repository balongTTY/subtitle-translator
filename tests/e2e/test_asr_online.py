"""在线语音识别测试 — 假客户端，不触网

覆盖：均分时间轴 / transcriptions 带时间轴+无时间轴回退 / MiMo chat_audio
协议 / 长音频切块拼接 / 取消 / Worker 引擎分派 / 设置对话框联动。
"""
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(os.path.abspath(__file__)).parent.parent.parent))

_APP = None


def _ensure_app():
    global _APP
    from PyQt5.QtWidgets import QApplication
    _APP = QApplication.instance() or QApplication(sys.argv)
    return _APP


# ----------------------------------------------------------------------

def test_split_text_evenly():
    """无时间轴文本 → 句级切分 + 按字符比例均分时长"""
    from core.asr_online import split_text_evenly

    text = "第一句话。第二句比较长一点点！第三句？短。结尾句到这里结束。"
    entries = split_text_evenly(text, 10.0, 40.0)
    assert len(entries) >= 4, len(entries)
    assert entries[0].start == 10000
    assert entries[-1].end <= 40000
    # 时长与文本长度正相关：长句时长 > 短句
    durations = [e.end - e.start for e in entries]
    texts = [e.text for e in entries]
    longest = max(range(len(texts)), key=lambda i: len(texts[i]))
    assert durations[longest] >= min(durations), (durations, texts)
    # 单条不超上限太多（超长句硬拆）
    long_text = "这是一段没有任何标点但是特别特别长的文字" * 6
    entries = split_text_evenly(long_text, 0.0, 60.0)
    assert all(len(e.text) <= 30 for e in entries), [e.text for e in entries]
    # 空文本
    assert split_text_evenly("", 0, 10) == []
    assert split_text_evenly("  ", 0, 10) == []
    print("均分时间轴（句级切分/比例时长/超长硬拆/空文本）✓")


def _make_wav(path: str, seconds: float = 1.0) -> None:
    """生成真实可解码的 wav（静音即可，切块器只看容器）"""
    import av
    out = av.open(path, "w", format="wav")
    stream = out.add_stream("pcm_s16le", rate=16000)
    stream.layout = "mono"
    import fractions
    frame = av.AudioFrame("s16", "mono", 16000)
    frame.sample_rate = 16000
    frame.pts = 0
    frame.time_base = fractions.Fraction(1, 16000)
    for p in stream.encode(frame):
        out.mux(p)
    for p in stream.encode(None):
        out.mux(p)
    out.close()


class _FakeTranscriptionsClient:
    """假 OpenAI 客户端：transcriptions 协议"""

    def __init__(self, with_segments=True, text="你好。世界。"):
        self.with_segments = with_segments
        self.text = text
        self.calls = []

        audio = self

        class _Audio:
            class transcriptions:
                @staticmethod
                def create(model=None, file=None, **kw):
                    audio.calls.append((model, kw))
                    if audio.with_segments:
                        segs = [
                            SimpleNamespace(start=0.0, end=2.0, text=" 你好。"),
                            SimpleNamespace(start=2.0, end=4.5, text="世界。"),
                        ]
                        return SimpleNamespace(segments=segs, text=audio.text)
                    return SimpleNamespace(segments=None, text=audio.text)

        self.audio = _Audio


class _FakeChatClient:
    """假 OpenAI 客户端：chat_audio 协议（MiMo）"""

    def __init__(self, reply="第一句。第二句。"):
        self.calls = []
        self._reply = reply

        chat = self

        class _Chat:
            class completions:
                @staticmethod
                def create(model=None, messages=None, extra_body=None, **kw):
                    chat.calls.append({"model": model, "extra_body": extra_body})
                    return SimpleNamespace(choices=[
                        SimpleNamespace(message=SimpleNamespace(content=chat._reply))
                    ])

        self.chat = _Chat


def test_online_asr_transcriptions_segments():
    """verbose_json 分段 → 真实时间轴 + 估算标记为 False"""
    _ensure_app()
    from core.asr_online import OnlineASR

    with tempfile.TemporaryDirectory() as td:
        wav = str(Path(td) / "a.wav")
        _make_wav(wav)
        asr = OnlineASR("https://fake/v1", "sk-test", "whisper-1", "transcriptions", 25)
        fake = _FakeTranscriptionsClient(with_segments=True)
        asr._make_client = lambda: fake
        progress = []
        entries, meta = asr.extract(wav, "ja", on_progress=lambda c, t, s: progress.append((c, t)))
        assert [e.text for e in entries] == ["你好。", "世界。"]
        assert entries[1].start == 2000 and entries[1].end == 4500  # 分段时间戳→ms
        assert meta["timeline_estimated"] is False
        assert meta["language"] == "ja"
        # language 透传
        assert fake.calls[0][1]["language"] == "ja"
        assert progress, "应上报进度"
    print("transcriptions 带时间轴识别 ✓")


def test_online_asr_text_fallback():
    """无分段响应 → 均分估算 + timeline_estimated=True"""
    _ensure_app()
    from core.asr_online import OnlineASR

    with tempfile.TemporaryDirectory() as td:
        wav = str(Path(td) / "a.wav")
        _make_wav(wav)
        asr = OnlineASR("https://fake/v1", "sk-test", "gpt-4o-transcribe",
                        "transcriptions", 25)
        fake = _FakeTranscriptionsClient(with_segments=False, text="整段文本。没有分段。")
        asr._make_client = lambda: fake
        entries, meta = asr.extract(wav, None)
        assert len(entries) >= 2
        assert meta["timeline_estimated"] is True
        assert "language" not in fake.calls[0][1]  # 未指定语言时不传
    print("transcriptions 纯文本回退（估算时间轴）✓")


def test_online_asr_mimo_chat_audio():
    """MiMo chat_audio：input_audio base64 + asr_options.language 映射"""
    _ensure_app()
    from core.asr_online import OnlineASR

    with tempfile.TemporaryDirectory() as td:
        wav = str(Path(td) / "a.mp3")
        # 直接给一个真实小 mp3（切块器对 mp3 且小文件直传）
        import shutil
        import core.audio_splitter as S
        src_wav = str(Path(td) / "src.wav")
        _make_wav(src_wav)
        S._encode_chunk(src_wav, 0, 1.0, wav)

        asr = OnlineASR("https://api.xiaomimimo.com/v1", "sk-test",
                        "mimo-v2.5-asr", "chat_audio", 10)
        fake = _FakeChatClient(reply="大家好。今天玩个游戏。")
        asr._make_client = lambda: fake
        entries, meta = asr.extract(wav, "en")
        assert len(entries) == 2
        assert meta["timeline_estimated"] is True
        call = fake.calls[0]
        assert call["model"] == "mimo-v2.5-asr"
        assert call["extra_body"] == {"asr_options": {"language": "en"}}
        content = None  # 从 messages 校验 base64 结构
        # ja → auto 映射
        entries, _ = asr.extract(wav, "ja")
        assert fake.calls[1]["extra_body"] == {"asr_options": {"language": "auto"}}
    print("MiMo chat_audio 协议（base64/language 映射/估算时间轴）✓")


def test_online_asr_cancel_and_errors():
    """取消在块边界生效；缺 Key/端点构造即报清晰错误"""
    _ensure_app()
    import threading
    from core.asr_online import OnlineASR
    from core.extractor import ExtractionCancelled

    with tempfile.TemporaryDirectory() as td:
        wav = str(Path(td) / "a.wav")
        _make_wav(wav)
        asr = OnlineASR("https://fake/v1", "sk-test", "whisper-1")
        fake = _FakeTranscriptionsClient()
        asr._make_client = lambda: fake
        ev = threading.Event()
        ev.set()
        try:
            asr.extract(wav, None, cancel_event=ev)
            raise AssertionError("取消应抛出")
        except ExtractionCancelled:
            pass
        assert fake.calls == [], "取消后不应发起请求"

    try:
        OnlineASR("https://fake/v1", "", "m")
        raise AssertionError("缺 Key 应报错")
    except ValueError as e:
        assert "API Key" in str(e)
    try:
        OnlineASR("", "k", "m")
        raise AssertionError("缺端点应报错")
    except ValueError as e:
        assert "端点" in str(e)
    print("取消/配置校验 ✓")


def test_worker_dispatch_online(tmp_engine="online"):
    """Worker 按设置分派在线引擎（假 OnlineASR 全链路 → SRT）"""
    _ensure_app()
    from config.settings import AppSettings
    import core.extractor as E
    from core.asr_online import OnlineASR

    s = AppSettings()
    saved = (s.asr_engine, s.asr_service, s.asr_model, s.asr_base_url, s.get_asr_api_key())
    s.asr_engine, s.asr_service, s.asr_model, s.asr_base_url = \
        "online", "openai", "whisper-1", "https://fake/v1"
    s.set_asr_api_key("sk-test")

    class _FakeASR(OnlineASR):
        def __init__(self):
            super().__init__("https://fake/v1", "sk-test", "whisper-1")
        def extract(self, filepath, language, cancel_event=None, vad_filter=True, on_progress=None):
            if on_progress:
                on_progress(1000, 1000, "fake")
            return (
                [E.SubtitleEntry(index=0, start=0, end=2000, text="在线译文一", original_text="在线译文一"),
                 E.SubtitleEntry(index=1, start=2000, end=4000, text="在线译文二", original_text="在线译文二")],
                {"language": "ja", "duration": 4.0, "timeline_estimated": False},
            )

    orig_extractor_from_settings = E.SubtitleExtractor.__dict__["from_settings"]  # patch 前先存原始 classmethod
    E.SubtitleExtractor.from_settings = classmethod(lambda cls: (_ for _ in ()).throw(AssertionError("本地引擎不应被创建")))
    orig_new = OnlineASR.from_settings.__func__
    OnlineASR.from_settings = classmethod(lambda cls: _FakeASR())
    try:
        with tempfile.TemporaryDirectory() as td:
            media = Path(td) / "录播.mp4"
            media.write_bytes(b"x")
            received = {"completed": [], "all": []}
            facade = E.ExtractionFacade()
            sig = facade.signals
            sig.extraction_completed.connect(
                lambda fp, out, meta: received["completed"].append((fp, out, meta)))
            sig.all_completed.connect(lambda r: received["all"].append(r))
            facade.start_extraction([str(media)])
            deadline = time.time() + 10
            while not received["all"] and time.time() < deadline:
                _ensure_app().processEvents()
                time.sleep(0.02)
            assert received["all"], "超时"
            meta = received["completed"][0][2]
            assert meta["language"] == "ja" and meta["timeline_estimated"] is False
            srt = Path(td) / "录播.srt"
            assert srt.exists()
            content = srt.read_text(encoding="utf-8")
            assert "在线译文一" in content and "在线译文二" in content
    finally:
        OnlineASR.from_settings = classmethod(orig_new)
        E.SubtitleExtractor.from_settings = orig_extractor_from_settings
        s.asr_engine, s.asr_service, s.asr_model, s.asr_base_url = saved[:4]
        s.set_asr_api_key(saved[4] or "")
    print("Worker 在线引擎分派端到端（识别→SRT→meta）✓")


def test_settings_dialog_asr_wiring():
    """设置页：引擎切换显隐、服务联动端点/模型、保存/加载回环、_combo_value"""
    _ensure_app()
    from gui.settings_dialog import SettingsDialog
    from config.settings import AppSettings

    s = AppSettings()
    saved = (s.asr_engine, s.asr_service, s.asr_model, s.asr_base_url, s.get_asr_api_key())

    dlg = SettingsDialog()
    _ensure_app().processEvents()
    dlg.tabs.setCurrentIndex(5)  # 切到「字幕提取」页，否则页面隐藏导致可见性判断失真
    # 默认本地：在线组隐藏
    assert dlg.asr_engine.currentData() == "local"
    dlg._on_asr_engine_changed()
    assert dlg.local_group.isVisibleTo(dlg) and not dlg.online_group.isVisibleTo(dlg)

    # 切到 MiMo：端点自动填 + 模型预设
    dlg.asr_engine.setCurrentIndex(1)
    dlg._on_asr_engine_changed()
    assert dlg.online_group.isVisibleTo(dlg) and not dlg.local_group.isVisibleTo(dlg)
    idx = dlg.asr_service.findData("mimo")
    dlg.asr_service.setCurrentIndex(idx)
    dlg._on_asr_service_changed()
    assert dlg.asr_base_url_input.text() == "https://api.xiaomimimo.com/v1"
    assert dlg._combo_value(dlg.asr_model) == "mimo-v2.5-asr"

    # Groq：模型列表切换
    dlg.asr_service.setCurrentIndex(dlg.asr_service.findData("groq"))
    dlg._on_asr_service_changed()
    assert dlg.asr_base_url_input.text() == "https://api.groq.com/openai/v1"
    assert dlg._combo_value(dlg.asr_model) == "whisper-large-v3"

    # 保存回环
    dlg.asr_api_key_input.setText("sk-asr-test")
    dlg._on_ok()
    _ensure_app().processEvents()
    assert s.asr_engine == "online" and s.asr_service == "groq"
    assert s.asr_model == "whisper-large-v3"
    assert s.asr_base_url == "https://api.groq.com/openai/v1"
    assert s.get_asr_api_key() == "sk-asr-test"

    # 重开对话框：加载已存值
    dlg2 = SettingsDialog()
    assert dlg2.asr_engine.currentData() == "online"
    assert dlg2.asr_base_url_input.text() == "https://api.groq.com/openai/v1"
    assert dlg2._combo_value(dlg2.asr_model) == "whisper-large-v3"
    assert dlg2.asr_api_key_input.placeholderText().startswith("已设置")

    # _combo_value：选中预设后手改文本 → 以文本为准（修复「改了字存回旧值」）
    dlg2.asr_model.setCurrentIndex(0)
    dlg2.asr_model.setCurrentText("my-custom-asr")
    assert dlg2._combo_value(dlg2.asr_model) == "my-custom-asr"

    # 还原
    s.asr_engine, s.asr_service, s.asr_model, s.asr_base_url = saved[:4]
    s.set_asr_api_key(saved[4] or "")
    print("设置页引擎/服务联动 + 保存回环 + 下拉值修复 ✓")


def main() -> int:
    tests = [
        test_split_text_evenly,
        test_online_asr_transcriptions_segments,
        test_online_asr_text_fallback,
        test_online_asr_mimo_chat_audio,
        test_online_asr_cancel_and_errors,
        test_worker_dispatch_online,
        test_settings_dialog_asr_wiring,
    ]
    for t in tests:
        t()
    print(f"\n全部通过：{len(tests)} 组测试")
    return 0


if __name__ == "__main__":
    sys.exit(main())
