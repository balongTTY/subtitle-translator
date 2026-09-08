"""Claude (Anthropic) 翻译服务"""

import logging
import re

from anthropic import Anthropic

from config.settings import AppSettings
from services.base_llm import BaseLLMService, DeterministicError
from services.rate_limiter import get_rate_limiter
from services.prompt_builder import (
    build_translation_prompt, build_user_message, parse_response,
)
from core.translation_batch import TranslationBatch
from utils.token_counter import estimate_tokens

log = logging.getLogger("subtitle_translator")

# 批级可变内容区段标题：这些区块随批次/QC 重试变化（topic_context、建议术语、
# 预设参考术语等），不属于跨批恒定前缀，不能放进 Prompt Caching 的缓存块
_BATCH_VARIABLE_SECTION_RE = re.compile(
    r"^## (当前场景|强制术语|AI 建议术语|预设参考术语)"
)


class ClaudeService(BaseLLMService):
    """Claude 翻译实现，支持 prompt caching（仅缓存跨批恒定前缀）"""

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
        self._client: Anthropic | None = None
        self._last_key: str = ""
        self.rate_limiter = get_rate_limiter(60000, 30)

    @property
    def provider_name(self) -> str:
        return self._override_provider or "anthropic"

    @property
    def default_model(self) -> str:
        return self._override_model or "claude-sonnet-5"

    def _get_client(self) -> Anthropic:
        if self._override_api_key is not None:
            api_key = self._override_api_key
        else:
            api_key = self.settings.get_api_key("anthropic")
        if self._override_base_url is not None:
            base_url = self._override_base_url or None
        else:
            base_url = self.settings.get("base_url_anthropic", "") or None

        # key 或端点变化时重建客户端（兼容 Anthropic 格式的第三方端点）。
        # max_retries=0：禁用 SDK 内置自动重试——重试策略由 _execute 统一拥有
        cache_key = f"{api_key}|{base_url}"
        if self._client is None or cache_key != self._last_key:
            kwargs: dict = {"api_key": api_key, "max_retries": 0}
            if base_url:
                kwargs["base_url"] = base_url
            self._client = Anthropic(**kwargs)
            self._last_key = cache_key

        return self._client

    @staticmethod
    def _extract_text(response) -> str:
        """拼接响应中全部 text block。

        content 是 block 列表：首块可能是 thinking（.text 属性不存在），
        译文也可能拆成多个 text block——只取 content[0] 会 AttributeError
        被误判成 API 失败，或整段丢失后半译文。
        """
        parts = []
        for block in getattr(response, "content", None) or []:
            if getattr(block, "type", "") == "text":
                parts.append(getattr(block, "text", ""))
        return "".join(parts)

    def translate_batch(
        self, batch: TranslationBatch, source_lang: str, target_lang: str
    ) -> list[str]:
        client = self._get_client()
        model = self._override_model or self.settings.model
        temperature = self.settings.temperature

        system_prompt = self._build_system_prompt(batch, source_lang, target_lang)
        user_message = build_user_message(batch)

        # Prompt Caching 采用精确前缀匹配，且断点只能在块边界：
        # 若对整个 system prompt 打 cache_control（旧实现），topic_context / 建议术语 /
        # 预设参考术语等批级内容会使前缀逐批漂移，缓存永远零命中、反而多付 1.25x 费用。
        # 故拆成「跨批恒定规则（打缓存标记）」+「批级可变内容（不缓存）」两块，
        # 恒定前缀跨批复用才能真正命中（ja→zh 恒定部分实测约 1359 token，超过 1024 下限）。
        stable_part, batch_part = self._split_cached_prompt(system_prompt)
        system_blocks = [
            {"type": "text", "text": stable_part, "cache_control": {"type": "ephemeral"}}
        ]
        if batch_part:
            system_blocks.append({"type": "text", "text": batch_part})

        def call() -> list[str]:
            response = client.messages.create(
                model=model,
                max_tokens=8192,
                system=system_blocks,
                messages=[{"role": "user", "content": user_message}],
                temperature=temperature,
                timeout=120,
            )
            # 截断检测：不查 stop_reason 的话尾部条目会静默缺失、被原文填充
            if response.stop_reason == "max_tokens":
                raise DeterministicError(
                    f"批次 {batch.batch_id} 输出在 max_tokens=8192 处截断，"
                    "请减小 chunk_tokens 或拆分批次"
                )
            return parse_response(self._extract_text(response), batch)

        return self._execute("翻译", call, self.rate_limiter, batch.estimated_tokens)

    def analyze(self, prompt: str) -> str:
        client = self._get_client()
        model = self._override_model or self.settings.model
        token_limit = self.settings.analysis_tokens

        # 分析用的 system 块是固定短句（约 14 token），远低于 1024 token 最小可缓存前缀，
        # cache_control 会被 API 静默忽略，标记无意义，故不再添加
        system_prompt = [{"type": "text", "text": "分析字幕内容，以 JSON 格式回复。"}]

        # 限流按实际 prompt 大小扣费：full 模式可把整篇字幕塞进 prompt，
        # 实际体积可超过固定 analysis_tokens 上限的 2.8 倍，按固定值计费会欠计吞吐、
        # 触发更多 429。estimate_tokens 内部已有朴素估算兜底，不会抛异常
        acquire_tokens = estimate_tokens(prompt, model) + estimate_tokens(
            "分析字幕内容，以 JSON 格式回复。", model
        )

        def call() -> str:
            response = client.messages.create(
                model=model,
                max_tokens=min(token_limit, 8192),
                system=system_prompt,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                timeout=90,
            )
            return self._extract_text(response)

        return self._execute(
            "全篇分析", call, self.rate_limiter, acquire_tokens, backoff_base=3.0,
        )

    def validate_api_key(self) -> bool:
        try:
            client = self._get_client()
            model = (self._override_model or self.settings.model) or "claude-sonnet-5"
            client.messages.create(
                model=model,
                max_tokens=1,
                messages=[{"role": "user", "content": "hi"}],
                timeout=15,
            )
            return True
        except Exception as e:
            log.warning("Claude Key 验证失败: %s", e)
            return False

    def _build_system_prompt(
        self, batch: TranslationBatch, source_lang: str, target_lang: str
    ) -> str:
        return build_translation_prompt(batch, source_lang, target_lang)

    @staticmethod
    def _split_cached_prompt(system_prompt: str) -> tuple[str, str]:
        """把系统提示词拆成「跨批恒定前缀」与「批级可变内容」两段。

        恒定前缀（角色说明 + 基本规则 + 语言专项指南）在各批次间完全相同，
        作为 Prompt Caching 的缓存块；从第一个批级可变区段（当前场景 / 术语表）起，
        内容随批次、glossary 与 QC 重试变化，放到缓存块之后不标记 cache。
        """
        lines = system_prompt.split("\n")
        split_at = len(lines)
        for i, line in enumerate(lines):
            if _BATCH_VARIABLE_SECTION_RE.match(line):
                split_at = i
                break
        stable = "\n".join(lines[:split_at])
        batch_part = "\n".join(lines[split_at:])
        return stable, batch_part
