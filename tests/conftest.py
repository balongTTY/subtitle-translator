"""pytest 共享配置：把项目根目录加入 sys.path，保证顶层包可导入。

核心包（core / services / config / utils）以项目根为顶层命名空间，
本文件在收集测试前把项目根插入 sys.path 最前，并统一 UTF-8 输出
（与 tests/e2e/ 约定一致，避免 Windows GBK 控制台打印 CJK 崩溃）。
所有测试均为纯单元级，mock LLM，不发起任何网络请求。
"""

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 无显示环境下 QtCore 调用可安全运行（AppSettings 用 QSettings，不需要 QPA 平台）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# 测试会打印中文断言信息，统一 UTF-8 输出
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
