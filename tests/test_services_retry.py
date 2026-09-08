"""services 层修复锁定测试 — 截断检测 / DeterministicError / 退避封顶"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.base_llm import BaseLLMService, DeterministicError
from core.translation_batch import SubtitleEntry, TranslationBatch


def _make_batch(n: int = 2) -> TranslationBatch:
    entries = [
        SubtitleEntry(index=i, start=i * 1000, end=i * 1000 + 500,
                      text=f"原文{i}", original_text=f"原文{i}")
        for i in range(n)
    ]
    return TranslationBatch(
        batch_id=0, entries=entries,
        primary_start_idx=0, primary_end_idx=n,
    )


class _StubLimiter:
    def __init__(self):
        self.refunds = 0

    def acquire(self, tokens):
        pass

    def refund(self, tokens):
        self.refunds += 1


class _StubService(BaseLLMService):
    """最小具体实现，用于测 _execute/_classify_error"""

    def __init__(self, max_retries: int = 3):
        self.settings = SimpleNamespace(max_retries=max_retries)
        self.rate_limiter = _StubLimiter()

    def translate_batch(self, batch, source_lang, target_lang):
        return []

    def analyze(self, prompt):
        return ""

    def validate_api_key(self):
        return True

    @property
    def provider_name(self):
        return "stub"

    @property
    def default_model(self):
        return "stub-model"


class _Fake429(Exception):
    def __init__(self, retry_after: str | None = None):
        super().__init__("rate limited")
        self.status_code = 429
        headers = {"retry-after": retry_after} if retry_after else {}
        self.response = SimpleNamespace(headers=headers)


class TestDeterministicError:
    def test_no_retry_no_classification(self):
        """DeterministicError 直接上抛：不重试、退回预扣配额"""
        svc = _StubService(max_retries=3)
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            raise DeterministicError("输出被截断")

        with pytest.raises(DeterministicError):
            svc._execute("翻译", fn, svc.rate_limiter, 100)
        assert calls["n"] == 1
        assert svc.rate_limiter.refunds == 1

    def test_deterministic_error_not_swallowed_by_runtimeerror(self):
        """上抛的是 DeterministicError 本身，不是包壳 RuntimeError"""
        svc = _StubService(max_retries=3)
        with pytest.raises(DeterministicError):
            svc._execute("翻译", lambda: (_ for _ in ()).throw(DeterministicError("x")),
                         svc.rate_limiter, 0)


class TestRetryBackoff:
    def test_retry_after_capped_at_60s(self, monkeypatch):
        """服务端 Retry-After 小时级值必须被封顶，worker 不被无限挂起"""
        sleeps: list[float] = []
        monkeypatch.setattr("services.base_llm.time.sleep", sleeps.append)
        svc = _StubService(max_retries=3)
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            raise _Fake429(retry_after="3600")  # 服务端要求 1 小时

        with pytest.raises(RuntimeError, match="已重试"):
            svc._execute("翻译", fn, svc.rate_limiter, 0)
        assert calls["n"] == 3
        # 每次等待 ≤ 60s 封顶 + 抖动上限 min(2, 60*0.25)=2s
        assert all(s <= 62.0 for s in sleeps), sleeps

    def test_backoff_has_jitter(self, monkeypatch):
        """退避含随机抖动（不精确等于 2^n × base）"""
        observed: list[float] = []
        real_random = __import__("random").uniform

        monkeypatch.setattr("services.base_llm.time.sleep", observed.append)
        monkeypatch.setattr("services.base_llm.random.uniform",
                            lambda a, b: real_random(a, b))
        svc = _StubService(max_retries=2)

        def fn():
            raise _Fake429()  # 无 Retry-After → 走指数退避

        with pytest.raises(RuntimeError):
            svc._execute("翻译", fn, svc.rate_limiter, 0, backoff_base=2.0)
        assert len(observed) == 1
        # base=2 → 2s + 抖动 [0, 0.5] → 必然 > 2.0（纯退避无抖动时恰好 2.0）
        assert observed[0] > 2.0


class TestTruncationDetection:
    def test_openai_empty_choices_rejected(self, monkeypatch):
        """空 choices（内容过滤/网关异常）→ DeterministicError，不重试不解析"""
        from services.openai_service import OpenAIService

        svc = OpenAIService(api_key="sk-test", base_url="http://fake.local/v1")
        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **kw: SimpleNamespace(choices=[],
                                                finish_reason=None, model="m"),
        )))
        svc._client = fake_client
        svc._last_key = "sk-test|http://fake.local/v1"

        with pytest.raises(DeterministicError, match="空 choices"):
            svc.translate_batch(_make_batch(), "ja", "zh")

    def test_openai_length_truncation_rejected(self, monkeypatch):
        from services.openai_service import OpenAIService

        svc = OpenAIService(api_key="sk-test", base_url="http://fake.local/v1")
        fake_choice = SimpleNamespace(
            finish_reason="length",
            message=SimpleNamespace(content="0. 部分译文"),
        )
        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **kw: SimpleNamespace(choices=[fake_choice], model="m"),
        )))
        svc._client = fake_client
        svc._last_key = "sk-test|http://fake.local/v1"

        with pytest.raises(DeterministicError, match="截断"):
            svc.translate_batch(_make_batch(), "ja", "zh")

    def test_claude_max_tokens_truncation_rejected(self):
        """stop_reason=max_tokens → DeterministicError，尾部条目不再静默缺失"""
        from services.claude_service import ClaudeService

        svc = ClaudeService(api_key="sk-test", base_url="http://fake.local/v1")
        fake_response = SimpleNamespace(
            stop_reason="max_tokens",
            content=[SimpleNamespace(type="text", text="0. 部分译文")],
        )
        fake_client = SimpleNamespace(messages=SimpleNamespace(
            create=lambda **kw: fake_response,
        ))
        svc._client = fake_client
        svc._last_key = "sk-test|http://fake.local/v1"

        with pytest.raises(DeterministicError, match="截断"):
            svc.translate_batch(_make_batch(), "ja", "zh")

    def test_openai_normal_response_still_parsed(self):
        """正常响应不受截断检测影响"""
        from services.openai_service import OpenAIService

        svc = OpenAIService(api_key="sk-test", base_url="http://fake.local/v1")
        fake_choice = SimpleNamespace(
            finish_reason="stop",
            message=SimpleNamespace(content="0. 你好\n1. 再见"),
        )
        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **kw: SimpleNamespace(choices=[fake_choice], model="m"),
        )))
        svc._client = fake_client
        svc._last_key = "sk-test|http://fake.local/v1"

        result = svc.translate_batch(_make_batch(2), "ja", "zh")
        assert result == ["你好", "再见"]
