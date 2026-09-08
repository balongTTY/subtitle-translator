"""应用入口"""

import sys

# Windows 原生 DLL 冲突修复：ctranslate2（faster-whisper 推理后端）与 Qt5 的
# 运行时库加载顺序不兼容——若 Qt5Core 先加载，ctranslate2 构造 whisper 模型时
# 会直接段错误（0xC0000005）。必须在导入任何 PyQt5 模块之前预导入 ctranslate2。
# 未安装 faster-whisper（纯字幕翻译用法）时静默跳过。
try:
    import ctranslate2  # noqa: F401
except Exception:
    pass

from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import Qt

from utils.logger import setup_logging

def main() -> int:
    logger = setup_logging()

    # 全局异常钩子：windowed exe（console=False）里未捕获异常的 traceback
    # 会写进 PyInstaller 的 NullWriter 静默消失，用户只看到闪退；至少让它进日志
    def _excepthook(exc_type, exc, tb):
        logger.error("未捕获异常", exc_info=(exc_type, exc, tb))
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = _excepthook

    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setApplicationName("字幕翻译工具")

    # 延迟导入，避免循环依赖
    from gui.main_window import MainWindow
    window = MainWindow()
    window.show()

    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
