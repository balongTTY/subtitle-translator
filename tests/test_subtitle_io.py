"""SubtitleFile 单元测试 — SRT/ASS/VTT 加载、编码回退链、保存回写"""

import sys
from pathlib import Path

import pysubs2
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.subtitle_io import SubtitleFile
from core.translation_batch import SubtitleEntry


SRT_SAMPLE = (
    "1\n00:00:01,000 --> 00:00:02,500\nこんにちは\n\n"
    "2\n00:00:03,000 --> 00:00:04,000\n元気ですか\n\n"
)

SRT_MULTILINE = (
    "1\n00:00:01,000 --> 00:00:02,500\nおはよう\\n今日もいい天気\n\n"
)


def write_file(tmp_path: Path, name: str, data: bytes) -> str:
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


class TestLoadSRT:
    def test_load_basic_utf8(self, tmp_path):
        path = write_file(tmp_path, "a.srt", SRT_SAMPLE.encode("utf-8"))
        entries = SubtitleFile().load(path)
        assert len(entries) == 2
        assert entries[0].index == 0
        assert entries[0].text == "こんにちは"
        assert entries[1].text == "元気ですか"
        assert entries[0].start == 1000
        assert entries[0].end == 2500

    def test_load_utf8_with_bom(self, tmp_path):
        path = write_file(tmp_path, "bom.srt", b"\xef\xbb\xbf" + SRT_SAMPLE.encode("utf-8"))
        entries = SubtitleFile().load(path)
        assert entries[0].text == "こんにちは"

    def test_load_crlf_line_endings(self, tmp_path):
        crlf = SRT_SAMPLE.replace("\n", "\r\n")
        path = write_file(tmp_path, "crlf.srt", crlf.encode("utf-8"))
        entries = SubtitleFile().load(path)
        assert len(entries) == 2
        assert entries[0].text == "こんにちは"

    def test_load_multiline_text(self, tmp_path):
        path = write_file(tmp_path, "ml.srt", SRT_MULTILINE.encode("utf-8"))
        entries = SubtitleFile().load(path)
        # pysubs2 的 plaintext 会把 \N / 换行合并展示
        assert "おはよう" in entries[0].text
        assert "いい天気" in entries[0].text

    def test_load_gbk_chinese(self, tmp_path):
        content = "1\n00:00:01,000 --> 00:00:02,000\n大家好，今天开始直播\n\n"
        path = write_file(tmp_path, "gbk.srt", content.encode("gb18030"))
        entries = SubtitleFile().load(path)
        assert entries[0].text == "大家好，今天开始直播"

    def test_load_shift_jis_with_kana(self, tmp_path):
        path = write_file(tmp_path, "sjis.srt", SRT_SAMPLE.encode("shift_jis"))
        entries = SubtitleFile().load(path)
        assert entries[0].text == "こんにちは"

    def test_load_utf16_with_bom(self, tmp_path):
        """Windows 记事本常见的 UTF-16 导出（带 BOM）必须在编码链内"""
        path = write_file(tmp_path, "u16.srt", SRT_SAMPLE.encode("utf-16"))
        entries = SubtitleFile().load(path)
        assert entries[0].text == "こんにちは"
        assert entries[1].text == "元気ですか"

    def test_empty_file_raises_pysubs2_error(self, tmp_path):
        path = write_file(tmp_path, "empty.srt", b"")
        with pytest.raises(pysubs2.Pysubs2Error, match="无法识别字幕文件格式"):
            SubtitleFile().load(path)

    def test_garbage_raises_with_chinese_message(self, tmp_path):
        path = write_file(tmp_path, "bad.srt", b"\x00\x01\x02 not a subtitle at all")
        with pytest.raises(pysubs2.Pysubs2Error, match="无法识别字幕文件格式"):
            SubtitleFile().load(path)


class TestLoadASS:
    def test_load_ass_strips_tags_in_plaintext(self, tmp_path):
        content = (
            "[Script Info]\nScriptType: v4.00+\n\n"
            "[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
            "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
            "MarginL, MarginR, MarginV, Encoding\n"
            "Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,"
            "0,0,0,0,100,100,0,0,1,2,0,2,10,10,10,1\n\n"
            "[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
            "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,{\\i1}こんにちは{\\i0}\n"
        )
        path = write_file(tmp_path, "a.ass", content.encode("utf-8"))
        entries = SubtitleFile().load(path)
        assert len(entries) == 1
        assert entries[0].text == "こんにちは"           # plaintext 已剥标签
        assert "{\\i1}" in entries[0].original_text      # original_text 保留标签
        assert entries[0].style == "Default"

    def test_load_vtt(self, tmp_path):
        content = (
            "WEBVTT\n\n"
            "00:00:01.000 --> 00:00:02.000\nこんにちは\n\n"
        )
        path = write_file(tmp_path, "a.vtt", content.encode("utf-8"))
        entries = SubtitleFile().load(path)
        assert len(entries) == 1
        assert entries[0].text == "こんにちは"


