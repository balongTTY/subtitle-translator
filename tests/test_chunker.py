"""chunker 分块回归测试。

覆盖：
- 空输入 / 单批（预算内）
- 多批 + overlap 上下文（primary_start/end 正确）
- max_entries 条目数硬上限（短句字幕不被单批无限累积）
- 单条超 max_tokens 独占一批
- 时间间隔断点（SCENE_BREAK_GAP_MS）
- 话题上下文 / 被跳过的条目索引映射
- _get_config 受 context_limit 约束
- estimated_tokens 计入 overlap 与提示词固定开销

为确定性测试，用 stub 替换 AppSettings 与 estimate_tokens（按 len 估算），
不触碰真实设置与网络。
"""

import core.chunker as chunker
from core.chunker import ChunkConfig, SubtitleChunker
from core.translation_batch import AnalysisResult, SubtitleEntry


class _StubSettings:
    context_limit = 64000
    chunk_tokens = 2000
    overlap_entries = 5
    model = "gpt-4o"


def _make_entries(n: int, per_tokens: int = 100):
    """每条文本长度为 per_tokens（配合按 len 估算 token 的 stub）"""
    return [
        SubtitleEntry(
            index=i,
            start=float(i * 1000),
            end=float(i * 1000 + 500),
            text="x" * per_tokens,
            original_text="x" * per_tokens,
        )
        for i in range(n)
    ]


def _make_chunker(monkeypatch, config: ChunkConfig | None = None) -> SubtitleChunker:
    monkeypatch.setattr(chunker, "AppSettings", lambda: _StubSettings())
    monkeypatch.setattr(chunker, "estimate_tokens", lambda text, model: max(1, len(text)))
    ch = SubtitleChunker()
    if config is not None:
        monkeypatch.setattr(ch, "_get_config", lambda: config)
    return ch


def test_chunk_empty(monkeypatch):
    ch = _make_chunker(monkeypatch)
    assert ch.chunk([]) == []


def test_chunk_single_batch_within_budget(monkeypatch):
    """10 条 * 100 token = 1000 < target 2000 → 单批，无 overlap"""
    ch = _make_chunker(monkeypatch)
    entries = _make_entries(10, per_tokens=100)
    batches = ch.chunk(entries)
    assert len(batches) == 1
    tb = batches[0]
    assert tb.primary_start_idx == 0
    assert tb.primary_end_idx == 10
    assert [e.index for e in tb.entries] == list(range(10))


def test_chunk_multiple_batches_with_overlap(monkeypatch):
    """50 条 * 100 token：每批 20 条（2000 = target）→ 3 批，第 2/3 批带 overlap"""
    ch = _make_chunker(monkeypatch)
    entries = _make_entries(50, per_tokens=100)
    batches = ch.chunk(entries)
    assert len(batches) == 3

    b1 = batches[0]
    assert b1.primary_start_idx == 0 and b1.primary_end_idx == 20
    assert [e.index for e in b1.entries] == list(range(0, 20))

    # 批 2：context 15..20 + primary 20..40
    b2 = batches[1]
    assert b2.primary_start_idx == 5 and b2.primary_end_idx == 25
    assert [e.index for e in b2.entries] == list(range(15, 40))

    # 批 3：context 35..40 + primary 40..50
    b3 = batches[2]
    assert b3.primary_start_idx == 5 and b3.primary_end_idx == 15
    assert [e.index for e in b3.entries] == list(range(35, 50))


def test_chunk_estimated_tokens_includes_overlap_and_overhead(monkeypatch):
    """estimated_tokens = 主条目 + overlap 上下文 + 按语言实测的提示词开销"""
    ch = _make_chunker(monkeypatch)
    entries = _make_entries(50, per_tokens=100)
    batches = ch.chunk(entries)
    b2 = batches[1]
    assert b2.estimated_tokens == 20 * 100 + 5 * 100 + chunker.prompt_overhead_tokens("ja")


