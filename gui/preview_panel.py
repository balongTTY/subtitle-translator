"""预览面板 — 原文/译文双栏对照，支持筛选和就地编辑"""

from pathlib import Path

from PyQt5.QtCore import Qt, pyqtSignal, QEvent
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QTableWidget, QTableWidgetItem, QComboBox,
    QPushButton, QHeaderView, QAbstractItemView,
    QMenu, QAction, QApplication,
)

from core.translation_batch import SubtitleEntry, TranslationResult


class PreviewPanel(QWidget):
    """原文/译文对照预览面板"""

    entry_double_clicked = pyqtSignal(object)  # TranslationResult
    retranslate_requested = pyqtSignal(int)    # 条目索引
    file_selected = pyqtSignal(str)            # 用户切换查看的文件路径

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._entries: list[SubtitleEntry] = []
        self._results: dict[int, TranslationResult] = {}
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # 顶部栏
        top = QHBoxLayout()
        self.header = QLabel("对照预览")
        self.header.setStyleSheet("font-size: 11pt; font-weight: bold;")
        top.addWidget(self.header)
        top.addStretch()
        # 文件切换（并行翻译多个文件时查看各自结果）
        self.file_combo = QComboBox()
        self.file_combo.setMinimumWidth(160)
        self.file_combo.currentIndexChanged.connect(self._on_file_combo_changed)
        self.file_combo.setVisible(False)
        top.addWidget(self.file_combo)
        top.addWidget(QLabel("显示:"))
        self.filter_combo = QComboBox()
        self.filter_combo.addItem("全部", "all")
        self.filter_combo.addItem("需审核", "needs_review")
        self.filter_combo.addItem("已修改", "manually_edited")
        self.filter_combo.addItem("质检未通过", "qc_failed")
        self.filter_combo.currentIndexChanged.connect(self._apply_filter)
        top.addWidget(self.filter_combo)
        layout.addLayout(top)

        # 表格
        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(
            ["#", "开始", "结束", "原文", "译文", "状态"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        # 固定辅助列宽（序号/时间/状态），内容列Stretch——ResizeToContents 会随
        # 内容抖动且离屏渲染下计算偏窄，固定宽度保证时间戳完整可见
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.Fixed)
        self.table.setColumnWidth(0, 44)
        self.table.setColumnWidth(1, 62)
        self.table.setColumnWidth(2, 62)
        self.table.setColumnWidth(5, 64)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked)
        self.table.cellChanged.connect(self._on_cell_changed)
        self.table.doubleClicked.connect(self._on_double_click)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_context_menu)
        # 右键列头不显示排序等杂项
        self.table.horizontalHeader().setSectionsClickable(False)

        layout.addWidget(self.table)

        # 空状态提示（表格无内容时覆盖显示）
        self._empty_hint = QLabel(
            "加载字幕后，这里显示原文/译文对照\n\n拖入文件并「开始翻译」，或从视频「提取字幕」", self.table
        )
        self._empty_hint.setAlignment(Qt.AlignCenter)
        self._empty_hint.setWordWrap(True)
        self._empty_hint.setStyleSheet("color: #888; font-size: 10pt; border: none; background: transparent;")
        self._empty_hint.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.table.installEventFilter(self)
        self._update_empty_hint()

        # 底部图例 + 统计
        legend = QHBoxLayout()
        legend.addWidget(QLabel("⏳ 等待"))
        legend.addWidget(QLabel("|"))
        legend.addWidget(QLabel("✓ 通过"))
        legend.addWidget(QLabel("|"))
        legend.addWidget(QLabel("⚠ 需审核"))
        legend.addWidget(QLabel("|"))
        legend.addWidget(QLabel("✗ 失败"))
        legend.addWidget(QLabel("|"))
        legend.addWidget(QLabel("✎ 已修改"))
        legend.addStretch()
        self.stats_label = QLabel("")
        self.stats_label.setStyleSheet("color: #888; font-size: 8.5pt;")
        legend.addWidget(self.stats_label)
        layout.addLayout(legend)

    def eventFilter(self, obj, event) -> bool:
        """表格尺寸变化时同步空状态提示位置"""
        if obj is self.table and event.type() == QEvent.Resize:
            self._reposition_empty_hint()
        return super().eventFilter(obj, event)

    def _reposition_empty_hint(self) -> None:
        if self._empty_hint.isVisible():
            self._empty_hint.setGeometry(self.table.viewport().geometry())

    def _update_empty_hint(self) -> None:
        empty = self.table.rowCount() == 0
        self._empty_hint.setVisible(empty)
        if empty:
            self._reposition_empty_hint()
            self._empty_hint.raise_()

    def _update_stats(self) -> None:
        """底部统计：共 N 条 · 各状态数量"""
        total = len(self._entries)
        if not total:
            self.stats_label.setText("")
            return
        done = sum(1 for r in self._results.values() if r.status == "qc_passed")
        review = sum(1 for r in self._results.values() if r.status == "needs_review")
        parts = [f"共 {total} 条"]
        if done:
            parts.append(f"✓ {done}")
        if review:
            parts.append(f"⚠ {review}")
        self.stats_label.setText(" · ".join(parts))

    def load_entries(
        self,
        filepath: str,
        entries: list[SubtitleEntry],
        results: dict[int, TranslationResult] | None = None,
    ) -> None:
        """加载字幕条目和（可选的）翻译结果"""
        self._entries = entries
        self._results = results or {}
        self.header.setText(f"对照预览 · {Path(filepath).name}")
        self._populate_table()

    # ---- 文件切换（并行模式） ----

    def set_file_list(self, filepaths: list[str]) -> None:
        """更新文件下拉列表；多文件时显示，单文件隐藏"""
        current = self.file_combo.currentData()
        self.file_combo.blockSignals(True)
        self.file_combo.clear()
        for fp in filepaths:
            self.file_combo.addItem(Path(fp).name, fp)
        if current and current in filepaths:
            self.file_combo.setCurrentIndex(filepaths.index(current))
        self.file_combo.blockSignals(False)
        self.file_combo.setVisible(len(filepaths) > 1)

    def _on_file_combo_changed(self, index: int) -> None:
        fp = self.file_combo.itemData(index)
        if fp:
            self.file_selected.emit(fp)

    def clear(self) -> None:
        """清空预览（文件列表清空时）"""
        self._entries = []
        self._results = {}
        self.header.setText("对照预览")
        self.file_combo.blockSignals(True)
        self.file_combo.clear()
        self.file_combo.blockSignals(False)
        self.file_combo.setVisible(False)
        self._populate_table()

    def _populate_table(self) -> None:
        # blockSignals：setItem 会触发 cellChanged，若不屏蔽会把所有结果状态
        # 污染成 manually_edited（见 _on_cell_changed）。用户手动编辑不受影响，
        # 因为编辑器产生的 cellChanged 在解除屏蔽后正常触发。
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        entries_to_show = self._get_filtered_entries()

        self.table.setRowCount(len(entries_to_show))
        for row, entry in enumerate(entries_to_show):
            self._set_row(row, entry)
        self.table.blockSignals(False)
        self._update_empty_hint()
        self._update_stats()

    def update_results(self, results: list[TranslationResult]) -> None:
        """批量完成/质检失败后就地更新受影响行，避免整表销毁重建（审计 #59）。

        仅在「全部」筛选且表格已有行时做就地更新（此时表格行序 == _entries 顺序）；
        筛选视图或空表回退整表重建，保证状态变化后该出现/隐藏的行也正确。
        """
        if not results:
            return
        for r in results:
            self._results[r.index] = r
        if self.filter_combo.currentData() != "all" or not self.table.rowCount():
            self._populate_table()
            return

        # 索引 → 行号 映射（第 0 列显示的是 1 起始序号，解析时减 1）
        row_by_idx: dict[int, int] = {}
        for row in range(self.table.rowCount()):
            entry_idx = self._entry_index_at(row)
            if entry_idx is not None:
                row_by_idx[entry_idx] = row

        self.table.blockSignals(True)
        try:
            for r in results:
                row = row_by_idx.get(r.index)
                if row is not None:
                    self._set_row(row, self._entries[row])
        finally:
            self.table.blockSignals(False)
        self._update_stats()

    def _set_row(self, row: int, entry: SubtitleEntry) -> None:
        """填充一行（第 0 列显示 1 起始序号，与状态栏「第 N 条」提示一致）"""
        start_str = self._format_time(entry.start)
        end_str = self._format_time(entry.end)

        result = self._results.get(entry.index)
        translated = entry.text
        status = ""

        if result:
            translated = result.translated_text
            status_map = {
                "qc_passed": "[✓]",
                "qc_failed": "[✗]",
                "needs_review": "[⚠]",
                "manually_edited": "[✎]",
            }
            status = status_map.get(result.status, "")
        else:
            # 尚未翻译
            status = "[⏳]"

        items = [
            QTableWidgetItem(str(entry.index + 1)),     # col 0（1 起始）
            QTableWidgetItem(start_str),                 # col 1
            QTableWidgetItem(end_str),                   # col 2
            QTableWidgetItem(entry.original_text or entry.text),  # col 3
            QTableWidgetItem(translated),                # col 4 （可编辑）
            QTableWidgetItem(status),                    # col 5
        ]

        for col, item in enumerate(items):
            if col != 4:  # 译文列可编辑，其他不可编辑
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(row, col, item)

        # 状态着色
        if status == "[✓]":
            pass
        elif status == "[⏳]":
            for col in range(6):
                item = self.table.item(row, col)
                if item:
                    item.setForeground(Qt.gray)
        elif status == "[⚠]":
            for col in range(6):
                item = self.table.item(row, col)
                if item:
                    item.setBackground(Qt.yellow)
        elif status == "[✗]":
            for col in range(6):
                item = self.table.item(row, col)
                if item:
                    item.setBackground(
                        self.table.palette().color(self.table.palette().Highlight).lighter(160)
                    )

    def _entry_index_at(self, row: int) -> int | None:
        """从表格行解析条目索引（第 0 列显示 1 起始序号）"""
        idx_item = self.table.item(row, 0)
        if idx_item is None:
            return None
        try:
            return int(idx_item.text()) - 1
        except ValueError:
            return None

    def _format_time(self, ms: float) -> str:
        """紧凑时间戳：不足 1 小时省略小时位，不显示毫秒（列宽友好）"""
        total_s = int(ms / 1000)
        h = total_s // 3600
        m = (total_s % 3600) // 60
        s = total_s % 60
        if h > 0:
            return f"{h:02d}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"

    def _get_filtered_entries(self) -> list[SubtitleEntry]:
        filter_key = self.filter_combo.currentData()
        if not filter_key or filter_key == "all" or not self._results:
            return self._entries

        return [
            e
            for e in self._entries
            if self._results.get(e.index)
            and self._results[e.index].status == filter_key
        ]

    def _apply_filter(self) -> None:
        self._populate_table()

    def _on_cell_changed(self, row: int, col: int) -> None:
        """就地编辑译文"""
        if col != 4:
            return

        entry_idx = self._entry_index_at(row)
        if entry_idx is None:
            return

        new_text = self.table.item(row, 4).text()
        if entry_idx in self._results:
            self._results[entry_idx].translated_text = new_text
            self._results[entry_idx].status = "manually_edited"
            self._results[entry_idx].qc_issues = []
            # 同步更新状态单元格显示（否则编辑后仍显示旧状态图标）
            status_item = self.table.item(row, 5)
            if status_item:
                status_item.setText("[✎]")

    def _on_double_click(self, index) -> None:
        """双击打开编辑对话框"""
        entry_idx = self._entry_index_at(index.row())
        if entry_idx is None:
            return

        if entry_idx in self._results:
            self.entry_double_clicked.emit(self._results[entry_idx])

    def _on_context_menu(self, pos) -> None:
        row = self.table.rowAt(pos.y())
        if row < 0:
            return

        entry_idx = self._entry_index_at(row)
        if entry_idx is None:
            return

        menu = QMenu()
        retranslate_action = QAction("重新翻译此条", self)
        copy_orig = QAction("复制原文", self)
        copy_trans = QAction("复制译文", self)
        mark_reviewed = QAction("标记为已审核", self)

        menu.addAction(retranslate_action)
        menu.addSeparator()
        menu.addAction(copy_orig)
        menu.addAction(copy_trans)
        menu.addSeparator()
        menu.addAction(mark_reviewed)

        # entry_idx 已在 _entry_index_at(row) 求得；此处曾有
        # entry_idx = int(idx_item.text()) 引用未定义变量 → 右键即 NameError 崩溃
        orig_text = self.table.item(row, 3).text() or ""
        trans_text = self.table.item(row, 4).text() or ""

        copy_orig.triggered.connect(
            lambda: self._copy_to_clipboard(orig_text)
        )
        copy_trans.triggered.connect(
            lambda: self._copy_to_clipboard(trans_text)
        )
        mark_reviewed.triggered.connect(
            lambda: self._mark_reviewed(entry_idx)
        )
        retranslate_action.triggered.connect(
            lambda: self.retranslate_requested.emit(entry_idx)
        )

        menu.exec_(self.table.viewport().mapToGlobal(pos))

    def _mark_reviewed(self, entry_idx: int) -> None:
        result = self._results.get(entry_idx)
        if result and result.status in ("needs_review", "qc_failed"):
            result.status = "qc_passed"
            result.qc_issues = []
            self._populate_table()

    def _copy_to_clipboard(self, text: str) -> None:
        QApplication.clipboard().setText(text)
