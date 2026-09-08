"""音频按时间切块 — 在线 ASR 单请求大小上限的通用预处理

在线识别接口都有硬上限（MiMo base64 后 10MB、OpenAI/Groq transcriptions 25MB），
一小时录播远超上限。切块用 PyAV（faster-whisper 自带）重编码为
mono 16kHz 64kbps mp3（约 8KB/s），按时间窗口分次识别后拼接时间轴。
"""

import logging
import os
import tempfile
from pathlib import Path

import av

log = logging.getLogger("subtitle_translator")

# 重编码目标参数：mono 16kHz 64kbps mp3 ≈ 8KB/s（+~5% 容器开销）
_CHUNK_BYTES_PER_SEC = 8400
# chat_audio（base64 直传）的安全余量：base64 膨胀 ×4/3，0.74 留出
# 请求体其他字段 + base64 计算的 ~1KB 级余量（0.85 时膨胀后超限 13%）。
# transcriptions 等文件直传协议无 base64 膨胀，可放宽（见 _DIRECT_SIZE_SAFETY）
_SIZE_SAFETY = 0.74
_DIRECT_SIZE_SAFETY = 0.85
# 最短块长（秒）：避免极小上限切出海量碎片请求
_MIN_CHUNK_SECS = 30


def probe_duration(path: str) -> float | None:
    """探测媒体时长（秒）；探测失败返回 None"""
    try:
        with av.open(path) as c:
            if c.duration and c.duration > 0:
                return c.duration / 1_000_000
            for s in c.streams.audio:
                if s.duration and s.duration > 0:
                    return float(s.duration * s.time_base)
    except Exception as e:
        log.warning("探测音频时长失败 %s: %s", path, e)
    return None


def _input_bitrate(path: str) -> float | None:
    """探测输入文件的音频码率（bytes/s）；失败返回 None"""
    try:
        with av.open(path) as c:
            if c.bit_rate:
                return c.bit_rate / 8.0
            for s in c.streams.audio:
                if s.bit_rate:
                    return s.bit_rate / 8.0
    except Exception:
        return None
    return None


def iter_audio_chunks(
    path: str,
    max_upload_mb: float,
    direct_formats: tuple[str, ...] = (),
    tmp_dir: str | None = None,
):
    """产出 (start_s, end_s, chunk_path)。

    - 原文件大小在安全限内 且（无格式限制 或 扩展名在 direct_formats 内）
      → 单块直传原文件（零重编码，质量无损）
    - 否则按时间窗口重编码切块，临时文件调用方负责删除（这里返回的
      chunk_path == 原路径时表示直传，不能删）
    """
    size = os.path.getsize(path)
    ext = Path(path).suffix.lower()
    # direct_formats 非空即 chat_audio 协议——直传原文件同样要过 base64，
    # 膨胀不可避免，两条路径都用 0.74；transcriptions（无格式限制）为
    # multipart 直传，无 base64 膨胀 → 放宽到 0.85
    safety = _DIRECT_SIZE_SAFETY if not direct_formats else _SIZE_SAFETY
    limit_bytes = max_upload_mb * 1024 * 1024 * safety

    if size <= limit_bytes and (not direct_formats or ext in direct_formats):
        duration = probe_duration(path) or (size / _CHUNK_BYTES_PER_SEC)
        yield 0.0, duration, path
        return

    duration = probe_duration(path)
    if not duration or duration <= 0:
        # 无法定位时间轴时长时按输入自身码率估算（8400 B/s 是输出 mp3 的
        # 码率，不能用来算输入时长——WAV 会被估长 20 倍，切出大量空块）
        br = _input_bitrate(path)
        duration = size / br if br else size / max(_CHUNK_BYTES_PER_SEC, 1)

    chunk_secs = max(_MIN_CHUNK_SECS, int(limit_bytes / _CHUNK_BYTES_PER_SEC))
    out_dir = Path(tmp_dir or tempfile.mkdtemp(prefix="asr_chunks_"))

    start = 0.0
    idx = 0
    while start < duration - 0.01:
        end = min(start + chunk_secs, duration)
        chunk_path = out_dir / f"chunk_{idx:04d}.mp3"
        _encode_chunk(path, start, end, str(chunk_path))
        yield start, end, str(chunk_path)
        start = end
        idx += 1


def _encode_chunk(src: str, start: float, end: float, out_path: str) -> None:
    """按 [start, end) 时间窗重编码一段 mono 16kHz mp3"""
    inp = av.open(src)
    try:
        # 容器级 seek（微秒）；随后逐帧跳到窗口起点
        inp.seek(int(max(start - 0.2, 0) * 1_000_000))
        out = av.open(out_path, "w", format="mp3")
        try:
            stream = out.add_stream("libmp3lame", rate=16000)
            stream.layout = "mono"
            stream.bit_rate = 64000
            for frame in inp.decode(audio=0):
                t = frame.time if frame.time is not None else 0.0
                if t < start:
                    continue
                if t >= end:
                    break
                # 帧参数与流模板不一致时 PyAV 自动重采样/重排
                for packet in stream.encode(frame):
                    out.mux(packet)
            for packet in stream.encode(None):  # flush
                out.mux(packet)
        finally:
            out.close()
    finally:
        inp.close()
