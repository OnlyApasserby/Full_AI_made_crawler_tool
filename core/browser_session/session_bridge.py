"""SessionBridge 子模块：把浏览器登录态桥接到其它请求层。

三种请求层，一份登录态
----------------------
::

    ┌──────────────────────── 浏览器（已登录）────────────────────────┐
    │  driver.get_cookies() / cookie_url                             │
    └───────────────┬───────────────────────────────┬───────────────┘
                    │                               │
        get_requests_session()              get_playwright_context()
                    │                               │
             requests.Session                 持久化 BrowserContext
          （Cookie + UA + 代理 + 头）        （同一份 User Data / Cookie）

设计要点
--------
- **指纹一致**：从浏览器**实时读取** ``navigator.userAgent`` / ``navigator.language``
  等，写入 requests 的默认请求头，避免"浏览器是 Chrome/120、requests 是
  python-requests"这种一眼假的组合
- **不重复登录**：requests 层直接用浏览器已登录的 Cookie，而不是重新走登录流程
- **可降级**：浏览器不可用时可只用 CookieStore 加载 Cookie 构造 Session
  （此时没有实时 UA，回退到框架 UA 池）
"""

from __future__ import annotations

from dataclasses import dataclass

from core.browser_session.anti_detect import proxy_for_requests
from core.browser_session.config import SessionConfig
from core.browser_session.cookie_store import CookieStore, cookies_to_dict
from core.browser_session.exceptions import LoginStateError
from utils.logging_setup import get_logger

logger = get_logger("core.browser_session.session_bridge")

#: 浏览器请求头里**不适合**固化到 requests 会话上的字段：
#: 逐跳头由 requests 自己管理；Referer / Origin 是"逐请求"语义，
#: 固化进会话会导致每个请求都带着错误的来源页（反而更像机器人）。
_SKIP_HEADERS = ("host", "content-length", "connection", "accept-encoding",
                 "referer", "origin")


@dataclass
class PlaywrightHandoff:
    """``get_playwright_context()`` 的返回结构。

    ``context`` 就是浏览器当前正在使用的**持久化上下文**：在其中新建页面
    （``new_page()``）会直接继承已登录的 Cookie 与 User Data，无需再注入。
    """

    context: object
    driver: object = None
    user_agent: str = ""
    cookie_count: int = 0

    def new_page(self):
        """在已登录的上下文里新建页面。"""
        return self.context.new_page()

    def close(self) -> None:
        """关闭该上下文（同时会关闭其底层浏览器）。"""
        try:
            self.context.close()
        except Exception:
            pass


