"""qc_checker 质检回归测试。

覆盖：
- 空译文 / 缺占位符 / 多余占位符
- 平假名残留检测（>30% 告警，片假名豁免）
- 译文==原文的启发式分级（严重 / 非严重短词 / 纯符号豁免）
- 译文过短 / 术语一致性（#62）
- check() 状态标记 与 is_batch_passable 20% 阈值
"""

from core.qc_checker import QCChecker
from core.translation_batch import SubtitleEntry, TranslationResult


def _e(idx: int, text: str) -> SubtitleEntry:
    return SubtitleEntry(index=idx, start=0.0, end=1000.0, text=text, original_text=text)


def _qc() -> QCChecker:
    return QCChecker()


# ---------------------------------------------------------------------------
# 单条检查
# ---------------------------------------------------------------------------

def test_qc_empty_translation():
    assert "译文为空" in _qc()._check_single(_e(0, "こんにちは"), "")


def test_qc_ok_translation():
    assert _qc()._check_single(_e(0, "こんにちは世界"), "你好世界") == []


def test_qc_missing_placeholder():
    r = _qc()._check_single(_e(0, "こんにちは<TAG_0>世界"), "你好世界")
    assert any(i.startswith("缺占位符") for i in r)


def test_qc_extra_placeholder():
    r = _qc()._check_single(_e(0, "こんにちは"), "你好<TAG_0>世界")
    assert any(i.startswith("多余占位符") for i in r)


def test_qc_hiragana_leak_flagged():
    r = _qc()._check_single(_e(0, "こんにちは世界"), "こんにちは 你好")
    assert any(i.startswith("日文残留") for i in r)


def test_qc_hiragana_no_leak_when_translated():
    r = _qc()._check_single(_e(0, "こんにちは世界"), "你好世界")
    assert not any(i.startswith("日文残留") for i in r)


def test_qc_untouched_original_severe():
    """原文含假名且译文==原文 → 严重「译文与原文完全相同」"""
    r = _qc()._check_single(_e(0, "こんにちは世界"), "こんにちは世界")
    assert "译文与原文完全相同" in r


def test_qc_untouched_short_word_nonsevere():
    """短词（GG）原样保留 → 非严重提示，请人工确认"""
    r = _qc()._check_single(_e(0, "GG"), "GG")
    assert "译文与原文相同（短词，请人工确认）" in r


def test_qc_symbols_and_numbers_allowed_untouched():
    """纯符号/数字原样保留 → 豁免，不告警（#57）"""
    assert _qc()._check_single(_e(0, "888"), "888") == []
    assert _qc()._check_single(_e(0, "♪♪♪♪"), "♪♪♪♪") == []


def test_qc_too_short():
    r = _qc()._check_single(_e(0, "こんにちは世界"), "好")
    assert "译文过短" in r


def test_qc_glossary_term_not_used():
    """原文出现强制术语但译文未用对应译法 → 非严重告警（#62）"""
    glossary = {"配信開始": "直播開始"}
    r = _qc()._check_single(_e(0, "配信開始です"), "放送開始です", glossary)
    assert "术语未使用: 配信開始→直播開始" in r


def test_qc_glossary_term_used_no_issue():
    glossary = {"配信開始": "直播開始"}
    r = _qc()._check_single(_e(0, "配信開始"), "直播開始", glossary)
    assert r == []


# ---------------------------------------------------------------------------
# check() / is_batch_passable
# ---------------------------------------------------------------------------

def test_check_statuses():
    entries = [_e(0, "こんにちは世界"), _e(1, "配信開始")]
    translated = ["你好世界", ""]
    results = _qc().check(entries, translated)
    assert [r.status for r in results] == ["qc_passed", "qc_failed"]
    assert results[1].qc_issues == ["译文为空"]


def test_is_batch_passable_false_on_empty():
    results = [
        TranslationResult(index=0, original_text="こんにちは", translated_text="你好",
                          status="qc_passed", qc_issues=[]),
        TranslationResult(index=1, original_text="さようなら", translated_text="",
                          status="qc_failed", qc_issues=["译文为空"]),
    ]
    assert not _qc().is_batch_passable(results)


def _result(index: int, severe: bool) -> TranslationResult:
    if severe:
        return TranslationResult(index=index, original_text="こんにちは世界",
                                 translated_text="こんにちは世界", status="qc_failed",
                                 qc_issues=["译文与原文完全相同"])
    return TranslationResult(index=index, original_text="こんにちは世界",
                             translated_text="你好世界", status="qc_passed", qc_issues=[])


def test_is_batch_passable_20_percent_threshold():
    qc = _qc()
    # 5 条中 1 条严重（20%）→ 恰好通过
    assert qc.is_batch_passable([_result(i, i == 0) for i in range(5)])
    # 5 条中 2 条严重（40%）→ 拒绝
    assert not qc.is_batch_passable([_result(i, i < 2) for i in range(5)])
