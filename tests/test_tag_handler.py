"""tag_handler ASS 标签保护/还原回归测试。

覆盖：
- ASS 覆盖标签 {...} 与 SRT 内联标记 <i>/<b>/<u>/<font> 提取为 <TAG_N> 占位符
- restore_tags 将占位符还原为原始标签
- has_drawing 检测 {\\p1}（含复合块 {\\p1\\bord0}、{\\pos(...)\\p1}）
- is_skip_entry：空行 / 绘图块 / 纯音乐符号 / 仅标签行
"""

from core.tag_handler import ASSTagHandler


def _h() -> ASSTagHandler:
    return ASSTagHandler()


# ---------------------------------------------------------------------------
# 提取 / 还原
# ---------------------------------------------------------------------------

def test_extract_restore_ass_tags():
    text = r"{\i1}こんにちは{\i0}"
    cleaned, tag_map = _h().extract_tags(text)
    assert cleaned == "<TAG_0>こんにちは<TAG_1>"
    assert tag_map == {"<TAG_0>": r"{\i1}", "<TAG_1>": r"{\i0}"}
    assert _h().restore_tags("<TAG_0>你好世界<TAG_1>", tag_map) == r"{\i1}你好世界{\i0}"


def test_extract_restore_srt_tags():
    text = '<i>hello</i> <font color="#ffffff">world</font>'
    cleaned, tag_map = _h().extract_tags(text)
    assert cleaned == "<TAG_0>hello<TAG_1> <TAG_2>world<TAG_3>"
    assert set(tag_map.values()) == {"<i>", "</i>", '<font color="#ffffff">', "</font>"}
    assert _h().restore_tags(cleaned, tag_map) == text


def test_extract_mixed_ass_and_srt():
    text = r"{\an8}<b>タグ</b>"
    cleaned, tag_map = _h().extract_tags(text)
    assert cleaned == "<TAG_0><TAG_1>タグ<TAG_2>"
    assert _h().restore_tags(cleaned, tag_map) == text


def test_restore_no_placeholders():
    assert _h().restore_tags("普通文本", {}) == "普通文本"


def test_restore_partial_map():
    """占位符只还原 map 中存在的，其余文本原样保留"""
    restored = _h().restore_tags("<TAG_0>你好<TAG_1>", {"<TAG_0>": r"{\i1}"})
    assert restored == r"{\i1}你好<TAG_1>"


# ---------------------------------------------------------------------------
# 绘图检测
# ---------------------------------------------------------------------------

def test_has_drawing():
    h = _h()
    assert h.has_drawing(r"{\p1}") is True
    assert h.has_drawing(r"{\p1\bord0}") is True
    assert h.has_drawing(r"{\pos(10,20)\p1}") is True
    assert h.has_drawing(r"{\p0}") is False      # \p0 结束绘图，不算绘图块
    assert h.has_drawing(r"{\i1}text{\i0}") is False


# ---------------------------------------------------------------------------
# 跳过判定
# ---------------------------------------------------------------------------

def test_is_skip_entry():
    h = _h()
    assert h.is_skip_entry("") is True
    assert h.is_skip_entry("   ") is True
    assert h.is_skip_entry(r"{\p1}") is True
    assert h.is_skip_entry(r"{\an8}") is True          # 仅标签 → 剥离后为空
    assert h.is_skip_entry("♪ ♫") is True               # 纯音乐符号
    assert h.is_skip_entry("♪（）") is True
    assert h.is_skip_entry("こんにちは") is False
    assert h.is_skip_entry(r"{\i1}こんにちは{\i0}") is False
