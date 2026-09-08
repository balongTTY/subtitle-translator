"""OpenAI GPT 翻译服务"""

import logging
import re

from openai import OpenAI

from config.settings import AppSettings
from services.base_llm import BaseLLMService, DeterministicError
from services.rate_limiter import get_rate_limiter
from services.prompt_builder import (
    build_translation_prompt, build_user_message, parse_response,
)
from core.translation_batch import TranslationBatch

log = logging.getLogger("subtitle_translator")

# 推理模型族（gpt-5 / o 系）：Chat Completions 要求 max_completion_tokens
# （传 max_tokens 直接 400 "Unsupported parameter"），且仅支持默认 temperature
_REASONING_MODEL_RE = re.compile(r"^(gpt-5|o[134]($|-))")


def is_reasoning_model(model: str) -> bool:
    return bool(model) and bool(_REASONING_MODEL_RE.match(model.strip().lower()))


def chat_completion_kwargs(
    model: str, temperature: float | None = None, max_tokens: int | None = None,
) -> dict:
    """按模型族构造补全参数：推理模型用 max_completion_tokens 且不传 temperature。

    默认模型是 gpt-5-mini——不改这里的话出厂默认配置下所有请求都会 400。
    """
    kwargs: dict = {}
    if is_reasoning_model(model):
        if max_tokens is not None:
            kwargs["max_completion_tokens"] = max(16, max_tokens)  # 推理模型下限 16
    else:
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
    return kwargs


class OpenAIService(BaseLLMService):
    """OpenAI 兼容 API 翻译实现（支持 OpenAI / DeepSeek / 自定义端点）"""

    def __init__(
        self,
        *,
        provider: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        """显式配置参数仅供「测试连接」等临时场景覆盖全局设置，
        None 表示走全局 AppSettings（保持向后兼容）。
        """
        self.settings = AppSettings()
        self._override_provider = provider
        self._override_api_key = api_key
        self._override_base_url = base_url
        self._override_model = model
        self._client: OpenAI | None = None
        self._last_key: str = ""
        self._last_base_url: str = ""
        self.rate_limiter = get_rate_limiter(90000, 60)

    @property
    def provider_name(self) -> str:
        return self._override_provider or self.settings.provider

    @property
    def default_model(self) -> str:
        return self._override_model or self.settings.model

    def _get_client(self) -> OpenAI:
        provider = self._override_provider or self.settings.provider
        if self._override_api_key is not None:
            api_key = self._override_api_key
        else:
            api_key = self.settings.get_api_key(provider)
        if self._override_base_url is not None:
            base_url = self._override_base_url or None
        else:
            base_url = self.settings.custom_base_url or None

        # 如果 key 或 base_url 变了，重建客户端。
        # max_retries=0：禁用 SDK 内置自动重试（默认 2 次）——重试策略由
        # _execute 统一拥有，双层重试相乘最坏 9 次调用
        cache_key = f"{api_key}|{base_url}"
        if self._client is None or cache_key != self._last_key:
            kwargs = {"api_key": api_key, "max_retries": 0}
            if base_url:
                kwargs["base_url"] = base_url
            self._client = OpenAI(**kwargs)
            self._last_key = cache_key

        return self._client

    def translate_batch(
        self, batch: TranslationBatch, source_lang: str, target_lang: str
    ) -> list[str]:
        client = self._get_client()
        model = self._override_model or self.settings.model
        temperature = self.settings.temperature

        system_prompt = self._build_system_prompt(batch, source_lang, target_lang)
        user_message = build_user_message(batch)

        def call() -> list[str]:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                timeout=120,
                **chat_completion_kwargs(model, temperature=temperature),
            )
            # 截断/过滤检测：空 choices（部分兼容网关在内容过滤时返回）与
            # finish_reason="length"（输出截断）都属确定性失败，重试结果相同
            if not response.choices:
                raise DeterministicError(
                    f"批次 {batch.batch_id} API 返回空 choices（可能被内容过滤）"
                )
            choice = response.choices[0]
            if choice.finish_reason == "length":
                raise DeterministicError(
                    f"批次 {batch.batch_id} 输出被 max_tokens 截断，"
                    "请减小 chunk_tokens 或拆分批次"
                )
            if choice.finish_reason == "content_filter":
                raise DeterministicError(f"批次 {batch.batch_id} 输出被内容过滤")
            return parse_response(choice.message.content or "", batch)

        return self._execute("翻译", call, self.rate_limiter, batch.estimated_tokens)

    def analyze(self, prompt: str) -> str:
        client = self._get_client()
        model = self._override_model or self.settings.model
        token_limit = self.settings.analysis_tokens

        def call() -> str:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "分析字幕内容，以 JSON 格式回复。"},
                    {"role": "user", "content": prompt},
                ],
                timeout=90,
                # 分析输出（JSON）不会很大；封顶避免超过模型输出上限
                **chat_completion_kwargs(
                    model, temperature=0.2, max_tokens=min(token_limit, 8192),
                ),
            )
            return response.choices[0].message.content or ""

        return self._execute(
            "全篇分析", call, self.rate_limiter, token_limit, backoff_base=3.0,
        )

    def validate_api_key(self) -> bool:
        try:
            client = self._get_client()
            model = (self._override_model or self.settings.model) or "gpt-5-mini"
            # 用最小 chat completion 验证，兼容所有 OpenAI 格式 API（包括 DeepSeek）。
            # 短超时：坏网络下不能把 GUI 卡住几分钟
            client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "hi"}],
                timeout=15,
                **chat_completion_kwargs(model, max_tokens=1),
            )
            return True
        except Exception as e:
            log.warning("%s 连接验证失败: %s", self.provider_name, e)
            return False

    def _build_system_prompt(
        self, batch: TranslationBatch, source_lang: str, target_lang: str
    ) -> str:
        return build_translation_prompt(batch, source_lang, target_lang)
