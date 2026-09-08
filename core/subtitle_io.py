"""字幕文件 I/O — pysubs2 封装"""

import logging
import os
import re
from pathlib import Path

import pysubs2
from pysubs2.formats import FILE_EXTENSION_TO_FORMAT_IDENTIFIER

from .translation_batch import SubtitleEntry

log = logging.getLogger("subtitle_translator")

# 日文假名（平假名+片假名），用于 Shift-JIS 判定
_KANA_RE = re.compile(r"[ぁ-んァ-ヶ]")


class SubtitleFile:
    """统一处理 SRT/ASS/VTT 格式的字幕加载与保存"""

    def load(self, filepath: str) -> list[SubtitleEntry]:
        """加载字幕文件，返回内部表示（自动探测编码）"""
        ssa = self._load_any_encoding(filepath)
        entries: list[SubtitleEntry] = []

        for i, event in enumerate(ssa.events):
            entry = SubtitleEntry(
                index=i,
                start=event.start,
                end=event.end,
                text=event.plaintext,
                original_text=event.text,
                style=event.style if hasattr(event, "style") else "",
            )
            entries.append(entry)

        return entries

    @staticmethod
    def _load_any_encoding(filepath: str) -> pysubs2.SSAFile:
        """按回退链尝试解码，全部失败时给出明确错误

        顺序策略：
        - UTF-8（含 BOM）优先
        - Shift-JIS：仅当解码结果含日文假名才采用（避免 GBK 中文被误判为 SJIS）
        - GBK / GB18030（中文）
        - latin-1 兜底（永不失败）

        实现：一次性读入字节后逐个编码尝试 bytes.decode，
        只对首个成功解码的结果做一次 pysubs2 解析（避免对同一文件反复整读+重复解析）。
        空文件/结构损坏等无法识别格式的情形会抛 FormatAutodetectionError 并给出中文提示。
        """
        try:
            return SubtitleFile._decode_chain(filepath)
        except pysubs2.FormatAutodetectionError as e:
            # 空文件 / 结构损坏 / 无法识别格式：给出中文明确提示
            # （FormatAutodetectionError 的 str 是固定的英文，故用其基类携带中文消息）
            raise pysubs2.Pysubs2Error(
                f"无法识别字幕文件格式（文件可能为空或已损坏），"
                f"请检查文件内容或转存为标准 SRT/ASS/VTT 字幕。原始错误: {e}",
            ) from e

    @staticmethod
    def _decode_chain(filepath: str) -> pysubs2.SSAFile:
        """按编码回退链解码并解析（内部实现，异常原样上抛）"""
        raw = Path(filepath).read_bytes()

        def parse(text: str) -> pysubs2.SSAFile:
            # 统一换行为 \n：pysubs2 保存 SRT 用 \r\n，但其解析器读回时不剥 \r，
            # 会把 \r 污染进字幕文本（save→load 回环每行末尾多出 \r）
            return pysubs2.SSAFile.from_string(
                text.replace("\r\n", "\n").replace("\r", "\n"),
            )

        # 1) UTF-8
        for enc in ("utf-8-sig", "utf-8"):
            try:
                return parse(raw.decode(enc))
            except UnicodeDecodeError:
                continue

        # 1.5) UTF-16：仅当带 BOM 才尝试（Windows 记事本常见导出格式；
        # 无 BOM 的 UTF-16 无法与二进制数据区分，不盲试）
        if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
            try:
                return parse(raw.decode("utf-16"))
            except UnicodeDecodeError:
                pass

        # 2) Shift-JIS：含假名 → 日文字幕
        try:
            text = raw.decode("shift_jis")
        except UnicodeDecodeError:
            pass
        else:
            ssa = parse(text)
            parsed = "\n".join(e.plaintext for e in ssa.events)
            if _KANA_RE.search(parsed):
                return ssa

        # 3) GBK / GB18030（中文）
        try:
            return parse(raw.decode("gb18030"))
        except UnicodeDecodeError:
            pass

        # 4) 不含假名的 Shift-JIS（纯英文日文字幕）或 latin-1 兜底
        try:
            return parse(raw.decode("shift_jis"))
        except UnicodeDecodeError:
            pass
        try:
            return parse(raw.decode("latin-1"))
        except UnicodeDecodeError as e:
            raise UnicodeDecodeError(
                "utf-8", b"", 0, 1,
                f"无法识别字幕文件编码（已尝试 utf-8/shift_jis/gb18030/latin-1），"
                f"请手动转存为 UTF-8。原始错误: {e}",
            )

    def save(
        self,
        filepath: str,
        entries: list[SubtitleEntry],
        original_filepath: str | None = None,
    ) -> str:
        """保存翻译后的字幕，保留原格式和时间轴"""
        # 如果提供了原文件路径，从中加载样式/元数据
        if original_filepath and Path(original_filepath).exists():
            ssa = self._load_any_encoding(original_filepath)
        else:
            ssa = pysubs2.SSAFile()

        # 更新事件文本
        is_srt = Path(filepath).suffix.lower() == ".srt"
        for i, entry in enumerate(entries):
            if i < len(ssa.events):
                text = entry.text or ssa.events[i].plaintext
                if is_srt:
                    text = self._fix_srt_bold(text)
                ssa.events[i].text = text
                ssa.events[i].start = entry.start
                ssa.events[i].end = entry.end
            else:
                # 条目数超出原始文件 → 追加新事件
                text = entry.text or entry.original_text
                if is_srt:
                    text = self._fix_srt_bold(text)
                event = pysubs2.SSAEvent(
                    start=entry.start,
                    end=entry.end,
                    text=text,
                )
                ssa.events.append(event)

        out_path = str(filepath)
        # 原子写：先写临时文件再 replace，进程被杀/断电/磁盘满不会留下
        # 截断损坏的成品（旧的好文件可能已被备份移走，坏文件无处回退）。
        # 临时文件扩展名不受支持 → 显式按输出扩展名传格式标识
        tmp_path = out_path + ".tmp"
        fmt = FILE_EXTENSION_TO_FORMAT_IDENTIFIER.get(Path(out_path).suffix.lower())
        ssa.save(tmp_path, format_=fmt)
        os.replace(tmp_path, out_path)
        return out_path

    @staticmethod
    def _fix_srt_bold(text: str) -> str:
        """pysubs2 保存 SRT 时会把 \\i/\\u 转回 <i>/<u>，但会静默丢弃 \\b 标记。
        把 {\\b1}/{\\b0} 转回字面 <b>/</b>，保住粗体格式。"""
        text = re.sub(r"\{\\b1\}", "<b>", text)
        text = re.sub(r"\{\\b0\}", "</b>", text)
        return text

