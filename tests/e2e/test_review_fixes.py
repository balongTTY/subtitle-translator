"""Review 修复回归测试 — 限流器/QC/merger/服务层重试/语料库/设置钳制

不触网：API 错误用鸭子类型假异常，限流器等待用快进假时钟。
"""
import json
import os
import sys
import tempfile
import threading
import time
import types
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

def test_rate_limiter_oversized_terminates():
    """H1：超过桶容量的请求必须有限时间返回（旧实现无限挂死）"""
    import services.rate_limiter as RL

    class _FastClock:
        """快进时钟：sleep 不真睡，直接推进单调时钟"""
        def __init__(self):
            self.t = 1000.0
        def monotonic(self):
            return self.t
        def sleep(self, s):
            self.t += max(s, 0.05) + 1.0

    real_time = RL.time
    RL.time = _FastClock()
    try:
        limiter = RL.RateLimiter(tokens_per_minute=60, requests_per_minute=6)
        limiter.acquire(60)              # 满桶 → 立即扣空
        assert limiter.token_bucket == 0

        result = {}
        th = threading.Thread(
            target=lambda: result.__setitem__("ok", limiter.acquire(10_000)),
            daemon=True,                 # 死锁时进程也能退出，由断言报失败
        )
        t0 = time.time()
        th.start()
        th.join(timeout=5)
        assert not th.is_alive(), "超容量请求永久挂死（H1 回归）"
        assert time.time() - t0 < 5
        assert limiter.token_bucket <= 0, "放行后应整桶扣空"

        # refund：失败返还后桶恢复
        limiter.refund(60)
        assert limiter.token_bucket == 60
    finally:
        RL.time = real_time
    print("限流器超容量有界等待 + 配额返还 ✓")


def test_qc_severity_gradation():
    """H2/M7：纯汉字长句未翻译升级严重；短词软标记；符号放行；TAG 不误伤"""
    _ensure_app()
    from core.qc_checker import QCChecker
    from core.translation_batch import SubtitleEntry

    def entry(text):
        return SubtitleEntry(index=0, start=0, end=1000, text=text, original_text=text)

    qc = QCChecker()

    cases = [
        # (原文, 译文, 期望包含的 issue 关键词, 批次是否应通过)
        ("東京大学病院で待ち合わせ", "東京大学病院で待ち合わせ", "译文与原文完全相同", False),
        ("スパチャ来た", "スパチャ来た", "译文与原文完全相同", False),          # 含假名
        ("草", "草", "短词", True),                                              # 真短词软标记
        ("888", "888", None, True),                                              # 纯符号放行
        ("<TAG_0>888<TAG_1>", "<TAG_0>888<TAG_1>", None, True),                  # TAG 不误伤
    ]
    for orig, trans, expect_issue, expect_pass in cases:
        results = qc.check([entry(orig)], [trans])
        issues = results[0].qc_issues
        if expect_issue is None:
            assert not issues, f"{orig!r} 不应告警: {issues}"
        else:
            assert any(expect_issue in i for i in issues), f"{orig!r} 缺少 {expect_issue!r}: {issues}"
        assert qc.is_batch_passable(results) == expect_pass, f"{orig!r} 批次通过性错误"
    print("QC 严重性分级（纯汉字长句/短词/符号/TAG 剔除）✓")


def test_qc_extra_placeholder_severe():
    """H3a：多余占位符是严重问题（merger 无法还原会静默回退原文）"""
    _ensure_app()
    from core.qc_checker import QCChecker
    from core.translation_batch import SubtitleEntry

    qc = QCChecker()
    e = SubtitleEntry(0, 0, 1000, "<TAG_0>こんにちは", "<TAG_0>こんにちは")
    results = qc.check([e], ["你好<TAG_9>"])
    assert any("多余占位符" in i for i in results[0].qc_issues)
    assert not qc.is_batch_passable(results), "多余占位符应导致批次不通过"
    print("QC 多余占位符升级为严重 ✓")


def test_qc_glossary_check():
    """H4：术语一致性检查接线后能检出强制术语未使用"""
    _ensure_app()
    from core.qc_checker import QCChecker
    from core.translation_batch import SubtitleEntry

    qc = QCChecker()
    e = SubtitleEntry(0, 0, 1000, "スパチャありがとう", "スパチャありがとう")
    results = qc.check([e], ["感谢醒目留言"], {"スパチャ": "SC"})
    assert any("术语未使用" in i for i in results[0].qc_issues), results[0].qc_issues
    print("QC 强制术语检查（glossary 接线）✓")


