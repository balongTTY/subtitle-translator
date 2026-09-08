"""翻译合并器

将分批翻译的结果合并回完整的字幕结构。
"""

from core.translation_batch import SubtitleEntry, TranslationResult
from core.tag_handler import ASSTagHandler

import re


class Merger:
    """合并翻译结果"""

    def __init__(self) -> None:
        self.tag_handler = ASSTagHandler()

    def merge(
        self,
        original_entries: list[SubtitleEntry],
        all_results: dict[int, TranslationResult],
    ) -> list[SubtitleEntry]:
        """
        将翻译结果合并回字幕条目。

        参数:
            original_entries: 原始字幕条目（含标签）
            all_results: {条目索引: TranslationResult} 字典

        返回:
            翻译后的 SubtitleEntry 列表
        """
        merged: list[SubtitleEntry] = []

        for entry in original_entries:
            if entry.index in all_results:
                result = all_results[entry.index]
                translated_text = result.translated_text
            else:
                translated_text = entry.text

            # 将占位符还原为 ASS 标签
            # 从原文本中提取标签映射
            _, tag_map = self.tag_handler.extract_tags(entry.original_text)
            final_text = self.tag_handler.restore_tags(translated_text, tag_map)
            # 占位符未还原（LLM 弄丢了标签）→ 退回原文，避免输出字面 <TAG_0>；
            # 同时把该条标记为需审核——否则 UI 显示质检通过、成品却是原文，
            # 用户对丢译文毫无感知
            # 大小写不敏感：LLM 可能输出 <tag_0>/<Tag_0> 等变体
            if re.search(r"<TAG_\d+>", final_text, re.IGNORECASE):
                final_text = entry.original_text
                result = all_results.get(entry.index)
                if result is not None:
                    result.status = "needs_review"
                    issues = list(result.qc_issues or [])
                    if "标签占位符未还原，已回退原文" not in issues:
                        issues.append("标签占位符未还原，已回退原文")
                    result.qc_issues = issues

            merged.append(
                SubtitleEntry(
                    index=entry.index,
                    start=entry.start,
                    end=entry.end,
                    text=final_text,
                    original_text=entry.original_text,
                    style=entry.style,
                )
            )

        return merged
