"""智能字幕分块器

将字幕条目按 token 预算分批，利用分析结果和 overlap 确保翻译质量。
"""

import logging
from dataclasses import dataclass

from config.settings import AppSettings
from core.translation_batch import SubtitleEntry, TranslationBatch, AnalysisResult
from services.prompt_builder import prompt_overhead_tokens
from utils.token_counter import estimate_tokens
from utils.constants import SCENE_BREAK_GAP_MS

log = logging.getLogger("subtitle_translator")


@dataclass
class ChunkConfig:
    target_tokens: int = 2000
    max_tokens: int = 3000
    overlap_entries: int = 5
    min_entries: int = 3
    max_entries: int = 30
    break_on_gap_ms: int = SCENE_BREAK_GAP_MS


class SubtitleChunker:
    """智能分块器"""

    def __init__(self) -> None:
        self.settings = AppSettings()

    def _get_config(self) -> ChunkConfig:
        # 单批硬上限：默认 3000，但不超过上下文窗口的一半
        # （剩余空间留给系统提示词 / 术语表 / overlap 上下文）
        ctx = self.settings.context_limit
        max_tokens = min(3000, max(ctx // 2, 500))
        return ChunkConfig(
            target_tokens=self.settings.chunk_tokens,
            max_tokens=max_tokens,
            overlap_entries=self.settings.overlap_entries,
            break_on_gap_ms=SCENE_BREAK_GAP_MS,
        )

    def chunk(
        self,
        entries: list[SubtitleEntry],
        analysis: AnalysisResult | None = None,
        source_lang: str = "ja",
    ) -> list[TranslationBatch]:
        """将字幕条目划分为多个批次（source_lang 用于实测提示词开销记账）"""
        config = self._get_config()
        model = self.settings.model
        batches: list[TranslationBatch] = []

        if not entries:
            return batches

        # 全篇分析的术语表（分析失败时为空，强制术语由 prompt_builder
        # 从语料库注入——chunk 层不做渐进式积累）
        global_glossary: dict[str, str] = {}
        if analysis and analysis.glossary:
            global_glossary.update(analysis.glossary)

        # 根据分析结果建立话题索引。
        # 注意：跳过条目（绘图/音效）后 entries 里位置 ≠ 原始索引，
        # 需用「原始索引 → 位置」映射对齐话题分段。
        index_to_pos = {e.index: i for i, e in enumerate(entries)}
        topic_index = self._build_topic_index(analysis, len(entries), index_to_pos)
        # 话题索引按起点升序排序一次，循环内复用（避免每个批次重复全量排序）
        topic_index_sorted = sorted(topic_index.items())

        current_idx = 0
        batch_id = 0

        while current_idx < len(entries):
            start_idx = current_idx
            # 确定本批的上下文起始（overlap 回退）
            context_start = max(0, start_idx - config.overlap_entries)

            batch_entries: list[SubtitleEntry] = list(
                entries[context_start:start_idx]
            )
            batch = [entries[start_idx]]
            # 只跟踪主条目 token，overlap 的不计入预算
            current_token_estimate = estimate_tokens(entries[start_idx].text, model)
            if current_token_estimate > config.max_tokens:
                # 单条字幕自身就超出硬上限 → 会独占一批且超出上下文窗口，提示但继续
                log.warning(
                    "条目 #%d 超出单批 token 上限（%d > %d），将单独成批，"
                    "可能超出模型上下文窗口，建议拆分源字幕",
                    entries[start_idx].index,
                    current_token_estimate,
                    config.max_tokens,
                )

            for i in range(start_idx + 1, len(entries)):
                if len(batch) >= config.max_entries:
                    # 条目数硬上限：防止短句字幕单批积累上千条（此前 max_entries 从未生效）
                    current_idx = start_idx + len(batch)
                    break
                entry = entries[i]
                entry_tokens = estimate_tokens(entry.text, model)

                if current_token_estimate + entry_tokens > config.max_tokens:
                    # 超过单批硬上限，强制结束（确保提示词不超上下文窗口）
                    current_idx = start_idx + len(batch)
                    break

                if (
                    current_token_estimate + entry_tokens > config.target_tokens
                    and len(batch) >= config.min_entries
                ):
                    # 尝试在时间间隔处断点
                    break_found = False
                    look_back = min(5, len(batch))
                    for j in range(len(batch) - 1, max(0, len(batch) - look_back), -1):
                        # j ≤ len(batch)-1，start_idx+j+1 ≤ 当前条目 i < len(entries) 必然有效
                        next_start = entries[start_idx + j + 1].start
                        gap = next_start - entries[start_idx + j].end
                        if gap >= config.break_on_gap_ms:
                            # 在这里断开
                            batch = batch[: j + 1]
                            # 重新统计实际保留条目的 token（被裁掉的条目不计入限流配额）
                            current_token_estimate = sum(
                                estimate_tokens(e.text, model) for e in batch
                            )
                            current_idx = start_idx + j + 1
                            break_found = True
                            break

                    if not break_found:
                        current_idx = start_idx + len(batch)
                    break

                batch.append(entry)
                current_token_estimate += entry_tokens

            else:
                current_idx = len(entries)

            # 把上下文条目加进去
            all_entries = batch_entries + batch
            primary_start = len(batch_entries)
            primary_end = len(all_entries)

            # 获取当前话题上下文
            topic_context = self._get_topic_for_range(topic_index_sorted, start_idx)

            # 真实请求量 = 主条目 + overlap 上下文条目 + 按语言实测的提示词固定开销
            # （限流器按此扣配额，避免低估导致超发）
            total_estimate = current_token_estimate
            for ctx_entry in batch_entries:
                total_estimate += estimate_tokens(ctx_entry.text, model)
            total_estimate += prompt_overhead_tokens(source_lang)

            tb = TranslationBatch(
                batch_id=batch_id,
                entries=[
                    SubtitleEntry(
                        index=e.index,
                        start=e.start,
                        end=e.end,
                        text=e.text,
                        original_text=e.original_text,
                        style=e.style,
                    )
                    for e in all_entries
                ],
                primary_start_idx=primary_start,
                primary_end_idx=primary_end,
                is_overlap_only=False,
                glossary=dict(global_glossary),  # 各批独立副本，避免共享可变引用
                topic_context=topic_context,
                estimated_tokens=total_estimate,
            )
            batches.append(tb)
            batch_id += 1

        return batches

    def _build_topic_index(
        self,
        analysis: AnalysisResult | None,
        total_entries: int,
        index_to_pos: dict[int, int],
    ) -> dict[int, str]:
        """将分析结果中的话题分段映射为索引→话题的字典（按过滤后位置）"""
        index: dict[int, str] = {}
        if not analysis or not analysis.topic_segments:
            return index
        for seg in analysis.topic_segments:
            start = seg.get("start_idx", 0)
            topic = seg.get("topic", "")
            if topic:
                # 原始索引 → 过滤后位置；LLM 给出的越界/负数起点钳到有效范围
                pos = index_to_pos.get(start, start)
                pos = min(max(pos, 0), total_entries - 1)
                index[pos] = topic
        return index

    @staticmethod
    def _get_topic_for_range(topic_index: list[tuple[int, str]], start: int) -> str:
        """获取某索引处的话题描述（取起点不超过 start 的最后一个话题）。

        topic_index 需为按起点升序排序的 (seg_start, topic) 列表
        （由 chunk() 排序一次后复用，避免每批重复全量 sorted）。
        """
        relevant = ""
        for seg_start, topic in topic_index:
            if seg_start <= start:
                relevant = topic
            else:
                break
        return relevant
