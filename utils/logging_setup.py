"""日志接入helper：让各模块以统一格式取到可用的 ``logging.Logger``。

背景
----
框架里存在两套日志通道：

- **界面日志**：``ui`` 层把消息写入界面日志控件（回调 / 队列），不走 logging
- **库日志**：``core`` 层的模块（如 :mod:`core.ebook_fetcher`）用 ``logging``，
  并在「进程未配置 logging」时自带一个 ``StreamHandler``，保证被脚本直接调用
  时日志依然可见

本模块把后者那几行兜底代码收敛为 :func:`get_logger`，新增模块统一调用即可，
避免每个文件各写一份 handler 装配逻辑（也避免重复添加 handler 导致日志翻倍）。

约定：``propagate`` 保持默认的 True —— 若使用方（如 ``logging.basicConfig``
或 ``dictConfig``）已配置根 logger，消息会照常上传；只有在「根 logger 没有任何
handler」时才补一个 StreamHandler，此时把 ``propagate`` 置 False 以避免重复输出。
"""

from __future__ import annotations

import logging

#: 与既有模块（core.ebook_fetcher）一致的日志格式
LOG_FORMAT = "[%(asctime)s] %(levelname)s %(name)s: %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

#: 已由本模块装配过兜底 handler 的 logger 名，避免重复添加
_configured: set[str] = set()


def get_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    """返回指定名称的 logger，必要时装配兜底控制台 handler。

    :param name: logger 名称（约定用 ``模块全名``，如 ``core.browser_session.cookie_store``）
    :param level: 兜底 handler 生效时的日志级别
    """
    logger = logging.getLogger(name)
    if name in _configured or logger.handlers:
        return logger
    # 根 logger 已配置（如 basicConfig / dictConfig）：交给它输出即可，不重复加 handler
    if logging.getLogger().handlers:
        return logger
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT))
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    _configured.add(name)
    return logger


__all__ = ["LOG_FORMAT", "LOG_DATEFMT", "get_logger"]
