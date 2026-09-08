"""ASS 格式化标签处理"""

import re


# ASS 覆盖标签模式: {\tag}
ASS_TAG_PATTERN = re.compile(r"\{[^{}]*\}")

# SRT 内联标记模式: <i> </i> <b> <u> <font color=...>（大小写不敏感）。
# 标签名后必须紧跟 >、/ 或空白——否则 <italic> 会被当成 <i>+"talic"、
# <br> 被当成 <b>+"r" 整体吞成占位符
SRT_TAG_PATTERN = re.compile(r"<(/?)\s*(i|b|u|font)(?=[\s>/])[^>]*>", re.IGNORECASE)

# 绘图块检测：任意标签块里含 \p1~\p9 即视为绘图（ASS 允许 \pN 与 \bord\pos 等
# 复合在同一个 {…} 块里，如 {\p1\bord0}、{\pos(10,20)\p1}）
DRAWING_PATTERN = re.compile(r"\{[^{}]*\\p[1-9]\d*[^{}]*\}")

# 纯音符/音乐标记行（去掉标签后只剩这些符号则跳过翻译）
MUSIC_ONLY_RE = re.compile(r"^[♪♫♬♩🎵🎶〜～~…·.．\s（）()\[\]【】]*$")


class ASSTagHandler:
    """ASS 标签的提取与还原"""

    def extract_tags(self, text: str) -> tuple[str, dict[str, str]]:
        """
        提取 ASS/SRT 标签，替换为占位符。
        返回: (带占位符的文本, {占位符: 原标签})
        """
        tag_map: dict[str, str] = {}
        tag_counter = 0

        def replacer(m: re.Match) -> str:
            nonlocal tag_counter
            tag_text = m.group(0)
            placeholder = f"<TAG_{tag_counter}>"
            tag_map[placeholder] = tag_text
            tag_counter += 1
            return placeholder

        # 先处理 ASS 覆盖标签，再处理 SRT 内联标记（顺序无关紧要，占位符互不冲突）
        cleaned = ASS_TAG_PATTERN.sub(replacer, text)
        cleaned = SRT_TAG_PATTERN.sub(replacer, cleaned)
        return cleaned, tag_map

    def restore_tags(self, text: str, tag_map: dict[str, str]) -> str:
        """将占位符还原为 ASS 标签"""
        restored = text
        for placeholder, original_tag in tag_map.items():
            restored = restored.replace(placeholder, original_tag)
        return restored

    def has_drawing(self, text: str) -> bool:
        r"""检测是否包含绘图块 {\p1}...{\p0}"""
        return bool(DRAWING_PATTERN.search(text))

    def is_skip_entry(self, text: str) -> bool:
        """判断是否应跳过翻译的条目（绘图、音符、空行等）"""
        if not text or not text.strip():
            return True
        if self.has_drawing(text):
            return True
        # 去掉 ASS 与 SRT 两种标签后：为空、或只剩音符/括号符号。
        # 只剥 ASS 的话 SRT 的 <i>♪</i> 剥不掉字母 i，会被误判为可翻译
        stripped = ASS_TAG_PATTERN.sub("", text)
        stripped = SRT_TAG_PATTERN.sub("", stripped).strip()
        if not stripped:
            return True
        if MUSIC_ONLY_RE.match(stripped):
            return True
        return False