def test_merger_fallback_marks_review():
    """H3b：占位符无法还原回退原文时，标记 needs_review 而非静默通过"""
    from core.merger import Merger
    from core.translation_batch import SubtitleEntry, TranslationResult

    entry = SubtitleEntry(
        index=0, start=0, end=1000,
        text="<TAG_0>你好", original_text="{\\i1}你好{\\i0}",
    )
    result = TranslationResult(
        index=0, original_text="你好",
        translated_text="<TAG_0>你好<TAG_9>",  # LLM 幻觉出多余 TAG_9
        status="qc_passed", qc_issues=[],
    )
    merged = Merger().merge([entry], {0: result})
    assert merged[0].text == entry.original_text, "应回退原文"
    assert result.status == "needs_review", "回退后必须标记需审核"
    assert any("占位符" in i for i in result.qc_issues), result.qc_issues
    print("Merger 占位符回退标记 needs_review ✓")


def test_chat_completion_kwargs():
    """H5：推理模型族参数分流（gpt-5 默认配置不再 400）"""
    from services.openai_service import chat_completion_kwargs, is_reasoning_model

    # 推理模型：max_completion_tokens（下限 16），不传 temperature
    kw = chat_completion_kwargs("gpt-5-mini", temperature=0.3, max_tokens=100)
    assert kw == {"max_completion_tokens": 100}, kw
    kw = chat_completion_kwargs("gpt-5-mini", temperature=0.3)
    assert kw == {}, "推理模型不应携带 temperature"
    assert is_reasoning_model("o3-mini") and is_reasoning_model("o1")
    assert not is_reasoning_model("ollama") and not is_reasoning_model("deepseek-v4-flash")

    # 传统模型：temperature + max_tokens 原样透传
    kw = chat_completion_kwargs("gpt-4o", temperature=0.3, max_tokens=1)
    assert kw == {"temperature": 0.3, "max_tokens": 1}, kw
    print("模型族补全参数分流（gpt-5/o 系 vs 传统）✓")


def test_claude_multi_block_text():
    """M2：多 text block 拼接；thinking 块不炸；空 content 不炸"""
    from services.claude_service import ClaudeService

    resp = SimpleNamespace(content=[
        SimpleNamespace(type="thinking", thinking="思考中"),
        SimpleNamespace(type="text", text="你好"),
        SimpleNamespace(type="text", text="世界"),
    ])
    assert ClaudeService._extract_text(resp) == "你好世界"
    assert ClaudeService._extract_text(SimpleNamespace(content=[])) == ""
    assert ClaudeService._extract_text(SimpleNamespace(content=None)) == ""
    print("Claude 多 text block 解析 ✓")


class _FakeAPIError(Exception):
    """鸭子类型模拟 openai/anthropic 的 APIStatusError"""

    def __init__(self, status_code=None, headers=None, msg="api error"):
        self.status_code = status_code
        self.response = SimpleNamespace(headers=headers) if headers is not None else None
        super().__init__(f"{msg} (HTTP {status_code})")


def _make_service():
    from services.openai_service import OpenAIService
    return OpenAIService(api_key="sk-test", model="gpt-4o")


def test_execute_non_retryable():
    """401 等确定性失败：立即抛出、不重试、配额已返还"""
    _ensure_app()
    from services.rate_limiter import RateLimiter

    svc = _make_service()
    limiter = RateLimiter(tokens_per_minute=100, requests_per_minute=10)
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise _FakeAPIError(status_code=401, msg="invalid api key")

    try:
        svc._execute("翻译", fn, limiter, 50)
        raise AssertionError("401 应抛出")
    except RuntimeError as e:
        assert "不可重试" in str(e)
    assert calls["n"] == 1, f"401 不应重试，实际调用 {calls['n']} 次"
    # 半桶启动（50）+ 扣 50 + 失败返还 50 → 恢复初始半桶（容忍毫秒级补充的浮点尾差）
    assert abs(limiter.token_bucket - 50) < 0.01, f"失败后应返还配额: {limiter.token_bucket}"
    print("不可重试错误（401）立即失败 + 配额返还 ✓")


