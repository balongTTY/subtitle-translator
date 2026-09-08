"""tiktoken 封装，用于估算 token 数量"""

import logging
from functools import lru_cache

import tiktoken

log = logging.getLogger("subtitle_translator")

# tiktoken 模型映射
MODEL_ENCODING_MAP = {
    "gpt-5": "o200k_base",
    "gpt-5-mini": "o200k_base",
    "gpt-4o": "o200k_base",
    "gpt-4o-mini": "o200k_base",
    "gpt-4.1": "o200k_base",
    "gpt-4.1-mini": "o200k_base",
    "gpt-4.1-nano": "o200k_base",
    "gpt-4-turbo": "cl100k_base",
    # Claude 使用 cl100k_base 近似估算（Claude tokenizer 不公开）
    "claude-fable-5": "cl100k_base",
    "claude-opus-5": "cl100k_base",
    "claude-sonnet-5": "cl100k_base",
    "claude-haiku-4-5-20251001": "cl100k_base",
    "claude-sonnet-4-20250514": "cl100k_base",
    "claude-opus-4-20250514": "cl100k_base",
    "claude-haiku-4-20250514": "cl100k_base",
}

_encoding_cache: dict[str, "tiktoken.Encoding | None"] = {}
# tiktoken 编码数据（BPE 词表）加载失败时置 True，回退朴素估算（避免逐条告警刷屏）
_fallback_used = False


def get_encoding(model: str) -> "tiktoken.Encoding | None":
    """获取模型对应的 tiktoken 编码器。

    冷缓存且离线时 tiktoken 无法下载 BPE 数据会抛 ConnectionError，
    此处捕获并回退朴素估算，避免整条流水线失败。
    失败的编码名以 None 缓存——否则每条字幕的估算都会重新发起一次
    注定失败的网络尝试，批量估算性能塌陷。
    """
    global _fallback_used
    encoding_name = MODEL_ENCODING_MAP.get(model, "cl100k_base")
    if encoding_name not in _encoding_cache:
        try:
            _encoding_cache[encoding_name] = tiktoken.get_encoding(encoding_name)
        except Exception as e:
            if not _fallback_used:
                log.warning(
                    "tiktoken 编码数据加载失败（冷缓存且离线？），"
                    "已回退朴素 token 估算: %s",
                    e,
                )
                _fallback_used = True
            _encoding_cache[encoding_name] = None  # 短路后续同编码的重复尝试
    return _encoding_cache[encoding_name]


@lru_cache(maxsize=16384)
def estimate_tokens(text: str, model: str = "gpt-4o") -> int:
    """估算文本的 token 数量。

    lru_cache 记忆化：chunker 对同一条目会在分批/间隔断点/overlap 中
    重复估算（最多 3 次），analyzer 全篇编码也会与 chunker 重复——
    字幕文本短且重复率高，缓存命中后万条级文件的估算开销可忽略。
    """
    enc = get_encoding(model)
    if enc is None:
        # 朴素估算：CJK 字符约 1 token/字，其余约 4 字符/token
        cjk = sum(1 for ch in text if _is_cjk_char(ch))
        return max(1, (len(text) - cjk) // 4 + cjk)
    return len(enc.encode(text))


def _is_cjk_char(ch: str) -> bool:
    o = ord(ch)
    return (
        0x4E00 <= o <= 0x9FFF    # CJK 统一汉字
        or 0x3040 <= o <= 0x30FF  # 平假名 / 片假名
        or 0xAC00 <= o <= 0xD7AF  # 谚文
    )
