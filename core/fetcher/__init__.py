"""core.fetcher —— 统一抓取层（静态 requests / 动态 playwright）。

上层只需依赖 :class:`~core.fetcher.base.BaseFetcher` 的 ``fetch()`` /
``fetch_json()`` 契约，即可在"快而不全"（静态）与"全而更重"（渲染）之间切换。

``RENDER_AUTO`` 的处理方式
--------------------------
"自动"由编排层（``core/media_crawler.py``）负责，不在这里隐藏复杂度：
先静态抓取 → 评估媒体产出是否过少 → 再升级为浏览器渲染。
本模块只提供"按模式创建"与"可用性探测"两个原子能力。
"""

from __future__ import annotations

from core.fetcher import browser_pool, chrome_setup
from core.fetcher.base import (
    DEFAULT_LOCALE, DEFAULT_VIEWPORT, FORWARD_HEADER_KEYS, MODE_AUTO,
    MODE_DYNAMIC, MODE_STATIC, BaseFetcher, FetchContext, FetchError,
    FetcherUnavailable, NetworkEvent, decode_bytes, guess_encoding,
    interruptible_sleep,
)
from core.fetcher.playwright_fetcher import NetworkRecorder, PlaywrightFetcher
from core.fetcher.requests_fetcher import RequestsFetcher


def playwright_status(force: bool = False, auto_extract: bool = True,
                      progress=None) -> tuple:
    """探测 playwright 浏览器是否可用（必要时解压本地离线内核包）。

    :return: (是否可用, 不可用原因)；原因文本已包含安装指引，可直接展示给用户
    """
    return browser_pool.probe(force=force, auto_extract=auto_extract,
                              progress=progress)


def prepare_browser(auto_extract: bool = True, progress=None) -> tuple:
    """准备浏览器内核，返回 (可执行文件路径, 失败原因)。

    与 :func:`playwright_status` 的区别：本函数只做"查找/解压"这类轻量工作，
    不启动浏览器，适合界面初始化时预检（解压 200MB 压缩包耗时，需给用户提示）。
    """
    return chrome_setup.ensure_executable(auto_extract=auto_extract,
                                          progress=progress)


def create_fetcher(mode: str = MODE_STATIC, **kwargs) -> BaseFetcher:
    """按渲染模式创建抓取器实例。

    :param mode: ``static`` / ``dynamic`` / ``auto``
        - ``static``：requests 快速路径
        - ``dynamic``：Chromium 渲染 + 网络监听；不可用时抛 :class:`FetcherUnavailable`
        - ``auto``：返回静态抓取器（是否升级为渲染由调用方按产出判断）
    :raises FetcherUnavailable: 请求动态模式但浏览器不可用
    """
    mode = (mode or MODE_STATIC).lower()
    if mode == MODE_DYNAMIC:
        available, reason = playwright_status()
        if not available:
            raise FetcherUnavailable(reason)
        return PlaywrightFetcher(**kwargs)
    return RequestsFetcher(**kwargs)


def supports_dynamic() -> bool:
    """当前环境是否具备动态渲染能力（不抛异常，供 UI 决定控件可用性）。"""
    return playwright_status()[0]


__all__ = [
    # 基类与数据结构
    "BaseFetcher", "FetchContext", "NetworkEvent", "FetchError",
    "FetcherUnavailable", "MODE_STATIC", "MODE_DYNAMIC", "MODE_AUTO",
    "DEFAULT_LOCALE", "DEFAULT_VIEWPORT", "FORWARD_HEADER_KEYS",
    "decode_bytes", "guess_encoding", "interruptible_sleep",
    # 实现
    "RequestsFetcher", "PlaywrightFetcher", "NetworkRecorder",
    # 工厂与探测
    "create_fetcher", "playwright_status", "supports_dynamic",
    "prepare_browser", "browser_pool", "chrome_setup",
]
