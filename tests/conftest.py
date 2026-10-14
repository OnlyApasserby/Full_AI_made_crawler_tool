"""pytest 配置：把项目根目录加入 ``sys.path``，并注册自定义标记。

- 测试同时兼容 ``pytest`` 与标准库 ``unittest``：
  ``python -m unittest discover -s tests`` 亦可运行（集成测试会被自动跳过）
- ``integration`` 标记的用例需要**真实浏览器 / 真实网络**，默认不选：
  ``pytest tests -m "not integration"``
"""

from __future__ import annotations

import os
import sys

# 把项目根目录加入导入路径（tests 目录不在包内，需要手动补）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def pytest_configure(config) -> None:
    """注册本模块使用的自定义标记（与 unittest 运行方式无关）。"""
    config.addinivalue_line(
        "markers", "integration: 需要真实浏览器 / 真实网络的集成测试（默认不选）")
