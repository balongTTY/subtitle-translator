"""在线语音识别 — 两种 OpenAI SDK 兼容协议

- transcriptions：/v1/audio/transcriptions 文件直传（OpenAI/Groq/硅基流动），
  verbose_json 返回带时间轴分段——字幕质量最佳
- chat_audio：Chat Completions + input_audio（小米 MiMo 式），base64 传输，
  仅返回文本——时间轴按块边界 + 时长均分估算

长音频由 audio_splitter 按大小上限自动切块，分次识别后拼接。
"""

import base64
import logging
import re
import threading
import time
from pathlib import Path

from core.audio_splitter import iter_audio_chunks, probe_duration
from core.extractor import ExtractionCancelled, normalize_segment_text
from core.translation_batch import SubtitleEntry

log = logging.getLogger("subtitle_translator")

# 句末切分（保留分隔符），用于无时间轴文本的均分
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?…；;])")
_MAX_CUE_CHARS = 25  # 均分模式下每条字幕的目标长度上限


def check_online_asr_available() -> str | None:
    """openai SDK 是否可用（在线识别走它）。None=可用。"""
    try:
        import openai  # noqa: F401
        return None
    except ImportError as e:
        return str(e)


def split_text_evenly(text: str, start_s: float, end_s: float) -> list[SubtitleEntry]:
    """无时间轴文本 → 按句切分为字幕条目，时长按字符数比例均分。

    这是 chat_audio 协议（如 MiMo）唯一可行的时间轴方案；条目时间轴是
    估算值，跨句朗读节奏会有偏差，但块边界（切块窗口）是真实的。
    """
    text = normalize_segment_text(text)
    if not text or end_s <= start_s:
        return []

    sentences = [s for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
    # 极短句（≤2 字，如「w」「草」）并给下一句；再硬拆超长句
    merged: list[str] = []
    for s in sentences:
        if merged and len(merged[-1]) <= 2:
            merged[-1] += s
        elif len(s) > _MAX_CUE_CHARS * 2:
            for i in range(0, len(s), _MAX_CUE_CHARS):
                merged.append(s[i:i + _MAX_CUE_CHARS])
        else:
            merged.append(s)
    if not merged:
        return []

    total_chars = sum(len(s) for s in merged)
    span = end_s - start_s
    entries: list[SubtitleEntry] = []
    cursor = start_s
    for i, s in enumerate(merged):
        if i == len(merged) - 1:
            cue_dur = max(end_s - cursor, 1.0)
        else:
            cue_dur = max(span * len(s) / max(total_chars, 1), 0.8)
        cue_start, cue_end = cursor, min(cursor + cue_dur, end_s)
        if cue_end <= cue_start:
            cue_end = cue_start + 0.5
        cue_text = normalize_segment_text(s)
        if cue_text:
            entries.append(
                SubtitleEntry(index=len(entries), start=int(cue_start * 1000),
                              end=int(cue_end * 1000), text=cue_text,
                              original_text=cue_text)
            )
        cursor = cue_end
    return entries


class OnlineASR:
    """在线语音识别客户端（transcriptions / chat_audio 双协议）"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        protocol: str = "transcriptions",
        max_upload_mb: float = 25,
        timeout: int = 600,
    ) -> None:
        if not api_key:
            raise ValueError("未配置在线识别 API Key（设置 → 字幕提取 → 在线识别）")
        if not base_url:
            raise ValueError("未配置在线识别 API 端点")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.protocol = protocol
        self.max_upload_mb = max_upload_mb
        self.timeout = timeout

    @classmethod
    def from_settings(cls) -> "OnlineASR":
        from config.settings import AppSettings
        from utils.constants import ASR_SERVICES
        s = AppSettings()
        service = ASR_SERVICES.get(s.asr_service)
        _, default_url, _, protocol, max_mb = service or ("", "", "", "transcriptions", 25)
        return cls(
            base_url=s.asr_base_url or default_url,
            api_key=s.get_asr_api_key(),
            model=s.asr_model,
            protocol=protocol,
            max_upload_mb=max_mb,
            timeout=s.asr_timeout,
        )

    # ---- 客户端（独立方法便于测试注入假客户端） ----

    def _make_client(self):
        from openai import OpenAI
        return OpenAI(api_key=self.api_key, base_url=self.base_url, max_retries=0)

    def _create_with_retry(self, fn, *, attempts: int = 3):
        """瞬态错误（连接/超时/限流/5xx）按指数退避重试；其余异常立即上抛。

        _make_client 显式 max_retries=0（SDK 内建重试对流式/大文件有副作用），
        应用层在这里做有界重试——一次网络抖动不该让已重编码的全部音频块作废。
        """
        try:
            from openai import (
                APIConnectionError,
                APITimeoutError,
                RateLimitError,
                InternalServerError,
            )
            transient = (APIConnectionError, APITimeoutError, RateLimitError, InternalServerError)
        except ImportError:
            transient = ()  # openai 未安装时无从分类，按原样执行一次

        for i in range(attempts):
            try:
                return fn()
            except transient as e:
                if i == attempts - 1:
                    raise
                wait = 2 ** i * (2 if isinstance(e, RateLimitError) else 1)
                log.warning("在线识别请求失败（第 %d/%d 次），%ds 后重试: %s",
                            i + 1, attempts, wait, e)
                time.sleep(wait)

    def validate_api_key(self) -> bool:
        """用 /models 轻量端点验证 Key（不消耗识别额度）"""
        try:
            client = self._make_client()
            client.models.list()
            return True
        except Exception as e:
            log.warning("在线识别 Key 验证失败: %s", e)
            return False

    # ---- 主入口（与本地 SubtitleExtractor.extract 同签名） ----

    def extract(
        self,
        filepath: str,
        language: str | None,
        cancel_event: threading.Event | None = None,
        vad_filter: bool = True,  # 在线接口无本地 VAD，忽略
        on_progress=None,
    ) -> tuple[list[SubtitleEntry], dict]:
        client = self._make_client()
        total = probe_duration(filepath) or 0.0
        entries: list[SubtitleEntry] = []
        tmp_chunks: list[str] = []
        estimated_timeline = True  # 是否为估算时间轴

        try:
            # MiMo 仅接受 wav/mp3；transcriptions 接口接受常见格式直传
            direct = (".mp3", ".wav") if self.protocol == "chat_audio" else ()
            for t0, t1, chunk in iter_audio_chunks(
                filepath, self.max_upload_mb, direct_formats=direct,
            ):
                if cancel_event is not None and cancel_event.is_set():
                    raise ExtractionCancelled(filepath)
                if chunk != filepath:
                    tmp_chunks.append(chunk)
                if self.protocol == "chat_audio":
                    chunk_entries = self._transcribe_chat_audio(client, chunk, t0, t1, language)
                else:
                    chunk_entries, got_timeline = self._transcribe_api(
                        client, chunk, t0, t1, language,
                    )
                    # 任意块返回真实分段（verbose_json）即整体视为真实时间轴
                    if got_timeline:
                        estimated_timeline = False
                entries.extend(chunk_entries)
                if on_progress:
                    pct_ms = int(min(t1, total if total else t1) * 1000)
                    on_progress(pct_ms, int((total or t1) * 1000), "在线识别中…")
        finally:
            for p in tmp_chunks:
                try:
                    Path(p).unlink()
                    Path(p).parent.rmdir()
                except OSError:
                    pass

        if not entries:
            raise RuntimeError(
                "在线识别未返回任何内容（音频可能无声，或模型不支持该音频格式）"
            )
        # 重编号（跨块连续）
        for i, e in enumerate(entries):
            e.index = i
        meta = {
            "language": language or "",
            "duration": total,
            "timeline_estimated": estimated_timeline,
        }
        return entries, meta

    # ---- 协议一：/audio/transcriptions（verbose_json 带时间轴） ----

    def _transcribe_api(self, client, chunk_path, t0, t1, language):
        kwargs = {"response_format": "verbose_json", "timeout": self.timeout}
        if language:
            kwargs["language"] = language

        def _call():
            with open(chunk_path, "rb") as f:
                return client.audio.transcriptions.create(
                    model=self.model, file=f, **kwargs,
                )

        try:
            resp = self._create_with_retry(_call)
        except Exception as e:
            raise RuntimeError(f"在线识别请求失败（{self.model}）: {e}") from e

        segments = getattr(resp, "segments", None) or []
        entries: list[SubtitleEntry] = []
        for seg in segments:
            text = normalize_segment_text(getattr(seg, "text", ""))
            if not text:
                continue
            start_s = t0 + float(getattr(seg, "start", 0.0))
            end_s = t0 + float(getattr(seg, "end", start_s + 1))
            if end_s <= start_s:
                end_s = start_s + 0.2
            entries.append(
                SubtitleEntry(index=0, start=int(start_s * 1000), end=int(end_s * 1000),
                              text=text, original_text=text)
            )
        if entries:
            return entries, True

        # 无分段（如 gpt-4o-transcribe 只回纯文本）→ 按块均分
        text = normalize_segment_text(getattr(resp, "text", "") or "")
        return split_text_evenly(text, t0, t1), False

    # ---- 协议二：Chat Completions + input_audio（MiMo 式） ----

    def _transcribe_chat_audio(self, client, chunk_path, t0, t1, language):
        raw = Path(chunk_path).read_bytes()
        b64 = base64.b64encode(raw).decode()
        # 护栏校验 base64 后体积（服务端按传输体计量），而非原始字节数
        if len(b64) + 1024 > self.max_upload_mb * 1024 * 1024:
            raise RuntimeError(
                f"音频块编码后 {len(b64) / 1e6:.1f}MB 超过服务上限 {self.max_upload_mb}MB"
            )
        suffix = Path(chunk_path).suffix.lower()
        if suffix == ".wav":
            mime, fmt = "audio/wav", "wav"
        else:
            mime, fmt = "audio/mpeg", "mp3"
        content = [{
            "type": "input_audio",
            "input_audio": {"data": f"data:{mime};base64,{b64}", "format": fmt},
        }]
        # MiMo 的 language 取值 auto/zh/en；日语/自动 → auto
        asr_lang = "en" if language == "en" else "auto"

        def _call():
            return client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": content}],
                extra_body={"asr_options": {"language": asr_lang}},
                timeout=self.timeout,
            )

        try:
            resp = self._create_with_retry(_call)
        except Exception as e:
            raise RuntimeError(f"在线识别请求失败（{self.model}）: {e}") from e

        text = ""
        try:
            text = resp.choices[0].message.content or ""
        except (AttributeError, IndexError):
            text = ""
        return split_text_evenly(text, t0, t1)
