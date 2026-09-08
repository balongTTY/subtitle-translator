"""进度条组件"""

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QProgressBar,
    QLabel, QPushButton, QSizePolicy,
)


class ProgressWidget(QWidget):
    """翻译进度条 + 状态标签 + 取消按钮"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.status_label = QLabel("就绪")
        self.status_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)

        self.progress_bar = QProgressBar()
        self.progress_bar.setMinimum(0)
        self.progress_bar.setMaximum(100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("%v%")
        self.progress_bar.setMinimumWidth(200)

        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.setObjectName("dangerBtn")
        self.cancel_btn.setFixedWidth(60)
        self.cancel_btn.setEnabled(False)

        layout.addWidget(self.status_label)
        layout.addWidget(self.progress_bar, 1)
        layout.addWidget(self.cancel_btn)

    def set_progress(self, current: int, total: int, status: str = "") -> None:
        """设置进度"""
        if total > 0:
            pct = int(current / total * 100)
            self.progress_bar.setValue(pct)
            self.progress_bar.setFormat(f"{current}/{total} (%p%)")
        else:
            self.progress_bar.setValue(0)
            self.progress_bar.setFormat("...")

        if status:
            self.status_label.setText(status)
            # 状态区宽度有限，长文本（如完整文件名）被截断时 tooltip 可看全文
            self.status_label.setToolTip(status)

    def set_running(self, running: bool) -> None:
        self.cancel_btn.setEnabled(running)
        if not running:
            self.progress_bar.setValue(0)
            self.progress_bar.setFormat("")
            self.status_label.setText("就绪")

    def reset(self) -> None:
        self.set_running(False)
