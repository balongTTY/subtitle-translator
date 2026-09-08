"""Merger 单元测试 — 合并翻译结果与标签还原的边界用例"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.merger import Merger
from core.translation_batch import SubtitleEntry, TranslationResult


def make_entry(index: int, text: str, original: str | None = None,
               start: float = 1000, end: float = 2000, style: str = "Default") -> SubtitleEntry:
    return SubtitleEntry(
        index=index,
        start=start,
        end=end,
        text=text if original is None else original,
        original_text=original or text,
        style=style,
    )


class TestMergeBasic:
    def test_translated_result_replaces_text(self):
        entries = [make_entry(0, "こんにちは")]
        results = {0: TranslationResult(index=0, original_text="こんにちは",
                                        translated_text="你好", status="translated")}
        merged = Merger().merge(entries, results)
        assert len(merged) == 1
        assert merged[0].text == "你好"

    def test_missing_result_falls_back_to_original(self):
        entries = [make_entry(0, "こんにちは"), make_entry(1, "さようなら")]
        results = {1: TranslationResult(index=1, original_text="さようなら",
                                        translated_text="再见")}
        merged = Merger().merge(entries, results)
        # 条目 0 无翻译结果 → 保留原文
        assert merged[0].text == "こんにちは"
        assert merged[1].text == "再见"

    def test_timeline_and_style_preserved(self):
        entries = [make_entry(0, "こんにちは", start=5000, end=6500, style="OP")]
        results = {0: TranslationResult(index=0, original_text="こんにちは",
                                        translated_text="你好")}
        merged = Merger().merge(entries, results)
        assert merged[0].start == 5000
        assert merged[0].end == 6500
        assert merged[0].style == "OP"
        assert merged[0].index == 0

    def test_original_text_preserved_in_output(self):
        entries = [make_entry(0, "こんにちは", original="{\\i1}こんにちは{\\i0}")]
        results = {0: TranslationResult(index=0, original_text="{\\i1}こんにちは{\\i0}",
                                        translated_text="<TAG_0>你好<TAG_1>")}
        merged = Merger().merge(entries, results)
        assert merged[0].original_text == "{\\i1}こんにちは{\\i0}"

    def test_empty_entries_returns_empty(self):
        assert Merger().merge([], {}) == []

    def test_empty_results_all_fallback(self):
        entries = [make_entry(0, "あ"), make_entry(1, "い")]
        merged = Merger().merge(entries, {})
        assert [e.text for e in merged] == ["あ", "い"]


class TestTagRestoration:
    def test_ass_tags_restored_from_placeholders(self):
        original = "{\\i1}こんにちは{\\i0}"
        entries = [make_entry(0, "こんにちは", original=original)]
        results = {0: TranslationResult(index=0, original_text=original,
                                        translated_text="<TAG_0>你好<TAG_1>")}
        merged = Merger().merge(entries, results)
        assert merged[0].text == "{\\i1}你好{\\i0}"

    def test_srt_inline_tags_restored(self):
        original = "<i>こんにちは</i>"
        entries = [make_entry(0, "こんにちは", original=original)]
        results = {0: TranslationResult(index=0, original_text=original,
                                        translated_text="<TAG_0>你好<TAG_1>")}
        merged = Merger().merge(entries, results)
        assert merged[0].text == "<i>你好</i>"

    def test_fully_lost_placeholder_passes_through(self):
        """LLM 完全丢掉占位符时 Merger 不做回退——缺失占位符由 QC 层
        （qc_checker 的「缺占位符」检查）负责检测并标记，两层职责不同"""
        original = "{\\i1}こんにちは{\\i0}"
        entries = [make_entry(0, "こんにちは", original=original)]
        result = TranslationResult(index=0, original_text=original,
                                   translated_text="你好")  # 无占位符
        merged = Merger().merge(entries, {0: result})
        assert merged[0].text == "你好"
        assert result.status == "pending"  # Merger 不改状态，状态归 QC/流水线管

    def test_hallucinated_placeholder_falls_back_to_original(self):
        """译文中出现 tag_map 里不存在的占位符（幻觉/大小写错）→ 还原后仍残留
        <TAG_ 字样 → 回退原文并标记 needs_review"""
        original = "{\\i1}危ない{\\i0}"
        entries = [make_entry(0, "危ない", original=original)]
        result = TranslationResult(index=0, original_text=original,
                                   translated_text="<tag_0>危险<tag_1>")  # 小写幻觉
        merged = Merger().merge(entries, {0: result})
        assert merged[0].text == original
        assert result.status == "needs_review"
        assert "标签占位符未还原，已回退原文" in result.qc_issues

    def test_out_of_range_placeholder_falls_back(self):
        original = "{\\b1}強い{\\b0}"
        entries = [make_entry(0, "強い", original=original)]
        result = TranslationResult(index=0, original_text=original,
                                   translated_text="<TAG_7>强<TAG_8>")  # 越界编号
        merged = Merger().merge(entries, {0: result})
        assert merged[0].text == original
        assert result.status == "needs_review"

    def test_lost_placeholder_dedupes_qc_issue(self):
        original = "{\\b1}強い{\\b0}"
        entries = [make_entry(0, "強い", original=original)]
        result = TranslationResult(index=0, original_text=original,
                                   translated_text="强",
                                   qc_issues=["标签占位符未还原，已回退原文"])
        Merger().merge(entries, {0: result})
        assert result.qc_issues.count("标签占位符未还原，已回退原文") == 1

    def test_hallucinated_placeholder_extends_existing_issues(self):
        """幻觉占位符触发回退时，已有 qc_issues 保留且新问题追加"""
        original = "{\\i1}危ない{\\i0}"
        entries = [make_entry(0, "危ない", original=original)]
        result = TranslationResult(index=0, original_text=original,
                                   translated_text="<TAG_9>危险", qc_issues=["译文为空"])
        Merger().merge(entries, {0: result})
        assert "译文为空" in result.qc_issues
        assert "标签占位符未还原，已回退原文" in result.qc_issues

    def test_no_tags_no_placeholder_needed(self):
        """原文无标签时，纯译文直接通过，不触碰结果状态（状态归 QC/流水线管）"""
        entries = [make_entry(0, "普通文本")]
        results = {0: TranslationResult(index=0, original_text="普通文本",
                                        translated_text="普通译文")}
        merged = Merger().merge(entries, results)
        assert merged[0].text == "普通译文"
        assert results[0].status == "pending"


class TestMergeEdgeCases:
    def test_duplicate_index_results_last_wins(self):
        """all_results 中同索引出现多次（不应发生，但字典语义为后者覆盖）"""
        entries = [make_entry(0, "こんにちは")]
        results = {
            0: TranslationResult(index=0, original_text="こんにちは", translated_text="第一版"),
        }
        results[0] = TranslationResult(index=0, original_text="こんにちは", translated_text="第二版")
        merged = Merger().merge(entries, results)
        assert merged[0].text == "第二版"

    def test_indices_out_of_order(self):
        """original_entries 顺序与 index 无关时按列表顺序合并"""
        entries = [make_entry(2, "その三"), make_entry(0, "その一")]
        results = {
            0: TranslationResult(index=0, original_text="その一", translated_text="第一"),
            2: TranslationResult(index=2, original_text="その三", translated_text="第三"),
        }
        merged = Merger().merge(entries, results)
        assert [e.text for e in merged] == ["第三", "第一"]

    def test_multiline_translation_restored_with_tags(self):
        original = "{\\i1}おはよう{\\i0}\\N今日もいい天気"
        entries = [make_entry(0, "おはよう\\N今日もいい天気", original=original)]
        results = {0: TranslationResult(
            index=0, original_text=original,
            translated_text="<TAG_0>早上好<TAG_1>\\N今天天气也不错",
        )}
        merged = Merger().merge(entries, results)
        assert merged[0].text == "{\\i1}早上好{\\i0}\\N今天天气也不错"
