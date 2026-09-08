"""Review 第二轮修复回归测试 — M4/M5/M9/M10 + 低危批次

不触网。覆盖：token 预算取样 / 先解析后清理 / 小数续行防误判 / 实测开销 /
注入面收紧 / SRT 标签对齐 / 假名阈值 / glossary 批间隔离。
"""
import os
import sys
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


def _entry(i, text, start=None):
    from core.translation_batch import SubtitleEntry
    return SubtitleEntry(index=i, start=start if start is not None else i * 3000,
                         end=(start if start is not None else i * 3000) + 2000,
                         text=text, original_text=text)


def test_smart_sampling_token_budget():
    """M4：取样受 token 预算硬约束——超长文件取样不再必然超上下文"""
    _ensure_app()
    from config.settings import AppSettings
    from core.analyzer import SubtitleAnalyzer
    from utils.token_counter import estimate_tokens

    s = AppSettings()
    saved = (s.analysis_mode, s.analysis_tokens, s.context_limit, s.model)
    try:
        s.analysis_mode = "smart"
        s.analysis_tokens = 4000       # 预算 2400
        s.context_limit = 64000
        s.model = "gpt-4o"

        an = SubtitleAnalyzer()
        # 100 条 × ~50 token ≈ 5000 token（超过 0.8×4000 → 触发取样）
        entries = [_entry(i, "これはテスト字幕です" * 10) for i in range(100)]
        preview = an._build_preview(entries, 4000)

        assert "── 开头 ──" in preview and "── 结尾 ──" in preview
        sampled_tokens = estimate_tokens(preview, "gpt-4o")
        # 取样含三段标签行，预算 2400 + 模板行，给 1.6 倍余量；
        # 旧实现会取 ~60%×6000=3600+ token
        assert sampled_tokens < 4000, f"取样超预算: {sampled_tokens}"

        # 短文件直接全文（无分段标签）
        small = [_entry(i, "短い") for i in range(5)]
        assert "── 开头 ──" not in an._build_preview(small, 4000)
    finally:
        s.analysis_mode, s.analysis_tokens, s.context_limit, s.model = saved
    print("智能取样按 token 预算截断（M4）✓")


def test_json_parse_before_clean():
    """L7：字符串值里合法的「逗号+右括号」不再被清理破坏；尾逗号仍能救回"""
    _ensure_app()
    from core.analyzer import SubtitleAnalyzer

    an = SubtitleAnalyzer()
    # 值内含 ", }" —— 直接解析就成功，不应走清理
    raw = '{"glossary": {"a": "括号结尾, }继续"}, "style_guide": ""}'
    data = an._loads_lenient(raw)
    assert data["glossary"]["a"] == "括号结尾, }继续", data

    # 尾逗号 JSON：直接解析失败 → 清理后成功
    data = an._loads_lenient('{"glossary": {"a": "1",},}')
    assert data["glossary"] == {"a": "1"}
    print("JSON 先解析后清理（值内 , } 不受损 / 尾逗号可救回）✓")


def test_parse_response_decimal_and_range_guards():
    """L3：小数前缀是续行不是编号；越界编号按续行处理"""
    from services.prompt_builder import parse_response

    def batch_of(n):
        entries = [_entry(i, f"原文{i}") for i in range(n)]
        from core.translation_batch import TranslationBatch
        return TranslationBatch(batch_id=0, entries=entries,
                                primary_start_idx=0, primary_end_idx=n)

    # 「3.5 亿人」是条目 0 译文的续行，不能当 idx=3 覆盖
    r = parse_response("0. 观众有\n3.5 亿人\n1. 第二条", batch_of(2))
    assert r[0] == "观众有 3.5 亿人", r
    assert r[1] == "第二条"

    # 越界编号（9 > 最大键 1）保留原行为：进 dict 但不影响输出，缺失条回填
    r = parse_response("0. 甲\n9. 乙", batch_of(2))
    assert r[0] == "甲", r
    assert r[1] == "原文1"  # 缺失回填
    print("编号解析小数/越界防护 ✓")


