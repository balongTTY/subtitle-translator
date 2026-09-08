"""Win11 主题管理器 — 明暗切换、QSS 加载、自定义背景"""

import logging
from pathlib import Path

import darkdetect
from PyQt5.QtCore import QObject, pyqtSignal, Qt
from PyQt5.QtWidgets import QApplication
from PyQt5.QtGui import QColor, QPalette, QBrush, QPixmap

log = logging.getLogger("subtitle_translator")


class ThemeManager(QObject):
    """Win11 主题管理单例"""

    theme_changed = pyqtSignal(str)  # "light" | "dark"

    _instance: "ThemeManager | None" = None

    def __new__(cls) -> "ThemeManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        super().__init__()
        self._initialized = True
        self._current_theme = ""
        self._applied = False  # 主题是否已真正应用到全局（区别于仅检测）
        self._style_dir = (
            Path(__file__).parent.parent / "resources" / "styles"
        )

    @property
    def current_theme(self) -> str:
        if not self._current_theme:
            self._detect_system_theme()
        return self._current_theme

    def _detect_system_theme(self) -> str:
        # 把检测结果写回 _current_theme：apply_theme 之前读取也能拿到真实值，
        # 否则分析面板在 __init__ 阶段拿到 "" 走浅色分支（审计 #61）
        try:
            self._current_theme = "dark" if darkdetect.isDark() else "light"
        except Exception:
            self._current_theme = "light"
        return self._current_theme

    def apply_theme(self, theme: str | None = None) -> None:
        """应用主题到全局"""
        if theme is None:
            theme = self._detect_system_theme()

        # 仅当主题真正应用过一次后才做相同主题去重；
        # _detect_system_theme 已写 _current_theme，首次 apply 时不能提前 return
        if theme == self._current_theme and self._applied:
            return

        self._current_theme = theme
        self._applied = True

        qss_path = self._style_dir / f"theme_{theme}.qss"
        if qss_path.exists():
            qss = qss_path.read_text(encoding="utf-8")
            QApplication.instance().setStyleSheet(qss)
            log.info("已应用主题: %s", theme)
        else:
            log.warning("主题文件不存在: %s", qss_path)

        self.theme_changed.emit(theme)

    # ------------------------------------------------------------------
    #  自定义背景
    # ------------------------------------------------------------------

    def apply_custom_background(
        self,
        color: str = "",
        opacity: int = 100,
        image_path: str = "",
        _called_from_clear: bool = False,
    ) -> None:
        """应用自定义背景色/图片/透明度到主窗口"""
        from config.settings import AppSettings

        if not _called_from_clear:
            s = AppSettings()
            if not color:
                color = s.bg_color
            if opacity == 100 and s.bg_opacity != 100:
                opacity = s.bg_opacity
            if not image_path:
                image_path = s.bg_image

        app = QApplication.instance()
        if not app:
            return

        # 窗口透明度
        for widget in app.topLevelWidgets():
            try:
                widget.setWindowOpacity(opacity / 100.0)
            except Exception:
                pass

        # 背景：用 palette（QSS 不再全局设 QWidget background，palette 可生效）
        for widget in app.topLevelWidgets():
            if not hasattr(widget, 'centralWidget'):
                continue
            cw = widget.centralWidget()
            if not cw:
                continue

            cw.setAutoFillBackground(True)
            p = cw.palette()

            if image_path and Path(image_path).exists():
                pixmap = QPixmap(image_path)
                if not pixmap.isNull():
                    scaled = pixmap.scaled(
                        cw.size() or widget.size(),
                        Qt.KeepAspectRatioByExpanding,
                        Qt.SmoothTransformation,
                    )
                    p.setBrush(QPalette.Window, QBrush(scaled))
            elif color:
                p.setColor(QPalette.Window, QColor(color))
            else:
                # 恢复默认
                cw.setAutoFillBackground(False)
                cw.setPalette(app.style().standardPalette())
                continue

            cw.setPalette(p)

    def clear_custom_background(self) -> None:
        """重置自定义背景"""
        self.apply_custom_background(color="", opacity=100, image_path="", _called_from_clear=True)
