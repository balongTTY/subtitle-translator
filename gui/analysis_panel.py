"""分析结果编辑面板 — 展示和手工修正分析结果"""

from PyQt5.QtCore import Qt, pyqtSignal, QSize
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QTreeWidget, QTreeWidgetItem, QTableWidget, QTableWidgetItem,
    QSplitter, QHeaderView, QAbstractItemView,
    QPushButton, QTextEdit, QGroupBox, QInputDialog,
    QStyledItemDelegate, QStyleOptionViewItem,
    QDialog, QLineEdit,
)
from PyQt5.QtGui import QFont

from core.translation_batch import AnalysisResult
from gui.theme_manager import ThemeManager

# 树节点 UserRole+1 存放纯话题文本（不带时段前缀），与显示文本解耦：
# _sync_from_ui 读回时用纯文本，避免把 "00:00~05:00 话题" 前缀写进
# topic 字段污染注入提示词（审计 #70）
_TOPIC_TEXT_ROLE = Qt.UserRole + 1


def _strip_topic_prefix(seg: dict, display_text: str) -> str:
    """从树节点显示文本中剥离时段前缀，还原纯话题文本"""
    time_range = seg.get("time_range", "")
    if time_range and display_text.startswith(time_range + "  "):
        return display_text[len(time_range) + 2:]
    return display_text


class _GlossaryDelegate(QStyledItemDelegate):
    """术语表编辑器代理：确保编辑控件尺寸充足"""

    def createEditor(self, parent, option, index):
        editor = QTextEdit(parent)
        editor.setMinimumHeight(40)
        editor.setMinimumWidth(160)
        editor.setFont(QFont("Microsoft YaHei UI", 9))
        editor.setStyleSheet(
            "QTextEdit { padding: 4px 6px; border: 2px solid #0078D4; border-radius: 4px; }"
        )
        return editor

    def setEditorData(self, editor, index):
        text = index.data(Qt.DisplayRole) or ""
        editor.setPlainText(text)
        editor.selectAll()

    def setModelData(self, editor, model, index):
        model.setData(index, editor.toPlainText().strip())

    def updateEditorGeometry(self, editor, option, index):
        # 编辑器最小 120px 宽，撑满列宽
        r = option.rect
        w = max(r.width(), 160)
        h = max(r.height(), 44)
        editor.setGeometry(r.x(), r.y(), w, h)

    def sizeHint(self, option, index):
        return QSize(160, 30)


