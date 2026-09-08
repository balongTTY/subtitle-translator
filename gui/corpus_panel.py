"""语料库管理面板"""

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QPushButton, QLineEdit,
)

from core.corpus_manager import CorpusManager


class _TermCellItem(QTableWidgetItem):
    """记录编辑前的旧文本，供 _on_cell_changed 识别被改写的源术语键（审计 #65）

    QTableModel 提交编辑时经 setData(DisplayRole) 写入新值，这里在写入前
    捕获旧值；编辑源术语列时据此删除旧键，避免语料库累积过期条目。
    """

    def __init__(self, text: str = "") -> None:
        super().__init__(text)
        self._prev_text = text

    def setData(self, role: int, value) -> None:
        if role == Qt.DisplayRole:
            self._prev_text = self.text()
        super().setData(role, value)


class CorpusPanel(QWidget):
    """术语库管理面板"""

    corpus_changed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._corpus = CorpusManager()
        self._setup_ui()
        self._populate()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        header = QLabel("术语库（强制译法）")
        header.setStyleSheet("font-size: 11pt; font-weight: bold;")
        layout.addWidget(header)

        hint = QLabel("在此定义的翻译规则优先级高于 AI 分析结果")
        hint.setStyleSheet("color: #888; font-size: 8pt;")
        layout.addWidget(hint)

        # 搜索
        search_layout = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("搜索术语…")
        self.search_input.textChanged.connect(self._apply_filter)
        search_layout.addWidget(self.search_input)
        layout.addLayout(search_layout)

        # 表格
        self.table = QTableWidget()
        self.table.setColumnCount(2)
        self.table.setHorizontalHeaderLabels(["源术语", "译法（双击编辑）"])
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.cellChanged.connect(self._on_cell_changed)
        layout.addWidget(self.table)

        # 按钮
        btn_layout = QHBoxLayout()
        add_btn = QPushButton("+ 添加")
        add_btn.clicked.connect(self._add_term)
        del_btn = QPushButton("- 删除")
        del_btn.clicked.connect(self._delete_term)
        import_btn = QPushButton("从分析导入")
        import_btn.setToolTip("将当前分析结果的术语表合并到语料库")
        self.import_btn = import_btn  # 主窗口负责接线：点击时把分析面板的 glossary 合并进来
        import_btn.clicked.connect(self._emit_changed)

        btn_layout.addWidget(add_btn)
        btn_layout.addWidget(del_btn)
        btn_layout.addWidget(import_btn)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

    # ---- 数据 ----

    @property
    def corpus(self) -> CorpusManager:
        return self._corpus

    def get_terms(self) -> dict[str, str]:
        return self._corpus.get_all()

    def _populate(self) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        terms = self._corpus.get_all()
        filter_text = self.search_input.text().strip().lower()

        rows = [
            (src, tgt) for src, tgt in terms.items()
            if (not filter_text or filter_text in src.lower() or filter_text in tgt.lower())
        ]
        self.table.setRowCount(len(rows))
        for i, (src, tgt) in enumerate(rows):
            self.table.setItem(i, 0, _TermCellItem(src))
            self.table.setItem(i, 1, _TermCellItem(tgt))
            self.table.setRowHeight(i, 26)

        self.table.blockSignals(False)

    def _apply_filter(self) -> None:
        self._populate()

    def _on_cell_changed(self, row: int, col: int) -> None:
        src = self.table.item(row, 0)
        tgt = self.table.item(row, 1)
        if not src or not tgt:
            return
        old_src = getattr(src, "_prev_text", None)
        new_src = src.text().strip()
        new_tgt = tgt.text().strip()
        # 编辑源术语列：旧键已失效，先删除再写入，避免旧键残留（审计 #65）
        if col == 0 and old_src and old_src != new_src:
            self._corpus.remove(old_src)
        if new_src and new_tgt:
            self._corpus.add(new_src, new_tgt)
        self.corpus_changed.emit()

    def _add_term(self) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, _TermCellItem(""))
        self.table.setItem(row, 1, _TermCellItem(""))
        self.table.setRowHeight(row, 26)
        self.table.editItem(self.table.item(row, 0))

    def _delete_term(self) -> None:
        row = self.table.currentRow()
        if row >= 0:
            src_item = self.table.item(row, 0)
            if src_item:
                self._corpus.remove(src_item.text())
            self.table.removeRow(row)
            self.corpus_changed.emit()

    def import_from_glossary(self, glossary: dict[str, str]) -> None:
        """将 glossary 中的条目合并到语料库（不覆盖已有）

        批量合并后一次 update_all 落盘，避免每个术语都全量写一次文件（审计 #79）
        """
        existing = self._corpus.get_all()
        merged = dict(existing)
        for term, translation in glossary.items():
            if term not in merged:
                merged[term] = translation
        self._corpus.update_all(merged)
        self._populate()
        self.corpus_changed.emit()

    def _emit_changed(self) -> None:
        self.corpus_changed.emit()