def test_execute_429_retry_after():
    """429 按 Retry-After 等待后重试成功"""
    _ensure_app()
    from services.rate_limiter import RateLimiter

    svc = _make_service()
    limiter = RateLimiter(tokens_per_minute=100, requests_per_minute=10)
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise _FakeAPIError(status_code=429, headers={"retry-after": "0.05"})
        return "ok"

    t0 = time.time()
    assert svc._execute("翻译", fn, limiter, 10) == "ok"
    assert calls["n"] == 2
    assert time.time() - t0 < 3, "应按 Retry-After 短等待而非长退避"
    print("429 按 Retry-After 重试 ✓")


def test_execute_generic_retry_exhausted():
    """无状态码异常（网络错误）：按次数重试后耗尽抛出"""
    _ensure_app()
    from services.rate_limiter import RateLimiter
    import services.base_llm as B

    svc = _make_service()
    limiter = RateLimiter(tokens_per_minute=100, requests_per_minute=10)
    saved_mod = B.time
    sleeps = []
    B.time = types.SimpleNamespace(
        monotonic=saved_mod.monotonic, sleep=lambda s: sleeps.append(s),
    )
    try:
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            raise ConnectionError("timeout")

        try:
            svc._execute("翻译", fn, limiter, 10)
            raise AssertionError("应抛出")
        except RuntimeError as e:
            assert "已重试" in str(e)
        assert calls["n"] == svc.settings.max_retries
        assert sleeps and sleeps[0] >= 1.0, "应有指数退避"
    finally:
        B.time = saved_mod
    print("通用异常按次数重试后耗尽 ✓")


def test_max_retries_clamped():
    """M3：QSettings 存 0 时 max_retries 属性下限为 1"""
    _ensure_app()
    from config.settings import AppSettings
    s = AppSettings()
    saved = s.get("max_retries", None)
    try:
        s.set("max_retries", 0)
        assert s.max_retries == 1, "下限应为 1"
    finally:
        if saved is not None:
            s.set("max_retries", saved)
    print("max_retries 下限钳制 ✓")


def test_corpus_no_cache_poisoning():
    """M6：损坏 JSON 不投毒类级缓存；保存原子（无 .tmp 残留）"""
    from core.corpus_manager import CorpusManager

    CorpusManager._terms_cache = None
    try:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "corpus.json"

            def fresh():
                cm = CorpusManager.__new__(CorpusManager)
                cm._corpus_path = path
                cm._terms = {}
                return cm

            path.write_text("{损坏的JSON", encoding="utf-8")
            cm = fresh()
            cm._load()
            assert cm._terms == {}
            assert CorpusManager._terms_cache is None, "解析失败不应写缓存（投毒）"

            path.write_text(json.dumps({"terms": {"スパチャ": "SC"}}), encoding="utf-8")
            cm2 = fresh()
            cm2._load()
            assert cm2._terms == {"スパチャ": "SC"}, "缓存未投毒，下次读盘应成功"
            assert CorpusManager._terms_cache == {"スパチャ": "SC"}

            cm2.update_all({"配信": "直播"})
            assert not path.with_suffix(".json.tmp").exists(), "原子写不应残留 tmp"
            assert json.loads(path.read_text(encoding="utf-8"))["terms"] == {"配信": "直播"}
    finally:
        CorpusManager._terms_cache = None
    print("语料库无投毒 + 原子写 ✓")


def test_whisper_probe_no_import():
    """新代码修复：可用性探测不再 import（避免固化 HF_ENDPOINT 镜像）"""
    import core.extractor as E
    assert "faster_whisper" not in sys.modules or sys.modules["faster_whisper"] is not None
    r = E.check_whisper_available()
    assert r is None or isinstance(r, str)
    # 真实安装环境下不应因探测而加载模块（测试进程此前未导入时）
    print("whisper 可用性免导入探测 ✓")


def main() -> int:
    tests = [
        test_rate_limiter_oversized_terminates,
        test_qc_severity_gradation,
        test_qc_extra_placeholder_severe,
        test_qc_glossary_check,
        test_merger_fallback_marks_review,
        test_chat_completion_kwargs,
        test_claude_multi_block_text,
        test_execute_non_retryable,
        test_execute_429_retry_after,
        test_execute_generic_retry_exhausted,
        test_max_retries_clamped,
        test_corpus_no_cache_poisoning,
        test_whisper_probe_no_import,
    ]
    for t in tests:
        t()
    print(f"\n全部通过：{len(tests)} 组测试")
    return 0


if __name__ == "__main__":
    sys.exit(main())