class AnalysisPanel(QWidget):
    """可编辑的全篇分析结果面板"""

    analysis_modified = pyqtSignal(object)
    approve_clicked = pyqtSignal(object)
    optimize_glossary = pyqtSignal(object)
    approve_all_clicked = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._analysis: AnalysisResult | None = None
        self._filepath: str = ""
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.header = QLabel("全篇分析")
        self.header.setStyleSheet("font-size: 11pt; font-weight: bold; padding: 2px 0;")
        layout.addWidget(self.header)

        splitter = QSplitter(Qt.Vertical)

        # ============================================================
        #  话题分段
        # ============================================================
        topic_group = QGroupBox("话题分段")
        topic_layout = QVBoxLayout(topic_group)
        topic_layout.setContentsMargins(8, 12, 8, 8)

        self.topic_tree = QTreeWidget()
        self.topic_tree.setHeaderLabels(["时段 & 话题（双击弹出编辑）"])
        self.topic_tree.setRootIsDecorated(True)
        self.topic_tree.setAlternatingRowColors(True)
        self.topic_tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.topic_tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.topic_tree.setStyleSheet(
            "QTreeWidget { font-size: 9pt; }"
            "QTreeWidget::item { padding: 3px 4px; }"
        )
        self.topic_tree.itemDoubleClicked.connect(self._edit_topic_dialog)

        topic_btns = QHBoxLayout()
        for label, slot in [("+ 添加话题", self._add_topic),
                            ("+ 关键术语", self._add_topic_term),
                            ("- 删除", self._delete_topic)]:
            btn = QPushButton(label)
            btn.setObjectName("smallBtn")
            btn.clicked.connect(slot)
            topic_btns.addWidget(btn)
        topic_btns.addStretch()

        topic_layout.addWidget(self.topic_tree)
        topic_layout.addLayout(topic_btns)
        splitter.addWidget(topic_group)

        # ============================================================
        #  术语表 (QTableWidget) — 双击弹出编辑框
        # ============================================================
        glossary_group = QGroupBox("术语表")
        glossary_layout = QVBoxLayout(glossary_group)
        glossary_layout.setContentsMargins(8, 12, 8, 8)

        self.glossary_table = QTableWidget()
        self.glossary_table.setColumnCount(2)
        self.glossary_table.setHorizontalHeaderLabels(["源术语", "译法（双击/直接输入编辑，Tab 跳格）"])
        self.glossary_table.setAlternatingRowColors(True)
        self.glossary_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.glossary_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.glossary_table.setEditTriggers(
            QAbstractItemView.DoubleClicked |
            QAbstractItemView.EditKeyPressed |
            QAbstractItemView.AnyKeyPressed
        )
        self.glossary_table.setItemDelegate(_GlossaryDelegate(self.glossary_table))
        self.glossary_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.glossary_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.glossary_table.verticalHeader().setVisible(False)
        self.glossary_table.setWordWrap(True)
        self.glossary_table.setStyleSheet(
            "QTableWidget { font-size: 9pt; }"
            "QTableWidget::item { padding: 6px 8px; }"
        )
        self.glossary_table.cellChanged.connect(self._on_glossary_cell_changed)

        gloss_btns = QHBoxLayout()
        for label, slot in [("+ 添加术语", self._add_glossary),
                            ("- 删除选中行", self._delete_glossary)]:
            btn = QPushButton(label)
            btn.setObjectName("smallBtn")
            btn.clicked.connect(slot)
            gloss_btns.addWidget(btn)

        self.optimize_gloss_btn = QPushButton("深度优化术语")
        self.optimize_gloss_btn.setObjectName("smallBtn")
        self.optimize_gloss_btn.setToolTip("让 AI 重新审视所有术语，自动优化翻译、补充遗漏、删除冗余")
        self.optimize_gloss_btn.clicked.connect(self._on_optimize_glossary)
        gloss_btns.addWidget(self.optimize_gloss_btn)

        gloss_btns.addStretch()

        glossary_layout.addWidget(self.glossary_table)
        glossary_layout.addLayout(gloss_btns)
        splitter.addWidget(glossary_group)

        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)

        # ============================================================
        #  风格指南
        # ============================================================
        style_group = QGroupBox("翻译风格指南")
        style_layout = QVBoxLayout(style_group)
        style_layout.setContentsMargins(8, 12, 8, 8)

        self.style_edit = QTextEdit()
        self.style_edit.setMinimumHeight(60)
        self.style_edit.setMaximumHeight(140)
        self.style_edit.setPlaceholderText("翻译风格建议，如：口语化、保留敬语、使用网络流行语...")
        self.style_edit.textChanged.connect(self._on_style_changed)
        style_layout.addWidget(self.style_edit)
        layout.addWidget(style_group)

        # ============================================================
        #  等待提示 + 确认按钮
        # ============================================================
        self.waiting_hint = QLabel("")
        self.waiting_hint.setWordWrap(True)
        self._update_waiting_hint_style()
        self.waiting_hint.setVisible(False)
        layout.addWidget(self.waiting_hint)

        btn_layout = QHBoxLayout()
        self.approve_btn = QPushButton("确认分析，继续翻译 →")
        self.approve_btn.setObjectName("primaryBtn")
        self.approve_btn.clicked.connect(self._on_approve)
        self.skip_btn = QPushButton("跳过分析，直接翻译")
        self.skip_btn.clicked.connect(self._on_skip)
        self.approve_all_btn = QPushButton("确认全部")
        self.approve_all_btn.setToolTip("批量处理多文件时：确认当前文件，并自动确认其余所有待确认的文件")
        self.approve_all_btn.clicked.connect(self._on_approve_all)
        btn_layout.addWidget(self.skip_btn)
        btn_layout.addStretch()
        btn_layout.addWidget(self.approve_all_btn)
        btn_layout.addWidget(self.approve_btn)
        layout.addLayout(btn_layout)

        self.setVisible(False)

    # ==================================================================
    #  数据加载
    # ==================================================================

    @property
    def analysis(self) -> AnalysisResult | None:
        self._sync_from_ui()
        return self._analysis

    @property
    def current_filepath(self) -> str:
        """当前面板展示分析结果对应的文件路径（供主窗口路由确认）"""
        return self._filepath

    def show_analysis(self, filepath: str, analysis: AnalysisResult) -> None:
        self._filepath = filepath
        self._analysis = analysis
        fname = filepath.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        self.header.setText(f"全篇分析 · {fname}")
        self.setVisible(True)
        self.approve_btn.setEnabled(True)
        self.skip_btn.setEnabled(True)
        self.waiting_hint.setText("翻译已暂停 — 请检查并修改分析结果（话题、术语、风格），确认无误后点击下方按钮继续翻译")
        self._update_waiting_hint_style()
        self.waiting_hint.setVisible(True)
        self._populate()

    def _update_waiting_hint_style(self) -> None:
        tm = ThemeManager()
        if tm.current_theme == "dark":
            bg = "#1E3A5F"
            fg = "#60CDFF"
        else:
            bg = "#E5F0FF"
            fg = "#0078D4"
        self.waiting_hint.setStyleSheet(
            f"color: {fg}; font-weight: bold; padding: 8px 12px; "
            f"background: {bg}; border-radius: 6px; font-size: 9.5pt;"
        )

    def clear(self) -> None:
        self.topic_tree.clear()
        self.glossary_table.setRowCount(0)
        self.style_edit.clear()
        self._analysis = None
        self.setVisible(False)

    def _populate(self) -> None:
        if not self._analysis:
            return
        self._populate_topics()
        self._populate_glossary()
        self._populate_style()

    # ------------------------------------------------------------------
    #  话题分段
    # ------------------------------------------------------------------

    def _populate_topics(self) -> None:
        self.topic_tree.clear()
        if not self._analysis:
            return
        for seg in self._analysis.topic_segments:
            item = QTreeWidgetItem()
            time_range = seg.get("time_range", "")
            topic = seg.get("topic", "")
            item.setText(0, f"{time_range}  {topic}" if time_range else topic)
            item.setToolTip(0, "双击弹出编辑")
            item.setData(0, Qt.UserRole, seg)
            item.setData(0, _TOPIC_TEXT_ROLE, topic)
            for term in seg.get("key_terms", [])[:8]:
                child = QTreeWidgetItem()
                child.setText(0, term)
                child.setToolTip(0, "双击弹出编辑")
                item.addChild(child)
            self.topic_tree.addTopLevelItem(item)
        self.topic_tree.expandAll()

    def _edit_topic_dialog(self, item: QTreeWidgetItem, column: int) -> None:
        """双击话题弹出编辑"""
        dlg = QDialog(self)
        dlg.setMinimumSize(480, 160)
        layout = QVBoxLayout(dlg)

        parent = item.parent()
        if parent:
            seg = parent.data(0, Qt.UserRole)
            old_text = item.text(0)
            dlg.setWindowTitle("编辑关键术语")
            layout.addWidget(QLabel(f"所属话题: {parent.text(0)}"))
            layout.addWidget(QLabel("术语:"))
            edit = QLineEdit(old_text)
        else:
            seg = item.data(0, Qt.UserRole)
            old_text = item.text(0)
            dlg.setWindowTitle("编辑话题")
            layout.addWidget(QLabel("话题描述 (格式: 00:00~05:00 话题名称):"))
            edit = QLineEdit(old_text)

        layout.addWidget(edit)

        btn_layout = QHBoxLayout()
        save_btn = QPushButton("保存")
        save_btn.setObjectName("primaryBtn")
        cancel_btn = QPushButton("取消")
        save_btn.clicked.connect(dlg.accept)
        cancel_btn.clicked.connect(dlg.reject)
        btn_layout.addStretch()
        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(save_btn)
        layout.addLayout(btn_layout)

        if dlg.exec_() == QDialog.Accepted:
            new_text = edit.text().strip()
            if new_text:
                item.setText(0, new_text)
                if isinstance(seg, dict):
                    if parent:
                        terms = seg.get("key_terms", [])
                        if old_text in terms:
                            terms[terms.index(old_text)] = new_text
                    else:
                        # 编辑话题：剥离时段前缀存纯文本，并同步 UserRole 数据
                        seg["topic"] = _strip_topic_prefix(seg, new_text)
                        item.setData(0, _TOPIC_TEXT_ROLE, seg["topic"])
                self._emit_modified()

    def _add_topic(self) -> None:
        text, ok = QInputDialog.getText(
            self, "添加话题",
            "话题描述（格式: 00:00~05:00 话题名称）:"
        )
        if ok and text.strip():
            seg = {"time_range": "", "topic": text.strip(), "key_terms": []}
            if self._analysis:
                self._analysis.topic_segments.append(seg)
            self._populate_topics()
            self._emit_modified()

    def _add_topic_term(self) -> None:
        item = self.topic_tree.currentItem()
        if not item:
            return
        # 如果是子节点，用父节点
        parent = item.parent()
        target = parent if parent else item
        seg = target.data(0, Qt.UserRole)
        if not isinstance(seg, dict):
            return
        text, ok = QInputDialog.getText(self, "添加关键术语", "术语:")
        if ok and text.strip():
            seg.setdefault("key_terms", []).append(text.strip())
            child = QTreeWidgetItem()
            child.setText(0, text.strip())
            child.setFlags(child.flags() | Qt.ItemIsEditable)
            target.addChild(child)
            target.setExpanded(True)
            self._emit_modified()

    def _delete_topic(self) -> None:
        item = self.topic_tree.currentItem()
        if not item:
            return
        parent = item.parent()
        if parent:
            seg = parent.data(0, Qt.UserRole)
            if isinstance(seg, dict) and item.text(0) in seg.get("key_terms", []):
                seg["key_terms"].remove(item.text(0))
            parent.removeChild(item)
            self._emit_modified()
        else:
            idx = self.topic_tree.indexOfTopLevelItem(item)
            if idx >= 0 and self._analysis:
                self._analysis.topic_segments.pop(idx)
                self.topic_tree.takeTopLevelItem(idx)
            self._emit_modified()

    # ------------------------------------------------------------------
    #  术语表 (QTableWidget)
    # ------------------------------------------------------------------

    def _populate_glossary(self) -> None:
        self.glossary_table.blockSignals(True)
        self.glossary_table.setRowCount(0)
        if not self._analysis:
            self.glossary_table.blockSignals(False)
            return

        items = list(self._analysis.glossary.items())
        self.glossary_table.setRowCount(len(items))
        for row, (src, tgt) in enumerate(items):
            src_item = QTableWidgetItem(src)
            src_item.setToolTip(src)
            tgt_item = QTableWidgetItem(tgt)
            tgt_item.setToolTip(tgt)
            self.glossary_table.setItem(row, 0, src_item)
            self.glossary_table.setItem(row, 1, tgt_item)
            self._adjust_glossary_row_height(row)

        self.glossary_table.blockSignals(False)

    def _on_glossary_cell_changed(self, row: int, col: int) -> None:
        """inline 编辑后自动同步到 AnalysisResult"""
        if not self._analysis:
            return
        src_item = self.glossary_table.item(row, 0)
        tgt_item = self.glossary_table.item(row, 1)
        if not src_item:
            return
        src = src_item.text().strip()
        tgt = tgt_item.text().strip() if tgt_item else ""
        if src:
            self._analysis.glossary[src] = tgt
            self._emit_modified()
        # 自适应行高
        self._adjust_glossary_row_height(row)

    def _adjust_glossary_row_height(self, row: int) -> None:
        src_item = self.glossary_table.item(row, 0)
        tgt_item = self.glossary_table.item(row, 1)
        src_len = len(src_item.text()) if src_item else 0
        tgt_len = len(tgt_item.text()) if tgt_item else 0
        # 粗略估算：每 ~25 字符换一行
        lines = max((max(src_len, tgt_len) // 25) + 1, 1)
        self.glossary_table.setRowHeight(row, max(32, lines * 24))

    def _add_glossary(self) -> None:
        row = self.glossary_table.rowCount()
        self.glossary_table.insertRow(row)
        self.glossary_table.setItem(row, 0, QTableWidgetItem(""))
        self.glossary_table.setItem(row, 1, QTableWidgetItem(""))
        self.glossary_table.setRowHeight(row, 32)
        # 直接在表格里编辑，选中第一格
        self.glossary_table.editItem(self.glossary_table.item(row, 0))

    def _delete_glossary(self) -> None:
        row = self.glossary_table.currentRow()
        if row < 0:
            return
        src_item = self.glossary_table.item(row, 0)
        if src_item and self._analysis:
            src = src_item.text().strip()
            self._analysis.glossary.pop(src, None)
        self.glossary_table.removeRow(row)
        self._emit_modified()

    # ------------------------------------------------------------------
    #  风格指南
    # ------------------------------------------------------------------

    def _populate_style(self) -> None:
        if self._analysis:
            self.style_edit.blockSignals(True)
            self.style_edit.setPlainText(self._analysis.style_guide)
            self.style_edit.blockSignals(False)

    def _on_style_changed(self) -> None:
        if self._analysis:
            self._analysis.style_guide = self.style_edit.toPlainText()
            self._emit_modified()

    # ------------------------------------------------------------------
    #  同步
    # ------------------------------------------------------------------

    def _sync_from_ui(self) -> None:
        if not self._analysis:
            return

        # 话题分段 — 从 UI 重建，避免重复累积
        for i in range(self.topic_tree.topLevelItemCount()):
            item = self.topic_tree.topLevelItem(i)
            if i < len(self._analysis.topic_segments):
                seg = self._analysis.topic_segments[i]
                # 读回纯话题文本（UserRole+1），避免把显示前缀 "00:00~05:00  "
                # 写回 topic 字段污染注入提示词；兼容无 UserRole 数据的旧节点
                topic = item.data(0, _TOPIC_TEXT_ROLE)
                if not isinstance(topic, str):
                    topic = _strip_topic_prefix(seg, item.text(0))
                seg["topic"] = topic
                # 重建 key_terms（替换而非追加）
                terms = []
                for j in range(item.childCount()):
                    child = item.child(j)
                    if child.text(0).strip():
                        terms.append(child.text(0).strip())
                if terms:
                    seg["key_terms"] = terms

        # 术语表
        new_glossary: dict[str, str] = {}
        for row in range(self.glossary_table.rowCount()):
            src_item = self.glossary_table.item(row, 0)
            tgt_item = self.glossary_table.item(row, 1)
            src = src_item.text().strip() if src_item else ""
            tgt = tgt_item.text().strip() if tgt_item else ""
            if src:
                new_glossary[src] = tgt
        self._analysis.glossary = new_glossary
        self._analysis.style_guide = self.style_edit.toPlainText()

    def _emit_modified(self) -> None:
        self._sync_from_ui()
        if self._analysis:
            self.analysis_modified.emit(self._analysis)

    # ------------------------------------------------------------------
    #  确认 / 跳过
    # ------------------------------------------------------------------

    def _on_optimize_glossary(self) -> None:
        """用户点击深度优化术语"""
        self._sync_from_ui()
        self.optimize_gloss_btn.setEnabled(False)
        self.optimize_gloss_btn.setText("优化中…")
        if self._analysis:
            self.optimize_glossary.emit(self._analysis)

    def on_glossary_optimized(self, optimized: dict[str, str]) -> None:
        """外部调用：LLM 优化完成后更新术语表"""
        if self._analysis:
            self._analysis.glossary = optimized
            self._populate_glossary()
            self._emit_modified()
        self.optimize_gloss_btn.setEnabled(True)
        self.optimize_gloss_btn.setText("深度优化术语")

    def _on_approve(self) -> None:
        self._sync_from_ui()
        self.approve_btn.setEnabled(False)
        self.skip_btn.setEnabled(False)
        self.waiting_hint.setVisible(False)
        if self._analysis:
            self.approve_clicked.emit(self._analysis)

    def _on_skip(self) -> None:
        self.approve_btn.setEnabled(False)
        self.skip_btn.setEnabled(False)
        self.waiting_hint.setVisible(False)
        self.approve_clicked.emit(None)

    def _on_approve_all(self) -> None:
        """批量确认：同步当前面板修改后交给主窗口处理"""
        self._sync_from_ui()
        self.approve_btn.setEnabled(False)
        self.skip_btn.setEnabled(False)
        self.approve_all_btn.setEnabled(False)
        self.waiting_hint.setVisible(False)
        self.approve_all_clicked.emit()
