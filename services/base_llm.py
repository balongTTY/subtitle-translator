"""LLM 服务抽象接口 + 统一重试执行器"""

import logging
import random
import time
from abc import ABC, abstractmethod

from core.translation_batch import TranslationBatch

log = logging.getLogger("subtitle_translator")

# 单次重试等待上限：服务端 Retry-After 可能给到分钟/小时级，
# 不封顶的话 worker 线程会静默挂起任意久，GUI 无提示也无法取消
MAX_RETRY_DELAY = 60.0


class DeterministicError(RuntimeError):
    """确定性失败（输出截断/解析失败/内容过滤等）——重试必然同样失败，
    _execute 收到后不重试直接上抛，避免重复计费"""


class BaseLLMService(ABC):
    """LLM 提供商的统一抽象接口。

    子类需提供 self.settings（AppSettings）与 self.rate_limiter；
    API 调用统一走 _execute() 获得一致的重试/退避/配额返还行为
    （旧实现 OpenAI/Claude 各写一份循环，已发生行为漂移）。
    """

    @abstractmethod
    def translate_batch(
        self,
        batch: TranslationBatch,
        source_lang: str,
        target_lang: str,
    ) -> list[str]:
        """
        翻译一个批次，返回翻译后的文本列表。
        按输入顺序返回，仅返回 primary 部分（不含 overlap）。
        """
        ...

    @abstractmethod
    def analyze(
        self,
        prompt: str,
    ) -> str:
        """发送全篇分析请求，返回 LLM 响应文本"""
        ...

    @abstractmethod
    def validate_api_key(self) -> bool:
        """验证 API Key 是否有效"""
        ...

    @property
    @abstractmethod
    def provider_name(self) -> str:
        ...

    @property
    @abstractmethod
    def default_model(self) -> str:
        ...

    # ------------------------------------------------------------------
    #  统一重试执行器
    # ------------------------------------------------------------------

    # 这些状态码是确定性失败（认证/授权/请求格式错误），重试必然同样失败，
    # 直接抛出——既省时间也避免触发 provider 风控。其余（429/5xx/网络错误）可重试。
    NON_RETRYABLE_STATUS = frozenset({400, 401, 403, 404, 405, 422})

    def _execute(self, action: str, fn, limiter, estimated_tokens: int,
                 backoff_base: float = 2.0):
        """执行一次 API 调用，统一处理限流扣费、异常分流、重试与配额返还。

        fn: () -> T  实际 API 调用（不含 acquire）
        返回 fn 的返回值；重试耗尽或不可重试时抛 RuntimeError。
        """
        # 设置项无下限校验（QSettings 可能存到 0），至少重试 1 次
        max_retries = max(1, int(self.settings.max_retries))
        for attempt in range(max_retries):
            limiter.acquire(estimated_tokens)
            try:
                return fn()
            except DeterministicError:
                # 截断/解析失败等确定性失败：重试结果相同，直接上抛
                limiter.refund(estimated_tokens)
                raise
            except Exception as e:
                # 请求未成功——返还预扣配额，避免重试重复计费挤占后续批次
                limiter.refund(estimated_tokens)
                retryable, retry_after = self._classify_error(e)
                if not retryable:
                    raise RuntimeError(f"{action}失败（不可重试）: {e}") from e
                if attempt < max_retries - 1:
                    delay = min(retry_after or backoff_base * (2 ** attempt), MAX_RETRY_DELAY)
                    # 随机抖动：parallel_files>1 时避免多个 worker 同节拍
                    # 重试互相顶撞配额（同步惊群）
                    delay += random.uniform(0, min(2.0, delay * 0.25))
                    log.warning(
                        "%s 调用失败 (尝试 %d/%d，%.1fs 后重试): %s",
                        action, attempt + 1, max_retries, delay, e,
                    )
                    time.sleep(delay)
                else:
                    raise RuntimeError(
                        f"{action}失败，已重试 {max_retries} 次: {e}"
                    ) from e
        raise RuntimeError(f"{action}失败")  # 理论不可达，防御 max_retries<=0

    @classmethod
    def _classify_error(cls, e: Exception) -> tuple[bool, float | None]:
        """异常分流 → (是否可重试, 建议等待秒数)。

        openai/anthropic SDK 的 APIStatusError 都有 .status_code 和
        .response.headers（duck-typing，无需 import 两个 SDK）。
        429 优先用服务端 Retry-After（仅支持数值秒，HTTP 日期格式忽略），
        由 _execute 统一封顶。
        """
        status = getattr(e, "status_code", None)
        if isinstance(status, int):
            if status == 429:
                retry_after = None
                headers = getattr(getattr(e, "response", None), "headers", None)
                if headers:
                    try:
                        retry_after = float(headers.get("retry-after"))
                    except (TypeError, ValueError):
                        retry_after = None
                return True, retry_after
            if status in cls.NON_RETRYABLE_STATUS:
                return False, None
            return True, None  # 5xx 等服务端错误
        return True, None  # 超时/连接错误等无状态码异常
