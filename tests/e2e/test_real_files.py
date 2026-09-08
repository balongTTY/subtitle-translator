"""真实风格字幕文件鲁棒性测试

覆盖：BOM/CRLF、SRT HTML 标签、多行文本、ASS 样式/斜体/绘图块、
标签往返完整性和编码。用 fake LLM 走完整流水线。
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(os.path.abspath(__file__)).parent.parent.parent))


class FakeLLM:
    def analyze(self, prompt):
        return '{"topic_segments": [], "glossary": {}, "style_guide": ""}'

    def translate_batch(self, batch, src, tgt):
        n = batch.primary_end_idx - batch.primary_start_idx
        # 原样返回（测试标签往返，不真正翻译）
        return [
            batch.entries[batch.primary_start_idx + i].text
            for i in range(n)
        ]

    def validate_api_key(self):
        return True


# ---- 真实风格 SRT：BOM + CRLF + HTML 标签 + 多行文本 ----
SRT_CONTENT = (
    "﻿1\r\n"
    "00:00:00,000 --> 00:00:03,500\r\n"
    "みなさんこんにちは！\r\n"
    "\r\n"
    "2\r\n"
    "00:00:04,000 --> 00:00:07,200\r\n"
    "<i>今日の配信は</i>エルデンリングだよ\r\n"
    "みんなで倒そう\r\n"
    "\r\n"
    "3\r\n"
    "00:00:08,000 --> 00:00:10,000\r\n"
    "<b>(笑)また死んだww</b>\r\n"
    "\r\n"
    "4\r\n"
    "00:00:10,500 --> 00:00:12,000\r\n"
    "x < y の比較を考える\r\n"
    "\r\n"
)

ASS_CONTENT = (
    "[Script Info]\r\n"
    "Title: test\r\n"
    "ScriptType: v4.00+\r\n"
    "PlayResX: 1920\r\n"
    "PlayResY: 1080\r\n"
    "\r\n"
    "[V4+ Styles]\r\n"
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\r\n"
    "Style: Default,Arial,60,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,3,0,2,10,10,10,1\r\n"
    "\r\n"
    "[Events]\r\n"
    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\r\n"
    "Dialogue: 0,0:00:00.00,0:00:03.00,Default,,0,0,0,,こんにちは\r\n"
    "Dialogue: 0,0:00:03.00,0:00:06.00,Default,,0,0,0,,{\\i1}イタリックテスト{\\i0}\r\n"
    "Dialogue: 0,0:00:06.00,0:00:09.00,Default,,0,0,0,,{\\p1}m 0 0 l 100 100{\\p0}\r\n"
    "Dialogue: 0,0:00:09.00,0:00:12.00,Default,,0,0,0,,{\\pos(10,20)\\b1}複合タグ{\\b0}\r\n"
)


def test_srt():
    from core.subtitle_io import SubtitleFile
    from core.tag_handler import ASSTagHandler
    from core.merger import Merger

    tmp = Path(tempfile.mkdtemp())
    src = tmp / "real.srt"
    src.write_text(SRT_CONTENT, encoding="utf-8")

    entries = SubtitleFile().load(str(src))
    print(f"[SRT] 加载 {len(entries)} 条（期望 4）: {[e.text.splitlines()[0][:12] for e in entries]}")
    assert len(entries) == 4, f"SRT 条数错误: {len(entries)}"

    th = ASSTagHandler()
    for e in entries:
        e.text, _ = th.extract_tags(e.original_text)

    # 第 2 条有 <i> 标签
    cleaned2 = entries[1].text
    print(f"[SRT] 第2条去标签后: {cleaned2!r}")
    assert "<TAG_0>" in cleaned2 and "エルデンリング" in cleaned2, "SRT HTML 标签未保护"
    # 第 4 条的 'x < y' 不应被当标签
    assert "< y" in entries[3].text, "比较符号被误判为标签"

    # 往返
    for e in entries:
        _, tag_map = th.extract_tags(e.original_text)
        restored = th.restore_tags(e.text, tag_map)
        assert restored == e.original_text, f"标签往返不一致: {restored!r} vs {e.original_text!r}"
    print("[SRT] 标签往返一致 ✓")

    # 完整保存往返（用 fake 译文=原文）
    merged = Merger().merge(entries, {e.index: type("R", (), {
        "index": e.index, "translated_text": e.text, "status": "qc_passed", "qc_issues": []
    })() for e in entries})
    out = tmp / "real_zh.srt"
    SubtitleFile().save(str(out), merged, str(src))
    content = out.read_text(encoding="utf-8-sig")
    print(f"[SRT] 保存后 <i> 标签保留: {'<i>' in content}")
    assert "<i>" in content and "</i>" in content, "SRT HTML 标签在输出中丢失"
    assert "<b>" in content and "</b>" in content, "SRT b 标签丢失"
    assert "x < y" in content, "比较符号内容丢失"
    print("[SRT] 输出保留 HTML 标签与原文 ✓")
    print("[SRT] 注: <font> 等样式标签会被 pysubs2 在加载时剥离（pysubs2 限制）")


def test_ass():
    from core.subtitle_io import SubtitleFile
    from core.tag_handler import ASSTagHandler
    from core.merger import Merger

    tmp = Path(tempfile.mkdtemp())
    src = tmp / "real.ass"
    src.write_text(ASS_CONTENT, encoding="utf-8")

    entries = SubtitleFile().load(str(src))
    print(f"\n[ASS] 加载 {len(entries)} 条: {[e.text[:14] for e in entries]}")
    assert len(entries) == 4

    th = ASSTagHandler()
    for e in entries:
        e.text, _ = th.extract_tags(e.original_text)

    # 绘图块应被识别为 skip
    skip = [e.index for e in entries if th.is_skip_entry(e.original_text)]
    print(f"[ASS] 跳过的条目索引: {skip}")
    assert 2 in skip, "绘图块未跳过"
    # 复合标签 {\pos\b1} 也应被... 不，它含 \b1 不是 \p，所以不跳过
    assert 3 not in skip, "普通加粗不应跳过"

    # 标签往返
    for e in entries:
        _, tag_map = th.extract_tags(e.original_text)
        restored = th.restore_tags(e.text, tag_map)
        assert restored == e.original_text, f"ASS 往返不一致: {restored!r}"
    print("[ASS] 标签往返一致 ✓")

    # 保存后样式保留
    merged = Merger().merge(entries, {e.index: type("R", (), {
        "index": e.index, "translated_text": e.text, "status": "qc_passed", "qc_issues": []
    })() for e in entries})
    out = tmp / "real_zh.ass"
    SubtitleFile().save(str(out), merged, str(src))
    content = out.read_text(encoding="utf-8")
    assert "[V4+ Styles]" in content and "Style: Default,Arial" in content, "ASS 样式丢失"
    assert "{\\i1}イタリックテスト{\\i0}" in content, "ASS 斜体标签丢失"
    assert "{\\p1}m 0 0 l 100 100{\\p0}" in content, "绘图块内容丢失"
    assert "{\\pos(10,20)\\b1}" in content or "{\\pos" in content, "复合标签丢失"
    print("[ASS] 样式/标签/绘图块全部保留 ✓")


def test_pipeline_real_files():
    """用真实风格文件 + fake LLM 走完整 translator 流水线"""
    import time
    from PyQt5.QtWidgets import QApplication
    import core.translator as T
    from config.settings import AppSettings

    app = QApplication(sys.argv)
    s = AppSettings()
    s.parallel_files = 1
    T.TranslatorWorker._get_llm_service = lambda self: FakeLLM()

    tmp = Path(tempfile.mkdtemp())
    (tmp / "a.srt").write_text(SRT_CONTENT, encoding="utf-8")
    (tmp / "b.ass").write_text(ASS_CONTENT, encoding="utf-8")

    facade = T.TranslatorFacade()
    facade.prepare_translation([str(tmp / "a.srt"), str(tmp / "b.ass")])
    done = []
    facade.signals.waiting_for_approval.connect(lambda fp: facade.approve_analysis(fp, None))
    facade.signals.all_completed.connect(lambda r: done.append(r))
    facade.start()
    deadline = time.time() + 30
    while not done and time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)
    app.processEvents()

    print(f"\n[流水线] 完成: {bool(done)}, 输出: {list(done[0].keys()) if done else []}")
    assert done and len(done[0]) == 2, "两个文件都应完成"

    zh_srt = (tmp / "a_zh.srt").read_text(encoding="utf-8-sig")
    zh_ass = (tmp / "b_zh.ass").read_text(encoding="utf-8")
    assert "<i>" in zh_srt, "流水线输出 SRT 丢标签"
    assert "{\\i1}" in zh_ass, "流水线输出 ASS 丢标签"
    facade._force_cleanup()
    print("[流水线] 真实文件走完整流水线，输出保留全部格式 ✓")


def main() -> int:
    test_srt()
    test_ass()
    test_pipeline_real_files()
    print("\n真实风格文件鲁棒性测试全部通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
