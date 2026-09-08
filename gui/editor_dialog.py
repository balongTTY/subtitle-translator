"""字幕条目编辑对话框"""

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QTextEdit, QPushButton, QFormLayout,
)

from core.translation_batch import TranslationResult


class EditorDialog(QDialog):
    """逐条编辑字幕翻译"""

    def __init__(
        self, result: TranslationResult, parent=None
    ) -> None:
        super().__init__(parent)
        self.result = result
        self._setup_ui()
        self._load_data()

    def _setup_ui(self) -> None:
        self.setWindowTitle("编辑字幕")
        self.setMinimumSize(600, 300)
        layout = QVBoxLayout(self)

        form = QFormLayout()

        self.index_label = QLabel()
        form.addRow("编号:", self.index_label)

        self.original_text = QTextEdit()
        self.original_text.setReadOnly(True)
        self.original_text.setMaximumHeight(80)
        form.addRow("原文:", self.original_text)

        self.translated_text = QTextEdit()
        self.translated_text.setMaximumHeight(80)
        form.addRow("译文:", self.translated_text)

        self.status_label = QLabel()
        form.addRow("状态:", self.status_label)

        self.issues_label = QLabel()
        # 用 QSS 类而非硬编码颜色
        self.issues_label.setObjectName("warningLabel")
        form.addRow("问题:", self.issues_label)

        layout.addLayout(form)

        # 按钮
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        save_btn = QPushButton("保存")
        save_btn.clicked.connect(self._on_save)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(save_btn)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)

    def _load_data(self) -> None:
        # 1 起始显示，与预览表格「#」列及「第 N 条」状态提示口径一致
        self.index_label.setText(str(self.result.index + 1))
        self.original_text.setPlainText(self.result.original_text)
        self.translated_text.setPlainText(self.result.translated_text)
        self.status_label.setText(self.result.status)
        if self.result.qc_issues:
            self.issues_label.setText(", ".join(self.result.qc_issues))

    def _on_save(self) -> None:
        self.result.translated_text = self.translated_text.toPlainText()
        self.result.status = "manually_edited"
        self.result.qc_issues = []
        self.accept()