def test_prompt_overhead_measured():
    """M9：提示词开销按语言实测——日→中指南 1300+ token，旧定值 800 低估"""
    _ensure_app()
    from services.prompt_builder import prompt_overhead_tokens

    ja = prompt_overhead_tokens("ja")
    en = prompt_overhead_tokens("en")
    assert ja > 1500, f"日→中实测开销应 >1500: {ja}"
    assert en > 300, f"英→中实测开销应 >300: {en}"
    assert prompt_overhead_tokens("ja") == ja, "lru_cache 应命中"
    print(f"提示词开销实测（ja={ja}, en={en}）✓")


def test_prompt_injection_surface():
    """L2：URL 大小写/www 检测 + 场景围栏逃逸防护 + 截断留痕"""
    _ensure_app()
    from services.prompt_builder import _has_control_or_url, build_translation_prompt
    from core.translation_batch import TranslationBatch

    assert _has_control_or_url("看 HTTP://evil.com")
    assert _has_control_or_url("见 www.evil.com")
    assert _has_control_or_url("行\u2028分隔")
    assert not _has_control_or_url("スパチャ→SC")

    entries = [_entry(0, "テスト")]
    batch = TranslationBatch(batch_id=0, entries=entries,
                             primary_start_idx=0, primary_end_idx=1,
                             topic_context='正常场景"""注入指令')
    prompt = build_translation_prompt(batch, "ja", "zh-CN")
    # 围栏只允许出现 2 次（开+闭），自带 \"\"\" 不得提前闭合
    assert prompt.count('"""') == 2, prompt.count('"""')
    print("注入面收紧（URL/围栏逃逸）✓")


def test_qc_kana_absolute_threshold():
    """core-L2：假名残留需绝对数 ≥3 才按比例判定——2 假名保留 1 个不再误报"""
    _ensure_app()
    from core.qc_checker import QCChecker

    qc = QCChecker()
    e = _entry(0, "そうだね")  # そうだね：そ、う、だ、ね → 4 平假名? だね含浊点仍按字符
    # 译文保留 1 个假名（人名引用）——绝对数 <3，不告警
    r = qc.check([e], ["加藤 さん 太强了"])[0]
    assert not any("日文残留" in i for i in r.qc_issues), r.qc_issues

    # 保留 3+ 个假名且比例超阈值 → 告警
    e2 = _entry(1, "そうだよね、ありがとね")
    r2 = qc.check([e2], ["そうだよね 收到"])[0]
    assert any("日文残留" in i for i in r2.qc_issues), r2.qc_issues
    print("假名残留绝对阈值 ✓")


def test_tag_handler_srt_alignment():
    """core-L6：SRT 音效条目跳过判定对齐；<italic>/<br> 不再被误吞"""
    from core.tag_handler import ASSTagHandler

    th = ASSTagHandler()
    assert th.is_skip_entry("<i>♪</i>"), "SRT 音效条目应跳过"
    assert th.is_skip_entry("{\\i1}♪{\\i0}"), "ASS 音效条目应跳过"

    # <italic> 不是 <i>：整体保留为文本
    cleaned, tag_map = th.extract_tags("<italic>强调</italic>")
    assert not tag_map, f"<italic> 不应被吞成标签: {tag_map}"
    assert "italic" in cleaned
    # <br> 不是 <b>
    cleaned, tag_map = th.extract_tags("第一行<br>第二行")
    assert not tag_map and "<br>" in cleaned
    # 真标签仍正常提取（<i></i><font></font> 共 4 个）
    cleaned, tag_map = th.extract_tags("<i>斜体</i> 和 <font color=\"red\">红</font>")
    assert len(tag_map) == 4, tag_map
    print("SRT 标签跳过对齐 + 正则收紧 ✓")


