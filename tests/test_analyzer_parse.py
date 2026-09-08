"""SubtitleAnalyzer.parse_analysis_response 契约测试

区分「解析失败」（无 JSON/坏 JSON → 抛 ValueError，由 translator 跳过分析）
与「解析成功但结构为空」（短字幕合法情形 → 返回空 AnalysisResult 走确认面板）。
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.analyzer import SubtitleAnalyzer


class TestParseAnalysisResponse:
    def test_empty_structure_is_valid(self):
        """合法 JSON 但无话题/术语 → 空结果对象（短字幕合法情形），不抛错"""
        an = SubtitleAnalyzer()
        r = an.parse_analysis_response('{"topic_segments": [], "glossary": {}, "style_guide": ""}')
        assert r.topic_segments == []
        assert r.glossary == {}

    def test_garbage_text_raises(self):
        """纯文本无 JSON → ValueError（translator 将跳过分析）"""
        an = SubtitleAnalyzer()
        with pytest.raises(ValueError, match="解析失败"):
            an.parse_analysis_response("我无法完成这个任务。")

    def test_truncated_json_raises(self):
        """JSON 截断 → ValueError 而非静默空对象"""
        an = SubtitleAnalyzer()
        with pytest.raises(ValueError):
            an.parse_analysis_response('{"topic_segments": [{"start_idx": 0, "end')

    def test_valid_full_structure_parsed(self):
        an = SubtitleAnalyzer()
        resp = (
            '{"topic_segments": [{"start_idx": 0, "end_idx": 5, "topic": "开场", "key_terms": ["配信"]}],'
            ' "glossary": {"スパチャ": "SC"}, "style_guide": "口语化"}'
        )
        r = an.parse_analysis_response(resp)
        assert r.topic_segments[0]["topic"] == "开场"
        assert r.glossary == {"スパチャ": "SC"}
        assert r.style_guide == "口语化"

    def test_bad_segment_skipped_good_ones_kept(self):
        """单条坏分段只跳过该条，不废整篇"""
        an = SubtitleAnalyzer()
        resp = (
            '{"topic_segments": ['
            '{"start_idx": 0, "end_idx": 2, "topic": "A"},'
            '{"start_idx": "abc", "end_idx": 5, "topic": "坏"}'
            '], "glossary": {}}'
        )
        r = an.parse_analysis_response(resp)
        assert len(r.topic_segments) == 1
        assert r.topic_segments[0]["topic"] == "A"
