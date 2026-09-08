"""日志配置"""

import logging
import logging.handlers
import sys
from pathlib import Path


def setup_logging(log_dir: Path | None = None) -> logging.Logger:
    """配置 rotating file + console 日志

    日志目录创建/文件打开失败（权限、杀软锁定、盘符不存在）不抛出——
    main() 在 QApplication 之前调用本函数，抛异常的话 windowed exe
    表现为「双击没反应」且无任何日志可查。降级为仅控制台日志继续运行。
    """
    logger = logging.getLogger("subtitle_translator")
    logger.setLevel(logging.DEBUG)
    # 不向 root logger 冒泡：宿主程序配置过 root handler 时避免双份输出
    logger.propagate = False

    if logger.handlers:
        return logger

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 控制台 handler（windowed exe 的 sys.stderr 为 None → NullHandler 兜底）
    if sys.stderr is not None:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(fmt)
        logger.addHandler(console_handler)
    else:
        logger.addHandler(logging.NullHandler())

    try:
        if log_dir is None:
            log_dir = Path.home() / "AppData" / "Roaming" / "SubtitleTranslator" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        # 文件 handler: 最多保留 5 个 2MB 的日志文件
        # delay=True：首次写日志才打开文件，目录被锁时不至于构造即崩
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / "translator.log",
            maxBytes=2 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
            delay=True,
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)
    except OSError as e:
        logger.warning("日志文件初始化失败，降级为仅控制台日志: %s", e)

    return logger