class SessionBridge:
    """浏览器 ↔ requests / playwright 的桥接器。"""

    def __init__(self, config: SessionConfig, driver=None, store: CookieStore | None = None):
        self.config = config
        self.driver = driver
        self.store = store or CookieStore(config, driver)
        self._session = None
        self._headers: dict | None = None

    # ------------------------------------------------------------------
    # Cookie / 请求头
    # ------------------------------------------------------------------
    def cookies(self) -> list[dict]:
        """当前登录态的规范化 Cookie 列表（优先取浏览器实时值）。"""
        if self.driver is not None:
            try:
                self.store.export_from_driver(self.driver)
            except Exception as exc:
                logger.warning("从浏览器读取 Cookie 失败，退回已存储的 Cookie：%s", exc)
        return list(self.store.cookies)

    def cookie_dict(self) -> dict:
        """Cookie 的 ``{name: value}`` 形式。"""
        return cookies_to_dict(self.cookies())

    def headers(self, refresh: bool = False) -> dict:
        """与浏览器指纹一致的请求头（实时读取并缓存）。"""
        if self._headers is not None and not refresh:
            return dict(self._headers)
        headers: dict = {}
        if self.driver is not None:
            try:
                headers = self.driver.request_headers() or {}
            except Exception as exc:
                logger.debug("读取浏览器请求头失败，使用默认请求头：%s", exc)
        if not headers.get("User-Agent"):
            headers["User-Agent"] = self.config.user_agent or self._fallback_user_agent()
        headers.setdefault("Accept-Language", self.config.locale or "zh-CN")
        headers.setdefault("Accept", ("text/html,application/xhtml+xml,application/xml;"
                                      "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"))
        self._headers = {key: value for key, value in headers.items()
                         if key and value and key.lower() not in _SKIP_HEADERS}
        return dict(self._headers)

    @staticmethod
    def _fallback_user_agent() -> str:
        """没有浏览器可用时的兜底 UA（取框架 UA 池的第一项，固定而非随机）。"""
        try:
            from utils.user_agents import UserAgentPool
            return UserAgentPool().all()[0]
        except Exception:
            return "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " \
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

    # ------------------------------------------------------------------
    # requests 层
    # ------------------------------------------------------------------
    def get_requests_session(self, *, headers: dict | None = None,
                             verify_ssl: bool = True):
        """构造带登录态与一致指纹的 :class:`requests.Session`。

        Cookie 用 :class:`requests.cookies.RequestsCookieJar` 注入（保留
        domain/path/secure 属性，由 requests 自行决定是否发送），而不是简单
        拍平成字典——后者会把第三方域 Cookie 也塞给所有请求。

        :raises LoginStateError: 没有任何可用 Cookie（调用方应先导出/加载登录态）
        """
        try:
            import requests
        except ImportError as exc:
            raise LoginStateError("需要 requests 才能构造请求会话：pip install requests") from exc

        cookies = self.cookies()
        if not cookies:
            raise LoginStateError(
                "没有可用的 Cookie：请先启动带登录态的浏览器并导出一次"
                "（load_browser_session → export_cookies），或先用 load_cookies 载入")

        session = requests.Session()
        session.cookies = self.store.as_cookie_jar(self.config.domain)
        session.headers.update(self.headers())
        if headers:
            session.headers.update({k: v for k, v in headers.items() if v})
        if self.config.proxy:
            server = self.config.proxy_server()
            if server:
                session.proxies = {"http": server, "https": server}
        session.verify = bool(verify_ssl)
        self._session = session
        logger.info("已构建 requests 会话：%d 条 Cookie，UA=%s",
                    len(cookies), session.headers.get("User-Agent", "")[:60])
        return session

    # ------------------------------------------------------------------
    # playwright 层
    # ------------------------------------------------------------------
    def get_playwright_context(self) -> PlaywrightHandoff:
        """返回浏览器当前正在使用的 Playwright 持久化上下文。

        :raises LoginStateError: 当前没有运行中的 Playwright 浏览器
        """
        context = getattr(self.driver, "context", None)
        if context is None:
            raise LoginStateError(
                "当前没有运行中的 Playwright 浏览器上下文。\n"
                "请用 launcher='playwright' 启动浏览器（load_browser_session），"
                "或改用 get_requests_session() 走静态请求层；"
                "若只需要 Cookie，请用 cookie_store。")
        cookies = self.cookies()          # 先同步一次，保证计数是最新的
        return PlaywrightHandoff(context=context, driver=self.driver,
                                 user_agent=self.headers().get("User-Agent", ""),
                                 cookie_count=len(cookies))

    # ------------------------------------------------------------------
    # 框架抓取层
    # ------------------------------------------------------------------
    def create_fetcher(self, mode: str = "static", **kwargs):
        """构造框架的抓取器（:mod:`core.fetcher`），带上登录态与一致指纹。

        - 静态模式：``RequestsFetcher`` + 浏览器 UA / Cookie / 代理
        - 动态模式：``PlaywrightFetcher``（它自建浏览器实例，**不共用**本模块的
          User Data；若需要共用请直接用 ``get_playwright_context()``）

        :param mode: ``static`` / ``dynamic`` / ``auto``
        """
        from core.fetcher import create_fetcher
        from utils.user_agents import UserAgentPool

        headers = self.headers()
        ua = headers.get("User-Agent", "")
        merged = {key: value for key, value in headers.items()
                  if key.lower() != "user-agent"}
        merged.update(kwargs.pop("extra_headers", {}) or {})
        fetcher = create_fetcher(
            mode,
            user_agent=ua,
            ua_pool=UserAgentPool([ua] if ua else None),
            proxy={},
            extra_headers=merged,
            **kwargs)
        if mode != "dynamic":
            # 先与浏览器同步一次 Cookie，再挂到抓取器的会话上
            cookies = self.cookies()
            if cookies:
                try:
                    fetcher.set_cookie_jar(self.store.as_cookie_jar(self.config.domain))
                    fetcher.set_proxy(proxy_for_requests(self.config))
                except Exception as exc:
                    logger.debug("把 Cookie/代理注入抓取器会话失败（不影响其它能力）：%s", exc)
        return fetcher

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def close(self) -> None:
        """关闭桥接过程中创建的 requests 会话（不关闭浏览器）。"""
        if self._session is not None:
            try:
                self._session.close()
            finally:
                self._session = None

    def __enter__(self) -> "SessionBridge":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


__all__ = ["PlaywrightHandoff", "SessionBridge"]
