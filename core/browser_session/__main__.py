"""``python -m core.browser_session`` —— 模块只读自检入口。

等价于 ``python -m core.browser_session.registry``，但**不会**触发
"module found in sys.modules" 的 RuntimeWarning（包与子模块同名冲突）。

自检只做只读探测：查 User Data 目录、浏览器内核、驱动库与加密依赖，
**不启动浏览器、不写任何文件**。
"""

from __future__ import annotations

import sys

from core.browser_session.registry import selftest

if __name__ == "__main__":  # pragma: no cover - 手动执行的入口
    sys.exit(selftest())
