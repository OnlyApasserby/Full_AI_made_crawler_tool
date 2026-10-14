"""驱动适配层：Selenium 与 Playwright 通过适配器实现**同一套**接口。

契约
----
:class:`BrowserDriver` 是 CookieStore / SessionBridge / LoginStateChecker 唯一
依赖的接口。新增一种自动化实现（如 undetected-chromedriver、CDP 直连）只需再写
一个适配器并注册到 :data:`LAUNCHER_ADAPTERS`，上层代码不必改动。

实现要点
--------
- **Selenium**：``webdriver.Chrome`` + ``chrome_options``；Cookie 用 ``get_cookies()`` /
  ``add_cookie()``；导航 ``get()`` / 刷新 ``refresh()``
- **Playwright**：``launch_persistent_context``（**持久化上下文**才能真正复用
  User Data 目录），Cookie 用 ``context.cookies()`` / ``context.add_cookies()``；
  页面从 ``context.pages`` 里取或新建

关于"进程占用"的检测
--------------------
两个适配器在启动前都会调用注入的 ``presence_check(user_data_dir, profile_dir)``
（由 :mod:`.launcher` 传入），命中即抛 :class:`BrowserAlreadyRunning`——
这是本模块最关键的**显式失败**点：Chromium 对同一 ``--user-data-dir`` 是独占的，
静默转交给已有实例会让自动化拿到一个"没有控制权的空壳"。
"""

from __future__ import annotations

import importlib.util
import threading
from abc import ABC, abstractmethod

from core.browser_session.config import SessionConfig
from core.browser_session.exceptions import (
    BrowserAlreadyRunning, BrowserLaunchFailed, LauncherNotAvailable,
)
from utils.logging_setup import get_logger

logger = get_logger("core.browser_session.adapters")

#: 启动参数：关闭"自动化控制"标志（降低被识别为爬虫的概率）
BASE_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-dev-shm-usage",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
]

#: 使用真实 Profile 时必须**移除**的参数：它们会让浏览器忽略 User Data
#: （Chrome 对二者互斥，带上会出现"登录态为空"的诡异现象）
CONFLICTING_ARGS = (
    "--incognito", "--headless", "--user-data-dir", "--profile-directory",
    "--disable-extensions",
)


def normalize_proxy(proxy: dict | None) -> dict:
    """把多种写法的代理配置归一为 ``{server, username, password}``。

    支持 ``{"https": "http://ip:port"}``（框架既有写法）、
    ``{"server": "...", "username": ..., "password": ...}``（playwright 写法）
    以及纯字符串。
    """
    if not proxy:
        return {}
    if isinstance(proxy, str):
        text = proxy.strip()
        return {"server": text} if text else {}
    server = ""
    for key in ("server", "https", "http", "all"):
        value = proxy.get(key)
        if value and str(value).strip():
            server = str(value).strip()
            break
    if not server:
        return {}
    out = {"server": server}
    for key in ("username", "password"):
        value = proxy.get(key)
        if value and str(value).strip():
            out[key] = str(value).strip()
    return out