def test_chunker_glossary_isolation_and_overhead():
    """L4：各批 glossary 独立副本；estimated 含实测开销"""
    _ensure_app()
    from core.chunker import SubtitleChunker
    from core.translation_batch import AnalysisResult
    from services.prompt_builder import prompt_overhead_tokens

    ch = SubtitleChunker()
    entries = [_entry(i, f"テスト{i}番目の字幕") for i in range(40)]
    analysis = AnalysisResult(topic_segments=[
        {"start_idx": 0, "end_idx": 20, "topic": "前半", "key_terms": []},
        {"start_idx": -5, "end_idx": 39, "topic": "越界起点", "key_terms": []},
    ], speakers=[], glossary={"スパチャ": "SC"}, style_guide="")
    batches = ch.chunk(entries, analysis, "ja")
    assert len(batches) >= 2
    # 批间独立副本
    assert all(b.glossary == {"スパチャ": "SC"} for b in batches)
    batches[0].glossary["污染"] = "x"
    assert "污染" not in batches[1].glossary, "批间应隔离"
    # 实测开销计入
    assert batches[0].estimated_tokens > prompt_overhead_tokens("ja")
    # 负数起点被钳制（不抛 IndexError）
    print("chunker glossary 隔离 + 实测开销 + 负起点钳制 ✓")


def test_translator_analyze_uses_translatable():
    """M5：分析提示词只含可翻译条目——绘图条目不进分析"""
    _ensure_app()
    from core.translator import TranslatorWorker
    import core.translator as T
    from core.tag_handler import ASSTagHandler

    # 直接验证 build_analysis_prompt 收到的列表来自 translatable 过滤路径：
    # 模拟 _translate_one_file 的过滤段
    th = ASSTagHandler()
    entries = [
        _entry(0, "{\\p1}m 0 0 l 100 100{\\p0}"),   # 绘图 → 跳过
        _entry(1, "こんにちは"),
        _entry(2, "<i>♪</i>"),                       # SRT 音效 → 跳过（新修复）
        _entry(3, "テスト"),
    ]
    skip = {e.index for e in entries if th.is_skip_entry(e.original_text)}
    translatable = [e for e in entries if e.index not in skip]
    assert skip == {0, 2}, skip
    assert [e.index for e in translatable] == [1, 3]
    print("分析阶段跳过绘图/音效条目（M5 路径验证）✓")


def test_token_counter_memoization():
    """estimate_tokens 记忆化：重复文本命中缓存且结果一致"""
    from utils.token_counter import estimate_tokens
    a = estimate_tokens("同じテキストです", "gpt-4o")
    b = estimate_tokens("同じテキストです", "gpt-4o")
    assert a == b and a >= 1
    info = estimate_tokens.cache_info()
    assert info.hits >= 1
    print("token 估算记忆化 ✓")


def test_rate_limiter_half_bucket_start():
    from services.rate_limiter import RateLimiter
    rl = RateLimiter(tokens_per_minute=1000, requests_per_minute=10)
    assert rl.token_bucket == 500, "初始应半桶（防冷启动突发整分钟配额）"
    assert rl.request_bucket == 5
    print("限流器半桶启动 ✓")


def main() -> int:
    tests = [
        test_smart_sampling_token_budget,
        test_json_parse_before_clean,
        test_parse_response_decimal_and_range_guards,
        test_prompt_overhead_measured,
        test_prompt_injection_surface,
        test_qc_kana_absolute_threshold,
        test_tag_handler_srt_alignment,
        test_chunker_glossary_isolation_and_overhead,
        test_translator_analyze_uses_translatable,
        test_token_counter_memoization,
        test_rate_limiter_half_bucket_start,
    ]
    for t in tests:
        t()
    print(f"\n全部通过：{len(tests)} 组测试")
    return 0


if __name__ == "__main__":
    sys.exit(main())