def test_chunk_max_entries_cap(monkeypatch):
    """每条 1 token 不会触发 token 断点 → 靠 max_entries=30 硬上限分 4 批"""
    ch = _make_chunker(monkeypatch)
    entries = _make_entries(100, per_tokens=1)
    batches = ch.chunk(entries)
    assert len(batches) == 4
    for tb in batches:
        assert tb.primary_end_idx - tb.primary_start_idx <= 30
    assert batches[0].primary_end_idx - batches[0].primary_start_idx == 30
    assert batches[-1].primary_end_idx - batches[-1].primary_start_idx == 10


def test_chunk_oversized_single_entry(monkeypatch):
    """单条 5000 token > max 3000 → 独占一批继续（提示但不停摆）"""
    ch = _make_chunker(monkeypatch)
    entries = [
        SubtitleEntry(index=0, start=0.0, end=1000.0, text="y" * 5000, original_text="y" * 5000)
    ]
    batches = ch.chunk(entries)
    assert len(batches) == 1
    tb = batches[0]
    assert [e.index for e in tb.entries] == [0]
    assert tb.primary_end_idx - tb.primary_start_idx == 1


def test_chunk_gap_break(monkeypatch):
    """接近 target 时在 ≥5000ms 的时间间隔处断开（e19→e20 场景切换）"""
    ch = _make_chunker(monkeypatch)
    entries = _make_entries(50, per_tokens=100)
    # 把 e20 起点推到 65000ms，使 e19.end(19500) 与 e20.start 之间 gap >= 5000
    entries[20] = SubtitleEntry(index=20, start=65000.0, end=65500.0,
                                text="x" * 100, original_text="x" * 100)
    batches = ch.chunk(entries)
    b1 = batches[0]
    assert [e.index for e in b1.entries] == list(range(0, 20))
    assert b1.primary_end_idx - b1.primary_start_idx == 20
    # 批 2 从 e20 开始
    assert batches[1].entries[batches[1].primary_start_idx].index == 20


def test_chunk_topic_context(monkeypatch):
    """话题上下文按批次起点取最近的前置分段"""
    ch = _make_chunker(monkeypatch)
    entries = _make_entries(50, per_tokens=100)
    analysis = AnalysisResult(topic_segments=[
        {"start_idx": 0, "topic": "开场寒暄"},
        {"start_idx": 20, "topic": "游戏实况"},
        {"start_idx": 40, "topic": "下播"},
    ])
    batches = ch.chunk(entries, analysis)
    assert len(batches) == 3
    assert batches[0].topic_context == "开场寒暄"
    assert batches[1].topic_context == "游戏实况"
    assert batches[2].topic_context == "下播"


def test_build_topic_index_maps_original_to_filtered(monkeypatch):
    """被跳过条目（绘图/音效）后，话题分段按 原始索引→过滤后位置 对齐"""
    ch = _make_chunker(monkeypatch)
    entries = [
        SubtitleEntry(index=i, start=0.0, end=1000.0, text="x", original_text="x")
        for i in (0, 1, 3, 4)
    ]
    index_to_pos = {e.index: i for i, e in enumerate(entries)}
    analysis = AnalysisResult(topic_segments=[
        {"start_idx": 3, "topic": "片段A"},
        {"start_idx": 0, "topic": "开头"},
    ])
    idx = ch._build_topic_index(analysis, len(entries), index_to_pos)
    assert idx == {0: "开头", 2: "片段A"}


def test_get_topic_for_range_picks_last_before_start(monkeypatch):
    ch = _make_chunker(monkeypatch)
    topic_index = [(0, "A"), (10, "B"), (20, "C")]
    assert ch._get_topic_for_range(topic_index, 0) == "A"
    assert ch._get_topic_for_range(topic_index, 12) == "B"
    assert ch._get_topic_for_range(topic_index, 30) == "C"


def test_get_config_respects_context_limit(monkeypatch):
    """max_tokens = min(3000, max(ctx//2, 500))：小上下文窗口时收缩硬上限"""
    class SmallCtx:
        context_limit = 1000
        chunk_tokens = 2000
        overlap_entries = 5
        model = "gpt-4o"

    monkeypatch.setattr(chunker, "AppSettings", lambda: SmallCtx())
    ch = SubtitleChunker()
    cfg = ch._get_config()
    assert cfg.max_tokens == 500
    assert cfg.target_tokens == 2000
    assert cfg.overlap_entries == 5