class BrowserDriver(ABC):
    """自动化驱动的统一接口（Selenium / Playwright 适配器共同实现）。"""

    name = "base"

    def __init__(self, config: SessionConfig):
        self.config = config
        self._closed = False
        self._lock = threading.RLock()

    # ---------- 页面导航 ----------
    @abstractmethod
    def get(self, url: str) -> None:
        """导航到 URL。"""

    @abstractmethod
    def current_url(self) -> str:
        """当前页面 URL。"""

    @abstractmethod
    def page_source(self) -> str:
        """当前页面 HTML 源码。"""

    @abstractmethod
    def refresh(self) -> None:
        """刷新当前页面。"""

    def title(self) -> str:
        """页面标题（适配器可选实现，默认空串）。"""
        return ""

    def text(self) -> str:
        """页面可见文本（适配器可选实现，默认取源码）。"""
        return self.page_source()

    # ---------- Cookie（字节/值的读写交由 .cookie_store 做规范化） ----------
    @abstractmethod
    def get_cookies(self) -> list[dict]:
        """读取当前浏览器上下文的全部 Cookie。"""

    @abstractmethod
    def add_cookies(self, cookies: list[dict]) -> tuple[int, list[str]]:
        """注入 Cookie，返回 ``(成功数, 失败原因列表)``。"""

    @abstractmethod
    def delete_all_cookies(self) -> None:
        """清空当前上下文的 Cookie。"""

    def cookie_url(self) -> str:
        """当前可用于 Cookie 注入的基准 URL。"""
        return self.current_url() or self.config.login_url or ""

    # ---------- 请求头与指纹 ----------
    def user_agent(self) -> str:
        """当前真实 UA（用于让 requests 层与浏览器指纹一致）。"""
        return self.config.user_agent

    def request_headers(self) -> dict:
        """浏览器当前的常见请求头（requests 层复用，保证指纹一致）。"""
        headers = {
            "Accept-Language": self.config.locale or "zh-CN",
            "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                       "image/avif,image/webp,image/apng,*/*;q=0.8"),
            "Upgrade-Insecure-Requests": "1",
        }
        ua = self.user_agent()
        if ua:
            headers["User-Agent"] = ua
        return headers

    # ---------- 行为拟人化与元素探测（可选能力） ----------
    def execute_script(self, script: str, *args):
        """执行 JS（适配器可选实现，不支持时抛 :class:`NotImplementedError`）。"""
        raise NotImplementedError

    def wait_for_selector(self, selector: str, timeout: float = 10.0) -> bool:
        """等待选择器出现，出现返回 True（适配器可选实现）。"""
        raise NotImplementedError

    def human_click(self, selector: str, delay_range: tuple = (0.4, 1.1)) -> bool:
        """以拟人化轨迹点击元素（适配器可选实现）。"""
        raise NotImplementedError

    def human_type(self, selector: str, text: str, delay_range: tuple = (0.05, 0.2)) -> bool:
        """以拟人化节奏输入文本（适配器可选实现）。"""
        raise NotImplementedError

    # ---------- 生命周期 ----------
    @abstractmethod
    def quit(self) -> None:
        """关闭浏览器并释放资源（可重复调用）。"""

    def alive(self) -> bool:
        """底层会话是否仍然可用。"""
        return not self._closed

    def kind(self) -> str:
        """返回 ``selenium`` / ``playwright``（由子类覆盖）。"""
        return self.name

    def __enter__(self) -> "BrowserDriver":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.quit()


class SeleniumDriver(BrowserDriver):
    """Selenium 适配器（``selenium>=4.6``：驱动由 Selenium Manager 自动获取）。"""

    name = "selenium"

    def __init__(self, driver, config: SessionConfig):
        super().__init__(config)
        self.driver = driver
        #: 上一次鼠标位置（让连续点击的轨迹从"当前位置"出发，而非每次回到原点）
        self._cursor: tuple = (0.0, 0.0)

    # ---------- 页面导航 ----------
    def get(self, url: str) -> None:
        self.driver.get(url)

    def current_url(self) -> str:
        try:
            return self.driver.current_url or ""
        except Exception:
            return ""

    def page_source(self) -> str:
        try:
            return self.driver.page_source or ""
        except Exception:
            return ""

    def refresh(self) -> None:
        self.driver.refresh()

    def title(self) -> str:
        try:
            return self.driver.title or ""
        except Exception:
            return ""

    def text(self) -> str:
        try:
            return self.driver.find_element("tag name", "body").text or ""
        except Exception:
            return self.page_source()

    # ---------- Cookie ----------
    def get_cookies(self) -> list[dict]:
        return [dict(item) for item in (self.driver.get_cookies() or [])]

    def add_cookies(self, cookies: list[dict]) -> tuple[int, list[str]]:
        ok, errors = 0, []
        for cookie in cookies:
            try:
                self.driver.add_cookie(cookie)
                ok += 1
            except Exception as exc:  # InvalidCookieDomainException / 字段非法等
                errors.append(f"{cookie.get('name', '?')}@{cookie.get('domain', '?')}："
                              f"{type(exc).__name__}: {str(exc)[:120]}")
        return ok, errors

    def delete_all_cookies(self) -> None:
        self.driver.delete_all_cookies()

    # ---------- 指纹 ----------
    def user_agent(self) -> str:
        ua = self.execute_script("return navigator.userAgent;")
        return str(ua) if ua else super().user_agent()

    def request_headers(self) -> dict:
        headers = super().request_headers()
        script = ("return {lang: navigator.language, "
                  "platform: navigator.platform, "
                  "ua: navigator.userAgent, "
                  "w: window.innerWidth, h: window.innerHeight};")
        try:
            info = self.execute_script(script) or {}
        except Exception:
            return headers
        if info.get("lang"):
            headers["Accept-Language"] = str(info["lang"])
        if info.get("ua"):
            headers["User-Agent"] = str(info["ua"])
        if info.get("platform"):
            headers["Sec-CH-UA-Platform"] = f'"{info["platform"]}"'
        return headers

    # ---------- JS 与拟人化操作 ----------
    def execute_script(self, script: str, *args):
        return self.driver.execute_script(script, *args)

    def wait_for_selector(self, selector: str, timeout: float = 10.0) -> bool:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait
        try:
            WebDriverWait(self.driver, float(timeout)).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, selector)))
            return True
        except Exception:
            return False

    def _element_center(self, selector: str):
        from selenium.webdriver.common.by import By
        element = self.driver.find_element(By.CSS_SELECTOR, selector)
        rect = self.execute_script(
            "const r = arguments[0].getBoundingClientRect();"
            "return {x: r.left + r.width / 2, y: r.top + r.height / 2};", element)
        return element, float(rect.get("x", 0)), float(rect.get("y", 0))

    def human_click(self, selector: str, delay_range: tuple = (0.4, 1.1)) -> bool:
        from core.browser_session.anti_detect import (
            bezier_path, human_scroll, rand_delay,
        )
        try:
            element, x, y = self._element_center(selector)
            rand_delay(*delay_range)
            human_scroll(self)
            start = self._cursor
            for px, py in bezier_path(start, (x, y), steps=self.config.mouse_steps):
                self.driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                    "type": "mouseMoved", "x": int(px), "y": int(py)})
            self._cursor = (x, y)
            rand_delay(0.05, 0.25)
            element.click()
            return True
        except Exception:
            return False

    def human_type(self, selector: str, text: str, delay_range: tuple = (0.05, 0.2)) -> bool:
        from core.browser_session.anti_detect import rand_delay
        from selenium.webdriver.common.by import By
        try:
            element = self.driver.find_element(By.CSS_SELECTOR, selector)
            element.click()
            for char in str(text):
                element.send_keys(char)
                rand_delay(*delay_range)
            return True
        except Exception:
            return False

    # ---------- 生命周期 ----------
    def quit(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self.driver.quit()
            except Exception:
                pass

    def alive(self) -> bool:
        if self._closed:
            return False
        try:
            _ = self.driver.current_url
            return True
        except Exception:
            return False


class PlaywrightDriver(BrowserDriver):
    """Playwright 适配器（``launch_persistent_context`` 持久化上下文）。"""

    name = "playwright"

    def __init__(self, playwright, context, config: SessionConfig):
        super().__init__(config)
        self.playwright = playwright
        self.context = context
        self._page = None

    # ---------- 页面 ----------
    def page(self):
        """返回当前页面（优先复用上下文里已打开的页面）。"""
        if self._page is not None:
            try:
                if not self._page.is_closed():
                    return self._page
            except Exception:
                pass
        try:
            pages = [item for item in self.context.pages if not item.is_closed()]
        except Exception:
            pages = []
        self._page = pages[0] if pages else self.context.new_page()
        return self._page

    def get(self, url: str) -> None:
        page = self.page()
        page.goto(url, wait_until="domcontentloaded",
                  timeout=float(self.config.timeout) * 1000)

    def current_url(self) -> str:
        try:
            return self.page().url or ""
        except Exception:
            return ""

    def page_source(self) -> str:
        try:
            return self.page().content() or ""
        except Exception:
            return ""

    def refresh(self) -> None:
        try:
            self.page().reload(wait_until="domcontentloaded",
                               timeout=float(self.config.timeout) * 1000)
        except Exception:
            pass

    def title(self) -> str:
        try:
            return self.page().title() or ""
        except Exception:
            return ""

    def text(self) -> str:
        try:
            return self.page().inner_text("body") or ""
        except Exception:
            return self.page_source()

    # ---------- Cookie ----------
    def get_cookies(self) -> list[dict]:
        return [dict(item) for item in (self.context.cookies() or [])]

    def add_cookies(self, cookies: list[dict]) -> tuple[int, list[str]]:
        ok, errors = 0, []
        for cookie in cookies:
            try:
                self.context.add_cookies([cookie])
                ok += 1
            except Exception as exc:
                errors.append(f"{cookie.get('name', '?')}@{cookie.get('domain', '?')}："
                              f"{type(exc).__name__}: {str(exc)[:120]}")
        return ok, errors

    def delete_all_cookies(self) -> None:
        self.context.clear_cookies()

    # ---------- JS 与拟人化操作 ----------
    def execute_script(self, script: str, *args):
        return self.page().evaluate(script, *args)

    def wait_for_selector(self, selector: str, timeout: float = 10.0) -> bool:
        try:
            self.page().wait_for_selector(selector, timeout=float(timeout) * 1000)
            return True
        except Exception:
            return False

    def human_click(self, selector: str, delay_range: tuple = (0.4, 1.1)) -> bool:
        from core.browser_session.anti_detect import (
            bezier_path, human_scroll, rand_delay,
        )
        try:
            page = self.page()
            box = page.locator(selector).first.bounding_box()
            rand_delay(*delay_range)
            human_scroll(self)
            if box:
                start = (0.0, 0.0)
                target = (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                for px, py in bezier_path(start, target, steps=self.config.mouse_steps):
                    page.mouse.move(float(px), float(py))
            rand_delay(0.05, 0.25)
            page.locator(selector).first.click(timeout=float(self.config.timeout) * 1000)
            return True
        except Exception:
            return False

    def human_type(self, selector: str, text: str, delay_range: tuple = (0.05, 0.2)) -> bool:
        try:
            target = self.page().locator(selector).first
            target.click()
            target.press_sequentially(str(text), delay=float(delay_range[0]) * 1000)
            return True
        except Exception:
            return False

    # ---------- 生命周期 ----------
    def quit(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for closer in (self.context.close, self.playwright.stop):
                try:
                    closer()
                except Exception:
                    continue

    def alive(self) -> bool:
        if self._closed:
            return False
        try:
            return bool(self.context.pages is not None)
        except Exception:
            return False


#: 驱动实现名 → 启动函数（由 :mod:`.launcher` 填充，避免循环导入）
LAUNCHER_ADAPTERS: dict[str, object] = {}


def register_launcher(name: str, launcher) -> bool:
    """注册一个启动器函数（重复注册或非法名称返回 False）。"""
    key = str(name or "").strip().lower()
    if not key or not callable(launcher) or key in LAUNCHER_ADAPTERS:
        return False
    LAUNCHER_ADAPTERS[key] = launcher
    return True


def _module_available(name: str) -> bool:
    """探测可选依赖是否可导入（不真正导入，避免无谓的副作用）。"""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _assert_launcher_available(launcher: str) -> None:
    """驱动库缺失时给出带安装指引的异常（属于环境问题，不重试）。"""
    if launcher == "selenium":
        if not _module_available("selenium"):
            raise LauncherNotAvailable(
                "未安装 selenium，请执行：pip install selenium\n"
                "（Selenium 4.6+ 自带 Selenium Manager，会自动获取 chromedriver；"
                "首次运行需要联网，或预先配置 webdriver 到 PATH）")
    elif launcher == "playwright":
        if not _module_available("playwright"):
            raise LauncherNotAvailable(
                "未安装 playwright，请执行：pip install playwright\n"
                "本项目还会优先复用 resources/browsers 下的本地内核，"
                "无需再执行 playwright install chromium")


def _check_presence(config: SessionConfig, presence_check=None) -> None:
    """启动前检查 User Data 是否被运行中的浏览器占用。

    ``presence_check`` 由 :mod:`.launcher` 注入（签名
    ``(user_data_dir, profile_dir, browser) -> (busy, reason)``），
    这样适配层不必反向依赖 launcher（避免循环导入）。
    """
    if presence_check is None:
        return
    busy, reason = presence_check(config.user_data_dir, config.profile_dir,
                                 config.browser)
    if busy:
        raise BrowserAlreadyRunning(reason)


def _assert_headless_ok(config: SessionConfig) -> None:
    """无头 + 复用 User Data 的组合给出**显式提示**（默认不阻断，只记日志）。

    并非技术上绝对不可行，但 Chromium 会把无头实例与图形实例的 Profile 视为
    同一份，极易出现锁冲突与"Cookie 被覆盖"。使用者可用 ``impersonate=True``
    显式承担该风险。
    """
    if config.headless_mode != "off" and not config.impersonate:
        logger.warning(
            "复用 User Data 时建议使用有头模式（headless=off）：无头实例与图形实例"
            "共用同一 Profile 容易互相覆盖 Cookie。如确需无头，请设置 impersonate=True。")


# ---------------------------------------------------------------------------
# 启动实现
# ---------------------------------------------------------------------------
def launch_selenium(config: SessionConfig, presence_check=None, progress=None) -> SeleniumDriver:
    """启动 Selenium 驱动的浏览器实例（带 ``--user-data-dir`` / ``--profile-directory``）。

    :raises LauncherNotAvailable: 未安装 selenium
    :raises BrowserAlreadyRunning: User Data 正被运行中的浏览器占用
    :raises BrowserLaunchFailed: 启动失败（参数不支持 / 内核损坏等）
    """
    _assert_launcher_available("selenium")
    _check_presence(config, presence_check)

    from selenium import webdriver

    options = webdriver.EdgeOptions() if config.browser == "edge" else webdriver.ChromeOptions()
    # 复用真实 Profile：让浏览器自己去读已有的 Cookie / 登录态
    options.add_argument(f"--user-data-dir={config.user_data_dir}")
    options.add_argument(f"--profile-directory={config.profile}")
    for arg in BASE_LAUNCH_ARGS:
        options.add_argument(arg)
    if config.headless_mode != "off":
        options.add_argument(f"--headless={config.headless_mode}")
    if config.locale:
        options.add_argument(f"--lang={config.locale}")
    if config.user_agent:
        options.add_argument(f"--user-agent={config.user_agent}")
    if config.timezone:
        options.add_argument(f"--timezone={config.timezone}")
    proxy = normalize_proxy(config.proxy)
    if proxy.get("server"):
        options.add_argument(f"--proxy-server={proxy['server']}")
    if progress:
        progress(f"正在启动 {config.browser}（Selenium，Profile={config.profile}）…")

    try:
        if config.browser == "edge":
            driver = webdriver.Edge(options=options)
        else:
            if config.executable_path:
                # Selenium 4 通过 binary_location 指定内核，避免被 Selenium Manager 覆盖
                options.binary_location = config.executable_path
            driver = webdriver.Chrome(options=options)
        if config.timeout:
            driver.set_page_load_timeout(float(config.timeout))
    except Exception as exc:
        raise _translate_launch_error(exc, config) from exc
    return SeleniumDriver(driver, config)


def launch_playwright(config: SessionConfig, presence_check=None, progress=None) -> PlaywrightDriver:
    """启动 Playwright **持久化上下文**（复用 User Data 目录）。

    :raises LauncherNotAvailable: 未安装 playwright
    :raises BrowserAlreadyRunning: User Data 正被运行中的浏览器占用
    :raises BrowserLaunchFailed: 启动失败
    """
    _assert_launcher_available("playwright")
    _check_presence(config, presence_check)

    from playwright.sync_api import sync_playwright

    _assert_headless_ok(config)
    if progress:
        progress(f"正在启动 {config.browser}（Playwright 持久化上下文，"
                 f"Profile={config.profile}）…")

    playwright = None
    try:
        playwright = sync_playwright().start()
        executable = config.executable_path or _resolve_playwright_executable(config)
        args = list(BASE_LAUNCH_ARGS) + [f"--profile-directory={config.profile}"]
        if config.locale:
            args.append(f"--lang={config.locale}")
        if config.timezone:
            args.append(f"--timezone={config.timezone}")
        launch_kwargs = {
            "user_data_dir": config.user_data_dir,
            "headless": config.headless_mode != "off",
            "args": args,
            "locale": config.locale or "zh-CN",
            "viewport": {"width": int(config.viewport[0]), "height": int(config.viewport[1])},
            "ignore_https_errors": True,
            "no_viewport": False,
        }
        if executable:
            launch_kwargs["executable_path"] = executable
        if config.user_agent:
            launch_kwargs["user_agent"] = config.user_agent
        proxy = normalize_proxy(config.proxy)
        if proxy.get("server"):
            launch_kwargs["proxy"] = proxy
        context = playwright.chromium.launch_persistent_context(**launch_kwargs)
        if config.anti_detect:
            from core.browser_session.anti_detect import stealth_script
            context.add_init_script(stealth_script(config))
        return PlaywrightDriver(playwright, context, config)
    except Exception as exc:
        try:
            if playwright is not None:
                playwright.stop()
        except Exception:
            pass
        raise _translate_launch_error(exc, config) from exc


def _resolve_playwright_executable(config: SessionConfig) -> str:
    """定位浏览器内核：复用框架既有的查找链（resources/browsers → 系统 → PATH）。"""
    try:
        from core.fetcher import chrome_setup
        found = chrome_setup.find_executable()
        if found:
            return found
    except Exception:
        pass
    return ""


def _translate_launch_error(exc: Exception, config: SessionConfig) -> Exception:
    """把驱动层的原始异常翻译成**可操作**的自定义异常。"""
    text = str(exc)
    low = text.lower()
    if "user data directory is already in use" in low or "profile appears to be in use" in low:
        return BrowserAlreadyRunning(
            f"User Data 目录正被占用：{config.user_data_dir}\n"
            "请先**完全退出**该浏览器（Windows 需在任务管理器结束所有 chrome.exe / "
            "msedge.exe 残留进程）后重试。")
    if "unable to obtain driver" in low or "driver_location" in low \
            or ("chromedriver" in low and "not found" in low):
        return BrowserLaunchFailed(
            "Selenium 未能获取浏览器驱动（chromedriver / msedgedriver）。\n"
            "Selenium 4.6+ 通常会自动下载驱动，但**需要联网**；离线环境的处理方式：\n"
            "  1) 把 chromedriver(.exe) 放到 PATH 或浏览器内核同目录；\n"
            "  2) 改用 Playwright 驱动（本模块默认，通过 --user-data-dir 复用同一份\n"
            "     User Data，无需额外驱动）：launcher='playwright' 或去掉 --selenium；\n"
            "  3) 或为 Selenium Manager 配置代理 / 离线镜像后重试。")
    if "executable doesn't exist" in low or "no such file" in low or "cannot find" in low:
        return BrowserLaunchFailed(
            f"未找到可用的浏览器内核（{config.browser}）。\n"
            "可任选其一：1) 用 CRAWLER_CHROME_PATH 指定 chrome.exe；"
            "2) 把 chrome-win64.zip 放到项目根目录；3) 安装 Chrome/Edge。")
    if "display" in low and ("error" in low or "cannot open" in low):
        return BrowserLaunchFailed(
            f"启动失败（无图形显示环境？）：{text[:200]}\n"
            "Linux 服务器上需要一个虚拟显示（如 xvfb-run）才能复用登录态"
            "（登录态与无头模式不相容）。")
    if "session not created" in low and "version" in low:
        return BrowserLaunchFailed(
            f"浏览器与驱动版本不匹配：{text[:200]}\n"
            "请执行：pip install -U selenium（Selenium Manager 会自动匹配驱动版本），"
            "或改用 Playwright 驱动。")
    return BrowserLaunchFailed(f"启动浏览器失败：{text[:300]}")


#: 默认启动器注册（导入本模块即完成，供 :func:`create_driver` 使用）
register_launcher("selenium", launch_selenium)
register_launcher("playwright", launch_playwright)


def create_driver(config: SessionConfig, launcher: str = "",
                  presence_check=None, progress=None) -> BrowserDriver:
    """按配置启动浏览器并返回统一驱动对象。

    :param launcher: 覆盖 ``config.launcher``（``selenium`` / ``playwright``）
    :raises ValueError: 未注册的驱动实现
    """
    name = (launcher or config.launcher or "playwright").strip().lower()
    factory = LAUNCHER_ADAPTERS.get(name)
    if factory is None:
        raise ValueError(
            f"未注册的驱动实现：{name!r}，可选：{', '.join(sorted(LAUNCHER_ADAPTERS))}")
    return factory(config, presence_check=presence_check, progress=progress)


__all__ = [
    "BASE_LAUNCH_ARGS", "CONFLICTING_ARGS", "LAUNCHER_ADAPTERS",
    "BrowserDriver", "SeleniumDriver", "PlaywrightDriver",
    "normalize_proxy", "register_launcher", "create_driver",
    "launch_selenium", "launch_playwright",
]
