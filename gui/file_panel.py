"""文件列表面板 — 拖拽添加、状态图标"""

import os
from pathlib import Path

from PyQt5.QtCore import Qt, pyqtSignal, QEvent
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QTreeWidget, QTreeWidgetItem,
    QHeaderView, QAbstractItemView, QPushButton, QHBoxLayout,
    QLabel,
)

from utils.constants import SUPPORTED_FORMATS, MEDIA_FORMATS


class FilePanel(QWidget):
    """文件列表面板"""

    files_changed = pyqtSignal(list)  # 文件路径列表变化

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # 按钮栏
        btn_layout = QHBoxLayout()
        self.add_btn = QPushButton("添加文件")
        self.add_btn.setToolTip("添加字幕文件（SRT/ASS/VTT）或视频/音频文件")
        self.add_btn.clicked.connect(self._on_add_files)
        remove_btn = QPushButton("移除选中")
        remove_btn.setToolTip("从列表移除选中的文件（不影响磁盘文件）")
        remove_btn.clicked.connect(self._on_remove_selected)
        self.clear_btn = QPushButton("清空列表")
        self.clear_btn.clicked.connect(self._on_clear)
        btn_layout.addWidget(self.add_btn)
        btn_layout.addWidget(remove_btn)
        btn_layout.addWidget(self.clear_btn)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        # 文件树
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["文件名", "格式", "状态", "进度"])
        self.tree.setRootIsDecorated(False)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setAlternatingRowColors(True)
        self.tree.header().setStretchLastSection(True)
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.tree.header().setSectionResizeMode(3, QHeaderView.ResizeToContents)

        layout.addWidget(self.tree)

        # 空状态提示（列表无文件时覆盖显示）
        self._empty_hint = QLabel(
            "拖入字幕 / 视频文件开始\n\n字幕：SRT · ASS · VTT → 直接翻译\n视频/音频：MP4 · MKV · MP3 … → 先提取字幕", self.tree
        )
        self._empty_hint.setAlignment(Qt.AlignCenter)
        self._empty_hint.setWordWrap(True)
        self._empty_hint.setStyleSheet("color: #888; font-size: 10pt; border: none; background: transparent;")
        self._empty_hint.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.tree.installEventFilter(self)
        self._update_empty_hint()

        # 拖拽支持
        self.setAcceptDrops(True)

    def eventFilter(self, obj, event) -> bool:
        """文件树尺寸变化时同步空状态提示位置"""
        if obj is self.tree and event.type() == QEvent.Resize:
            self._reposition_empty_hint()
        return super().eventFilter(obj, event)

    def _reposition_empty_hint(self) -> None:
        if self._empty_hint.isVisible():
            self._empty_hint.setGeometry(self.tree.viewport().geometry())

    def _update_empty_hint(self) -> None:
        empty = self.tree.topLevelItemCount() == 0
        self._empty_hint.setVisible(empty)
        if empty:
            self._reposition_empty_hint()
            self._empty_hint.raise_()

    def _on_add_files(self) -> None:
        from PyQt5.QtWidgets import QFileDialog
        all_filters = "字幕与媒体文件 ("
        for ext in list(SUPPORTED_FORMATS) + list(MEDIA_FORMATS):
            all_filters += f" *{ext}"
        all_filters += ")"
        sub_filters = "字幕文件 ("
        for ext, name in SUPPORTED_FORMATS.items():
            sub_filters += f" *{ext}"
        sub_filters += ")"
        media_filters = "媒体文件 ("
        for ext in MEDIA_FORMATS:
            media_filters += f" *{ext}"
        media_filters += ")"
        filters = f"{all_filters};;{sub_filters};;{media_filters};;所有文件 (*.*)"

        files, _ = QFileDialog.getOpenFileNames(
            self, "选择文件", "", filters
        )
        if files:
            self.add_files(files)

    def _on_clear(self) -> None:
        self.tree.clear()
        self._update_empty_hint()
        self.files_changed.emit([])

    def _on_remove_selected(self) -> None:
        """移除选中的文件行"""
        items = self.tree.selectedItems()
        if not items:
            return
        root = self.tree.invisibleRootItem()
        for item in items:
            root.removeChild(item)
        self._update_empty_hint()
        self.files_changed.emit(self.get_filepaths())

    def add_files(self, filepaths: list[str]) -> None:
        """添加文件到列表（字幕文件可直接翻译；视频/音频待提取）"""
        existing = self.get_filepaths()
        new_added = False
        for fp in filepaths:
            if fp not in existing:
                ext = Path(fp).suffix.lower()
                if ext in MEDIA_FORMATS:
                    format_name, status = MEDIA_FORMATS[ext], "待提取"
                else:
                    format_name, status = SUPPORTED_FORMATS.get(ext, ext.upper()), "待翻译"
                item = QTreeWidgetItem()
                item.setText(0, Path(fp).name)
                item.setToolTip(0, fp)
                item.setText(1, format_name)
                item.setText(2, status)
                item.setText(3, "")
                item.setData(0, Qt.UserRole, fp)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(0, Qt.Checked)
                self.tree.addTopLevelItem(item)
                new_added = True
        if new_added:
            self._update_empty_hint()
            self.files_changed.emit(self.get_filepaths())

    def get_filepaths(self) -> list[str]:
        """获取所有文件路径"""
        paths = []
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            fp = item.data(0, Qt.UserRole)
            if fp:
                paths.append(fp)
        return paths

    def get_checked_filepaths(self) -> list[str]:
        """获取勾选的文件路径"""
        paths = []
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            if item.checkState(0) == Qt.Checked:
                fp = item.data(0, Qt.UserRole)
                if fp:
                    paths.append(fp)
        return paths

    def update_file_status(self, filepath: str, status: str, progress: str = "") -> None:
        """更新文件状态。progress 为空串时清空进度列——终态（完成/失败）不能
        残留上一轮的「3/5」之类中间进度"""
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            if item.data(0, Qt.UserRole) == filepath:
                item.setText(2, status)
                item.setText(3, progress)
                return

    # ---- 拖拽支持 ----

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            files = []
            for url in event.mimeData().urls():
                path = url.toLocalFile()
                ext = Path(path).suffix.lower()
                if ext in SUPPORTED_FORMATS or ext in MEDIA_FORMATS:
                    files.append(path)
            if files:
                self.add_files(files)
                event.acceptProposedAction()
