"""翻译质检 + 自动重翻"""

import logging
import re

from config.settings import AppSettings
from core.translation_batch import SubtitleEntry, TranslationResult

log = logging.getLogger("subtitle_translator")

# 日文假名范围
KANA_RE = re.compile(r"[ぁ-ゟ゠-ヿ]")
# 平假名——残留检测只看平假名（片假名常是有意保留的专有名词/外来语）
HIRAGANA_RE = re.compile(r"[ぁ-ん]")


class QCChecker:
    """质检器"""

    def __init__(self) -> None:
        self.settings = AppSettings()

    def check(
        self,
        original_entries: list[SubtitleEntry],
        translated_texts: list[str],
        glossary: dict[str, str] | None = None,
    ) -> list[TranslationResult]:
        results: list[TranslationResult] = []

        if len(original_entries) != len(translated_texts):
            # 公共 API 防御：zip 会静默截断，多出的译文被无声丢弃、统计失真
            log.warning(
                "质检输入长度不一致：%d 条原文 vs %d 条译文（按短侧截断）",
                len(original_entries), len(translated_texts),
            )
        for orig, trans in zip(original_entries, translated_texts):
            entry_issues = self._check_single(orig, trans, glossary)
            status = "qc_passed" if not entry_issues else "qc_failed"
            results.append(
                TranslationResult(
                    index=orig.index,
                    original_text=orig.text,
                    translated_text=trans,
                    status=status,
                    qc_issues=entry_issues,
                )
            )

        passed = sum(1 for r in results if r.status == "qc_passed")
        failed = len(results) - passed
        log.info("质检: %d/%d 通过, %d 条问题", passed, len(results), failed)
        if failed:
            issues_sample = []
            for r in results:
                if r.qc_issues:
                    issues_sample.append(f"#{r.index}: {', '.join(r.qc_issues[:3])}")
            log.warning("质检问题: %s", " | ".join(issues_sample[:10]))

        return results

    def _check_single(
        self,
        original: SubtitleEntry,
        translated: str,
        glossary: dict[str, str] | None = None,
    ) -> list[str]:
        issues: list[str] = []

        stripped = translated.strip() if translated else ""

        # ---- 空译文 ----
        if not stripped:
            issues.append("译文为空")
            return issues

        # ---- 占位符完整性 ----
        orig_tags = set(re.findall(r"<TAG_\d+>", original.text))
        trans_tags = set(re.findall(r"<TAG_\d+>", stripped))
        missing = orig_tags - trans_tags
        extra = trans_tags - orig_tags
        if missing:
            issues.append(f"缺占位符: {', '.join(sorted(missing))}")
        if extra:
            issues.append(f"多余占位符: {', '.join(sorted(extra))}")

        # ---- 日文泄漏检测（平假名残留，片假名多为有意保留的专有名词） ----
        orig_kana = HIRAGANA_RE.findall(original.text)
        if orig_kana:
            trans_kana = HIRAGANA_RE.findall(stripped)
            # 绝对数量 ≥3 才做比例判定：原文只有 2 个假名、译文合理保留 1 个
            # （引用/人名）时 50% 的比例是天然误报
            if trans_kana and len(trans_kana) >= 3:
                leak_pct = len(trans_kana) / max(len(orig_kana), 1)
                if leak_pct > 0.3:
                    issues.append(f"日文残留 {leak_pct:.0%} ({''.join(trans_kana[:8])}…)")

        # ---- 原文未翻译（译文=原文） ----
        if stripped == original.text.strip():
            # 判定内容时剔除占位符：<TAG_0> 里的 "TAG" 会命中 [a-zA-Z]{3,}，
            # 把符号/数字类条目的原样保留误判成「未翻译」触发无谓重翻
            content = re.sub(r"<TAG_\d+>", "", original.text)
            if KANA_RE.search(content) or re.search(r"[a-zA-Z]{3,}", content):
                issues.append("译文与原文完全相同")
            elif not re.search(r"[A-Za-z一-鿿]", content):
                pass  # 纯符号/数字（888、♪♪ 等）→ 允许原样保留，不告警
            elif len(content.strip()) <= 4:
                # 真正的短词（GG、w、草 等）→ 非严重提示，请人工确认
                issues.append("译文与原文相同（短词，请人工确认）")
            else:
                # 纯汉字长句（正式台词、屏显文字，无假名无长拉丁串）——
                # 同样基本确定未翻译，必须按严重处理，否则整批静默放行
                issues.append("译文与原文完全相同")

        # ---- 译文过短 ----
        if len(stripped) <= 1 and len(original.text.strip()) > 3:
            issues.append("译文过短")

        # ---- 术语一致性（有 glossary 时检查） ----
        # 原文出现强制术语但译文未出现对应译法 → 提示（非严重，避免误伤合理改写）
        if glossary:
            for src_term, tgt_term in glossary.items():
                if not (src_term and tgt_term and src_term in original.text):
                    continue
                # 历史/预设数据 value 可能带「/」「、」备选（如 来真的/认真）——
                # 字面全串匹配永远不在正常译文里 → 必然误报，回灌还会诱导
                # 模型把斜杠字面量写进字幕。任一备选命中即算使用了
                candidates = [c.strip() for c in re.split(r"[/、]", tgt_term) if c.strip()]
                hit = tgt_term in stripped or any(c in stripped for c in candidates)
                if not hit:
                    issues.append(f"术语未使用: {src_term}→{tgt_term}")
                    break

        return issues

    def is_batch_passable(self, results: list[TranslationResult]) -> bool:
        """批次是否通过——严重问题（空译文/原文未翻译/占位符缺失或多余）不超过 20% 且无条目为空。

        「多余占位符」也是严重问题：QC 放行后 merger 无法还原多余占位符会
        静默回退原文——用户在 UI 上看到的却是质检通过（与缺占位符对称处理）。
        """
        if any(not r.translated_text or not r.translated_text.strip() for r in results):
            return False
        severe = sum(
            1 for r in results
            if any(
                iss.startswith("译文为空")
                or iss.startswith("译文与原文完全相同")
                or iss.startswith("缺占位符")
                or iss.startswith("多余占位符")
                for iss in r.qc_issues
            )
        )
        return severe <= len(results) * 0.2
