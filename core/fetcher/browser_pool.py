"""Playwright 浏览器生命周期管理（**线程局部**）。

关键约束
--------
``playwright.sync_api`` 不是线程安全的：它必须在**创建它的那个线程内**
``start()`` 与 ``stop()``。因此这里把 playwright / browser / context 挂在
``threading.local()`` 上，做到"每线程一套实例"，工作线程结束时由
:func:`release` 显式关闭，避免浏览器进程泄漏。

成本与限流
----------
每个 Chromium 实例约占 150–250MB 内存，因此调用方（``manager/task_manager``）
需按 ``config.DEFAULT_MEDIA_RENDER_WORKERS`` 限制并发渲染任务数，
超限时降级为静态抓取。
"""

from __future__ import annotations

import threading

from core.fetcher import chrome_setup
from core.fetcher.base import (
    DEFAULT_LOCALE, DEFAULT_VIEWPORT, FetcherUnavailable,
)

# 最常被反爬脚本检测的特征：抹除 navigator.webdriver、补齐 languages/plugins
_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || {runtime: {}};
"""

# 启动参数：关闭"自动化控制"标志并避免后台标签被降频（懒加载依赖滚动与事件）
_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
]

_HINT = ("两种准备方式（任选其一）：\n"
         "  1) 把 Chrome for Testing 压缩包放到项目根目录（如 chrome-win64.zip），"
         "程序会自动解压并使用\n"
         "  2) 联网下载官方内核：playwright install chromium\n"
         "（也可用环境变量 CRAWLER_CHROME_PATH 直接指定 chrome.exe 路径）")

_state = threading.local()
_error_lock = threading.Lock()
#: 进程级缓存的启动失败原因（避免每次请求都重复尝试启动浏览器）
_launch_error = ""
#: 进程级缓存的可用性探测结果
_probe_cache: tuple | None = None


def _proxy_from_config(proxy: dict) -> dict | None:
    """requests 风格代理配置 → playwright 的 proxy 结构。"""
    if not proxy:
        return None
    server = proxy.get("https") or proxy.get("http") or proxy.get("all") or ""
    if not server:
        return None
    config = {"server": server}
    username = proxy.get("username") or ""
    password = proxy.get("password") or ""
    if username:
        config["username"] = username
        config["password"] = password
    return config


class BrowserHandle:
    """线程内的浏览器句柄（playwright + browser + context）。"""

    def __init__(self, playwright, browser, context, profile_key: tuple):
        self.playwright = playwright
        self.browser = browser
        self.context = context
        self.profile_key = profile_key
        self._closed = False

    def alive(self) -> bool:
        """底层连接是否仍然可用。"""
        if self._closed:
            return False
        try:
            return bool(self.browser.is_connected())
        except Exception:
            return False

    def new_page(self):
        """在本上下文内新建页面（继承 Cookie 与 UA 等上下文设置）。"""
        return self.context.new_page()

    def close(self) -> None:
        """依次关闭 context → browser → playwright，任何一步失败都不影响后续。"""
        if self._closed:
            return
        self._closed = True
        for closer in (self.context.close, self.browser.close, self.playwright.stop):
            try:
                closer()
            except Exception:
                continue


def _close_current() -> None:
    handle = getattr(_state, "handle", None)
    if handle is not None:
        handle.close()
        _state.handle = None


def _friendly_launch_error(exc: Exception) -> str:
    """把 playwright 的启动异常翻译为可操作的提示。"""
    text = str(exc)
    low = text.lower()
    if "executable doesn't exist" in low or "playwright install" in low:
        return f"未找到可用的浏览器内核。\n{_HINT}"
    if "host system is missing dependencies" in low:
        return ("系统缺少 Chromium 运行依赖库，请安装后重试。\n"
                "（Linux 可执行：playwright install-deps chromium）")
    if "target page, context or browser has been closed" in low:
        return "浏览器已被关闭（可能是上一次停止操作），请重试"
    return f"启动浏览器失败：{text[:200]}"


def acquire(*, user_agent: str = "", locale: str = DEFAULT_LOCALE,
            viewport: tuple = DEFAULT_VIEWPORT, headless: bool = True,
            ignore_https_errors: bool = True, proxy: dict | None = None,
            executable_path: str = "", auto_extract: bool = True,
            progress=None) -> BrowserHandle:
    """获取当前线程的浏览器句柄（不存在或配置不一致时创建）。

    浏览器内核优先使用本地离线包（项目根目录的 ``chrome-win64.zip`` 自动解压，
    见 :mod:`core.fetcher.chrome_setup`），其次才回退到 playwright 官方内核。

    :raises FetcherUnavailable: playwright 未安装、浏览器内核缺失或启动失败
    """
    global _launch_error
    if _launch_error:
        raise FetcherUnavailable(_launch_error)

    # 定位浏览器可执行文件（必要时解压根目录的离线内核包）
    executable = executable_path
    if not executable:
        executable, reason = chrome_setup.ensure_executable(
            auto_extract=auto_extract, progress=progress)
        if not executable:
            message = f"{reason}。\n{_HINT}"
            with _error_lock:
                _launch_error = message
            raise FetcherUnavailable(message)

    proxy_config = _proxy_from_config(proxy)
    profile_key = (user_agent, locale, tuple(viewport), headless,
                   ignore_https_errors, executable,
                   (proxy_config or {}).get("server", ""))

    handle = getattr(_state, "handle", None)
    if handle is not None:
        # 配置未变化且连接仍正常：直接复用（保留登录态与 Cookie）
        if handle.alive() and handle.profile_key == profile_key:
            return handle
        _close_current()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # 依赖缺失
        message = ("未安装 playwright，请执行：pip install playwright\n" + _HINT)
        with _error_lock:
            _launch_error = message
        raise FetcherUnavailable(message) from exc

    playwright = None
    try:
        playwright = sync_playwright().start()
        launch_kwargs = {"headless": headless, "args": list(_LAUNCH_ARGS)}
        if executable:
            launch_kwargs["executable_path"] = executable
        if proxy_config:
            launch_kwargs["proxy"] = proxy_config
        browser = playwright.chromium.launch(**launch_kwargs)
        context_kwargs = {
            "locale": locale,
            "viewport": {"width": int(viewport[0]), "height": int(viewport[1])},
            "ignore_https_errors": bool(ignore_https_errors),
        }
        if user_agent:
            context_kwargs["user_agent"] = user_agent
        context = browser.new_context(**context_kwargs)
        context.add_init_script(_STEALTH_JS)
        handle = BrowserHandle(playwright, browser, context, profile_key)
        _state.handle = handle
        return handle
    except FetcherUnavailable:
        raise
    except Exception as exc:  # 启动失败：记录原因并释放已创建的资源
        message = _friendly_launch_error(exc)
        with _error_lock:
            _launch_error = message
        try:
            if playwright is not None:
                playwright.stop()
        except Exception:
            pass
        raise FetcherUnavailable(message) from exc


def release() -> None:
    """关闭当前线程的浏览器句柄（工作线程退出前调用）。"""
    _close_current()


def probe(force: bool = False, auto_extract: bool = True, progress=None) -> tuple:
    """探测 playwright 与浏览器内核是否可用（结果进程级缓存）。

    :param auto_extract: 未找到内核时是否自动解压项目根目录的离线包
    :return: (是否可用, 不可用原因)
    """
    global _probe_cache
    if _probe_cache is not None and not force:
        return _probe_cache
    try:
        handle = acquire(auto_extract=auto_extract, progress=progress)
    except FetcherUnavailable as exc:
        _probe_cache = (False, str(exc))
        return _probe_cache
    try:
        handle.context.new_page().close()
    except Exception as exc:
        _probe_cache = (False, f"浏览器不可用：{str(exc)[:200]}")
    else:
        _probe_cache = (True, "")
    finally:
        release()
    return _probe_cache


def reset_error() -> None:
    """清空启动失败缓存（补齐浏览器内核后无需重启进程即可重试）。"""
    global _launch_error, _probe_cache
    with _error_lock:
        _launch_error = ""
    _probe_cache = None
    chrome_setup.reset_cache()


__all__ = ["BrowserHandle", "acquire", "release", "probe", "reset_error"]