class TestSave:
    def _sample_entries(self) -> list[SubtitleEntry]:
        return [
            SubtitleEntry(index=0, start=1000, end=2500,
                          text="你好", original_text="こんにちは"),
            SubtitleEntry(index=1, start=3000, end=4000,
                          text="你还好吗", original_text="元気ですか"),
        ]

    def test_save_srt_roundtrip(self, tmp_path):
        src = write_file(tmp_path, "src.srt", SRT_SAMPLE.encode("utf-8"))
        entries = SubtitleFile().load(src)
        entries[0].text = "你好"
        entries[1].text = "你还好吗"
        out = str(tmp_path / "out.srt")
        SubtitleFile().save(out, entries, original_filepath=src)
        reloaded = SubtitleFile().load(out)
        assert len(reloaded) == 2
        assert [e.text for e in reloaded] == ["你好", "你还好吗"]
        assert reloaded[0].start == 1000 and reloaded[0].end == 2500
        assert not Path(out + ".tmp").exists()  # 原子写不残留临时文件

    def test_save_preserves_original_timeline(self, tmp_path):
        src = write_file(tmp_path, "src.srt", SRT_SAMPLE.encode("utf-8"))
        entries = SubtitleFile().load(src)
        # 只改文本、不动时间轴
        for e in entries:
            e.text = "译" + str(e.index)
        out = str(tmp_path / "out.srt")
        SubtitleFile().save(out, entries, original_filepath=src)
        reloaded = SubtitleFile().load(out)
        assert reloaded[1].start == 3000

    def test_save_appends_extra_entries(self, tmp_path):
        src = write_file(tmp_path, "src.srt", SRT_SAMPLE.encode("utf-8"))
        entries = SubtitleFile().load(src)
        entries.append(SubtitleEntry(index=2, start=5000, end=6000,
                                     text="新追加", original_text="追加"))
        out = str(tmp_path / "out.srt")
        SubtitleFile().save(out, entries, original_filepath=src)
        reloaded = SubtitleFile().load(out)
        assert len(reloaded) == 3
        assert reloaded[2].text == "新追加"
        assert reloaded[2].start == 5000

    def test_save_without_original(self, tmp_path):
        entries = self._sample_entries()
        out = str(tmp_path / "fresh.srt")
        SubtitleFile().save(out, entries)
        reloaded = SubtitleFile().load(out)
        assert [e.text for e in reloaded] == ["你好", "你还好吗"]

    def test_save_missing_original_falls_back_to_blank(self, tmp_path):
        """original_filepath 指向不存在的文件 → 视同无原始文件"""
        entries = self._sample_entries()
        out = str(tmp_path / "fresh2.srt")
        SubtitleFile().save(out, entries, original_filepath=str(tmp_path / "ghost.srt"))
        reloaded = SubtitleFile().load(out)
        assert len(reloaded) == 2

    def test_empty_text_keeps_original_event_text(self, tmp_path):
        """entry.text 为空 → 保留原事件文本，不写出空行"""
        src = write_file(tmp_path, "src.srt", SRT_SAMPLE.encode("utf-8"))
        entries = SubtitleFile().load(src)
        entries[0].text = ""
        out = str(tmp_path / "out.srt")
        SubtitleFile().save(out, entries, original_filepath=src)
        reloaded = SubtitleFile().load(out)
        assert reloaded[0].text == "こんにちは"


class TestFixSrtBold:
    def test_converts_ass_bold_tags(self):
        assert SubtitleFile._fix_srt_bold("{\\b1}粗体{\\b0}") == "<b>粗体</b>"

    def test_leaves_italic_to_pysubs2(self):
        assert SubtitleFile._fix_srt_bold("{\\i1}斜体{\\i0}") == "{\\i1}斜体{\\i0}"

    def test_no_tags_unchanged(self):
        assert SubtitleFile._fix_srt_bold("普通文本") == "普通文本"

    def test_partial_tags(self):
        assert SubtitleFile._fix_srt_bold("{\\b1}只有开头") == "<b>只有开头"
