"""OnlineASR / audio_splitter 修复锁定测试

覆盖审计修复：
- P1-1 chat_audio 切块体积按 base64 膨胀规划 + 运行时护栏按 b64 计量
- P1-2 input_audio.format 按扩展名推导（wav 直传不再误标 mp3）
- P2-1 瞬态错误指数退避重试
"""

import base64
import fractions
import sys
import threading
from pathlib import Path

import av
import httpx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.audio_splitter import iter_audio_chunks
from core.asr_online import OnlineASR


def _make_wav(path: str, seconds: int = 5) -> None:
    """生成真实可解码的 wav（每帧 1s 静音，共 seconds 秒）"""
    out = av.open(path, "w", format="wav")
    stream = out.add_stream("pcm_s16le", rate=16000)
    stream.layout = "mono"
    for i in range(seconds):
        frame = av.AudioFrame("s16", "mono", 16000)
        frame.sample_rate = 16000
        frame.pts = i * 16000
        frame.time_base = fractions.Fraction(1, 16000)
        for p in stream.encode(frame):
            out.mux(p)
    for p in stream.encode(None):
        out.mux(p)
    out.close()


# ---- 假 chat_audio 客户端 ----

class _Msg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Msg(content)


class _Resp:
    def __init__(self, content):
        self.choices = [_Choice(content)]


class _Completions:
    def __init__(self):
        self.captured = None

    def create(self, **kwargs):
        self.captured = kwargs
        return _Resp("テスト。こんにちは。")


class _FakeChatClient:
    def __init__(self):
        self.chat = type("Chat", (), {})()
        self.chat.completions = _Completions()


def _make_asr(max_upload_mb: float = 25.0) -> OnlineASR:
    return OnlineASR(
        base_url="http://fake.local/v1",
        api_key="sk-test",
        model="fake-asr",
        protocol="chat_audio",
        max_upload_mb=max_upload_mb,
    )


class TestChatAudioFormat:
    def test_wav_chunk_declares_wav_format(self, tmp_path):
        """P1-2：wav 直传时 format 必须是 wav（不再硬编码 mp3）"""
        wav = str(tmp_path / "chunk.wav")
        _make_wav(wav, seconds=1)
        asr = _make_asr()
        client = _FakeChatClient()
        entries = asr._transcribe_chat_audio(client, wav, 0.0, 10.0, "ja")
        assert len(entries) >= 1
        payload = client.chat.completions.captured["messages"][0]["content"][0]
        assert payload["input_audio"]["format"] == "wav"
        assert payload["input_audio"]["data"].startswith("data:audio/wav;base64,")

    def test_mp3_chunk_declares_mp3_format(self, tmp_path):
        mp3 = tmp_path / "chunk.mp3"
        mp3.write_bytes(b"\xff\xfb" + b"\x00" * 256)  # 假 mp3 字节（不解码，仅读大小）
        asr = _make_asr()
        client = _FakeChatClient()
        asr._transcribe_chat_audio(client, str(mp3), 0.0, 10.0, "ja")
        payload = client.chat.completions.captured["messages"][0]["content"][0]
        assert payload["input_audio"]["format"] == "mp3"
        assert payload["input_audio"]["data"].startswith("data:audio/mpeg;base64,")


class TestBase64Guard:
    def test_oversize_after_base64_rejected(self, tmp_path):
        """P1-1：护栏按 base64 后体积校验，而不是原始字节数"""
        wav = str(tmp_path / "chunk.wav")
        _make_wav(wav, seconds=5)
        raw = Path(wav).read_bytes()
        b64_len = len(base64.b64encode(raw))
        # 上限设为恰好容不下 base64 体积但容得下原始字节
        max_mb = (len(raw) + 1024) / (1024 * 1024)
        assert len(raw) < max_mb * 1024 * 1024          # 原始字节在限内（旧护栏会放行）
        assert b64_len + 1024 > max_mb * 1024 * 1024    # b64 超限（新护栏应拦截）
        asr = _make_asr(max_upload_mb=max_mb)
        with pytest.raises(RuntimeError, match="超过服务上限"):
            asr._transcribe_chat_audio(_FakeChatClient(), wav, 0.0, 10.0, "ja")


class TestTransientRetry:
    def test_transient_error_retried_then_succeeds(self, monkeypatch):
        from openai import APIConnectionError

        sleeps: list[float] = []
        monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))
        asr = _make_asr()
        req = httpx.Request("POST", "http://fake.local/v1/x")
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise APIConnectionError(request=req)
            return "ok"

        assert asr._create_with_retry(flaky) == "ok"
        assert calls["n"] == 3
        assert sleeps == [1, 2]  # 指数退避

    def test_rate_limit_backoff_doubled(self, monkeypatch):
        from openai import RateLimitError

        sleeps: list[float] = []
        monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))
        asr = _make_asr()
        req = httpx.Request("POST", "http://fake.local/v1/x")
        resp = httpx.Response(429, request=req)
        calls = {"n": 0}

        def limited():
            calls["n"] += 1
            raise RateLimitError("rate limited", response=resp, body=None)

        with pytest.raises(RateLimitError):
            asr._create_with_retry(limited)
        assert calls["n"] == 3
        assert sleeps == [2, 4]  # 限流退避 ×2

    def test_non_transient_propagates_immediately(self, monkeypatch):
        sleeps: list[float] = []
        monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))
        asr = _make_asr()
        calls = {"n": 0}

        def broken():
            calls["n"] += 1
            raise ValueError("认证失败")

        with pytest.raises(ValueError):
            asr._create_with_retry(broken)
        assert calls["n"] == 1
        assert sleeps == []


class TestChunkSizeRegression:
    def test_every_chunk_base64_within_limit(self, tmp_path):
        """P1-1 回归：重编码切块必须满足 b64(块) ≤ max_upload_mb（旧 0.85 系数必挂）"""
        wav = str(tmp_path / "long.wav")
        _make_wav(wav, seconds=60)
        max_mb = 0.5
        chunk_dir = tmp_path / "chunks"
        chunk_dir.mkdir()  # iter_audio_chunks 约定：调用方负责创建目录
        chunks = list(iter_audio_chunks(
            wav, max_mb, direct_formats=(".mp3", ".wav"),
            tmp_dir=str(chunk_dir),
        ))
        assert len(chunks) > 1  # 60s wav 远超 0.5MB → 必须切块
        for t0, t1, chunk in chunks:
            if chunk == wav:
                continue
            raw = Path(chunk).read_bytes()
            b64 = base64.b64encode(raw)
            assert len(b64) <= max_mb * 1024 * 1024, (
                f"块 [{t0:.0f},{t1:.0f}s] base64 后 {len(b64)}B 超过 {max_mb}MB 上限"
            )
            # 连续性：块窗口首尾相接
        spans = [(t0, t1) for t0, t1, _ in chunks]
        for (a0, a1), (b0, b1) in zip(spans, spans[1:]):
            assert b0 == pytest.approx(a1, abs=0.01)
