"""翻译数据类"""

from dataclasses import dataclass, field


@dataclass
class SubtitleEntry:
    """单条字幕条目（内部表示）"""
    index: int
    start: float       # 开始时间（毫秒）
    end: float         # 结束时间（毫秒）
    text: str          # 原文（已做标签处理）
    original_text: str # 原始文本（含标签）
    style: str = ""    # ASS 样式名


@dataclass
class AnalysisResult:
    """全篇分析结果（阶段一输出）"""
    # 话题分段: [{"start_idx": 0, "end_idx": 150, "topic": "...", "key_terms": [...]}]
    topic_segments: list[dict] = field(default_factory=list)
    # 说话人特征: [{"speaker": "...", "tone": "...", "speech_patterns": [...]}]
    speakers: list[dict] = field(default_factory=list)
    # 全局术语表: {"日文": "中文", ...}
    glossary: dict[str, str] = field(default_factory=dict)
    # 翻译风格指南
    style_guide: str = ""
    # 原始 LLM 响应（用于调试）
    raw_response: str = ""


@dataclass
class TranslationBatch:
    """翻译批次"""
    batch_id: int
    entries: list[SubtitleEntry]
    primary_start_idx: int       # 新条目起始索引
    primary_end_idx: int         # 新条目结束索引
    is_overlap_only: bool = False
    glossary: dict[str, str] = field(default_factory=dict)
    topic_context: str = ""      # 当前时段话题描述
    estimated_tokens: int = 0


@dataclass
class TranslationResult:
    """单条翻译结果"""
    index: int
    original_text: str
    translated_text: str
    status: str = "pending"      # pending | translated | qc_passed | qc_failed | needs_review
    qc_issues: list[str] = field(default_factory=list)
