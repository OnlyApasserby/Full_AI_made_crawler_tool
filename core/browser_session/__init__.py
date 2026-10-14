"""core.browser_session —— 浏览器登录态复用模块。

一句话说明
----------
让抓取流程直接复用**使用者本人**本地浏览器已登录的会话（User Data / Cookie），
从而跳过登录步骤，并降低被风控误判的概率。

子模块（每个职责单一、可单独测试）
----------------------------------
============================  ==================================================
:mod:`.config`               配置结构 + 路径自动探测（Win/macOS/Linux 的 Chrome/Edge）
:mod:`.browser_launcher`     (:mod:`.launcher`) 启动带 User Data 的浏览器实例
:mod:`.cookie_store`         Cookie 导出 / 加密持久化 / 加载 / 注入
:mod:`.session_bridge`       把登录态桥接到 requests / Playwright / 框架抓取层
:mod:`.login_state`          登录态有效性检查与失效重登入口
:mod:`.anti_detect`          指纹一致性与行为拟人化（**不实现任何破解逻辑**）
:mod:`.registry`             按框架约定注册模块并暴露生命周期钩子
============================  ==================================================

统一入口
--------
::

    from core.browser_session import load_browser_session

    handle = load_browser_session({"domain": "example.com"})   # 用本地 Chrome 登录态
    try:
        driver = handle.get_driver()
        print(handle.check_login())            # 已登录 → True
        handle.export_cookies("resources/sessions/example.json")
        session = handle.get_requests_session()  # requests 复用同一登录态
        print(session.get("https://example.com/me").status_code)
    finally:
        handle.close()

接口约定（与具体浏览器实现解耦）
--------------------------------
:class:`SessionHandle` 对调用方只暴露：``get_driver()`` /
``get_requests_session()`` / ``check_login()`` / ``export_cookies(path)`` /
``load_cookies(path)`` / ``close()``。Selenium 与 Playwright 通过
:mod:`.adapters` 实现**同一套**驱动接口，上层代码无需区分。

合规边界（请务必阅读）
----------------------
本模块**仅**用于复用使用者本人已登录的会话，服务于合法授权的自动化场景：

- **不**实现绕过验证码、破解加密、伪造他人身份等任何功能
- :mod:`.anti_detect` 只做指纹一致性与行为拟人化，**不承诺绕过风控审核**，
  仅用于降低被误判为机器人的概率
- 目标站点若明确禁止自动化访问，请使用者**自行确认合规性**后再使用；
  模块不判断、也无法判断某个站点是否允许抓取

Cookie 等同账号凭据，导出的文件默认加密（见 :mod:`.cookie_store`），
``resources/sessions/`` 目录不应提交到版本库。
"""

from __future__ import annotations

import os

from core.browser_session import (
    anti_detect, cookie_store, launcher, login_state, session_bridge,
)
from core.browser_session.adapters import (
    BrowserDriver, PlaywrightDriver, SeleniumDriver, create_driver,
    register_launcher,
)
from core.browser_session.config import (
    SessionConfig, default_user_data_dir, detect_installed_browsers,
    list_profiles, normalize_domain, target_domains,
)
from core.browser_session.cookie_store import CookieStore
from core.browser_session.exceptions import (
    BrowserAlreadyRunning, BrowserLaunchFailed, BrowserNotInstalled,
    BrowserSessionError, CookieDecryptError, CookieError, CookieExportError,
    CookieFileInvalid, CookieFileNotFound, CookieInjectError, HeadlessNotSupported,
    LauncherNotAvailable, LoginStateError, NotLoggedIn, ProfileNotFound,
    SessionUnavailable, UserDataDirNotFound,
)
from core.browser_session.login_state import (
    CheckResult, CheckRule, LoginStateChecker,
)
from core.browser_session.registry import (
    MODULE_CORE_MODULES, MODULE_DESCRIPTORS, MODULE_NAME, MODULE_NAMESPACE,
    ModuleDescriptor, get_descriptor, lifecycle, public_api,
    register_browser_session_module, registered_modules,
)
from core.browser_session.session_bridge import PlaywrightHandoff, SessionBridge
from utils.logging_setup import get_logger

logger = get_logger("core.browser_session")

#: 模块版本
__version__ = "1.0.0"


class SessionHandle:
    """一次"带登录态的会话"的统一句柄（本模块对外的核心接口）。

    驱动是**惰性**创建的：``load_browser_session()`` 只做配置校验与路径检查，
    真正调用 ``get_driver()`` 时才启动浏览器——这样"只检查一下配置/只复用
    Cookie 发请求"的场景不必付出启动浏览器的代价。
    """

    def __init__(self, config: SessionConfig, *, driver=None,
                 store: CookieStore | None = None, autostart: bool = False):
        self.config = config
        self._driver = driver
        self._closed = False
        self.store = store or CookieStore(config, driver)
        self.bridge = SessionBridge(config, driver, self.store)
        self.checker = LoginStateChecker(config, driver, self.bridge)
        self._fetcher = None
        self.last_check: CheckResult | None = None
        self._restored_from: str = ""
        if autostart:
            self.ensure_driver()

    # ------------------------------------------------------------------
    # 基本信息
    # ------------------------------------------------------------------
    def __repr__(self) -> str:
        state = "已关闭" if self._closed else (
            f"{self._driver.kind()} 运行中" if self._driver is not None else "驱动未启动")
        return (f"<SessionHandle {self.config.browser}/{self.config.profile} "
                f"domain={self.config.domain or '未设置'} {state}>")

    @property
    def driver(self):
        """当前驱动对象（可能为 None，未启动时）。"""
        return self._driver

    def is_open(self) -> bool:
        """驱动是否处于可用状态。"""
        return self._driver is not None and not self._closed and self._driver.alive()

    def describe(self) -> str:
        """多行可读摘要（不含 Cookie 值，可安全写日志）。"""
        lines = [self.config.describe(),
                 f"驱动状态：{'运行中' if self.is_open() else '未启动'}"
                 + (f"（{self._driver.kind()}）" if self._driver is not None else "")]
        if self.store.cookies:
            lines.append(self.store.summary())
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 驱动
    # ------------------------------------------------------------------
    def ensure_driver(self):
        """确保浏览器已启动并带登录态；返回驱动对象。

        启动后会自动：注入反检测脚本（适配器内完成）→ 若已加载 Cookie
        则注入浏览器 → 首次访问目标站点（让服务端会话生效）。

        :raises BrowserAlreadyRunning: User Data 被运行中的浏览器占用
        :raises UserDataDirNotFound / ProfileNotFound / BrowserNotInstalled: 环境问题
        """
        if self._closed:
            raise SessionUnavailable("会话已关闭，请重新调用 load_browser_session() 创建")
        if self._driver is not None and self._driver.alive():
            return self._driver
        self.config.validate()      # 启动前把"路径写错"这类问题尽早暴露
        logger.info("启动带登录态的浏览器：%s / %s",
                    self.config.browser, self.config.profile)
        self._driver = launcher.launch(self.config)
        self.store.driver = self._driver
        self.bridge.driver = self._driver
        self.checker.driver = self._driver
        self._restore_into_driver()
        return self._driver

    def _restore_into_driver(self) -> None:
        """把内存里已有的 Cookie 注入新启动的浏览器（若存在）。"""
        if not self.store.cookies:
            return
        try:
            applied = self.store.inject_into_driver(self._driver)
            logger.info("已把 %d 条 Cookie 注入新会话", applied)
        except Exception as exc:
            logger.warning("把已加载的 Cookie 注入浏览器失败（可继续用浏览器自带登录态）：%s",
                           exc)

    def get_driver(self):
        """返回统一驱动对象（必要时启动浏览器）。"""
        return self.ensure_driver()

    def get_playwright_context(self):
        """返回 Playwright 持久化上下文（登录态已生效，可直接开页面）。

        :raises LoginStateError: 当前驱动不是 Playwright（或未启动）
        """
        self.ensure_driver()
        return self.bridge.get_playwright_context()

    # ------------------------------------------------------------------
    # 请求层
    # ------------------------------------------------------------------
    def get_requests_session(self, *, use_browser_cookies: bool = True):
        """返回带登录态与一致指纹的 :class:`requests.Session`。

        :param use_browser_cookies: 浏览器在运行时是否实时读取其 Cookie
            （浏览器能反映"登录态已被服务端刷新"的最新值；关闭则只用已存储的 Cookie）
        """
        if use_browser_cookies and self._driver is not None and self._driver.alive():
            try:
                self.store.export_from_driver(self._driver)
            except Exception as exc:
                logger.warning("实时读取浏览器 Cookie 失败，改用已存储的 Cookie：%s", exc)
        return self.bridge.get_requests_session()

    def get_fetcher(self, mode: str = "static", **kwargs):
        """返回框架抓取器（:mod:`core.fetcher`），已带上登录态（**可选**能力）。

        便于把本模块直接接进既有抓取/下载流水线；只需要 HTTP 能力时用
        :meth:`get_requests_session` 更轻量。
        """
        if self._fetcher is None:
            if mode in ("static", "auto") and self._driver is not None \
                    and self._driver.alive():
                try:
                    self.store.export_from_driver(self._driver)
                except Exception as exc:
                    logger.debug("导出浏览器 Cookie 失败：%s", exc)
            self._fetcher = self.bridge.create_fetcher(mode, **kwargs)
        return self._fetcher

    def fetch(self, url: str, **kwargs):
        """按框架的 ``fetch()`` 契约抓取页面（内部走 :meth:`get_fetcher`）。

        ``core.pipeline`` / ``core.media_crawler`` 等上层可直接把本句柄当作
        抓取器使用，从而"复用登录态"而不改这些模块。
        """
        return self.get_fetcher("static").fetch(url, **kwargs)

    # ------------------------------------------------------------------
    # 登录态
    # ------------------------------------------------------------------
    def check_login(self, *, channel: str = "", url: str = "") -> bool:
        """检查登录态是否有效（``True`` 表示已登录）。

        通道选择：浏览器在运行 → ``browser``；否则有 Cookie → ``http``；
        都没有 → ``cookie``（静态兜底，结果里会标注 ``fallback=True``）。
        """
        result = self.checker.check(channel=channel, url=url)
        self.last_check = result
        return bool(result.logged_in)

    def check_login_detail(self, *, channel: str = "", url: str = "") -> CheckResult:
        """同 :meth:`check_login`，但返回带判定依据的完整结果。"""
        result = self.checker.check(channel=channel, url=url)
        self.last_check = result
        return result

    def require_login(self, **kwargs) -> CheckResult:
        """要求已登录，未登录时抛 :class:`LoginStateError`（附重登步骤）。"""
        result = self.checker.require_login(**kwargs)
        self.last_check = result
        return result

    def relogin_hint(self) -> str:
        """登录态失效时给使用者看的手动重登步骤文本。"""
        return self.checker.relogin_hint(self.last_check)

    def open_login_page(self, url: str = "") -> str:
        """打开登录页，供使用者手动登录一次后重新导出 Cookie。

        本模块**不**代替使用者完成登录（更不做验证码绕过），只把窗口送到位。
        """
        driver = self.ensure_driver()
        return launcher.open_login_page(self.config, driver, url)

    # ------------------------------------------------------------------
    # Cookie
    # ------------------------------------------------------------------
    def export_cookies(self, path: str = "", *, encrypt: bool | None = None) -> str:
        """从当前浏览器导出 Cookie 并落盘，返回写入路径。

        :param path: 目标路径；留空用 ``config.cookie_store_path``
        :raises CookieInjectError: 没有可用驱动
        """
        driver = self.ensure_driver()
        self.store.export_from_driver(driver)
        return self.store.save(path, encrypt=encrypt)

    def load_cookies(self, path: str = "", *, inject: bool = True) -> int:
        """从文件载入 Cookie；浏览器在运行则立即注入，返回注入条数（未注入返回 0）。

        浏览器尚未启动时只把 Cookie 读进内存，等 :meth:`get_driver` 启动时
        自动注入（见 :meth:`_restore_into_driver`）。
        """
        self.store.load(path)
        self._restored_from = self.store.path
        if not inject:
            return 0
        if self._driver is not None and self._driver.alive():
            return self.store.inject_into_driver(self._driver)
        logger.info("Cookie 已载入内存（%d 条），将在浏览器启动时自动注入",
                    len(self.store.cookies))
        return 0

    def cookies(self) -> list[dict]:
        """当前登录态的规范化 Cookie 列表。"""
        return list(self.store.cookies)

    def cookie_summary(self) -> str:
        """Cookie 的可读摘要（不含 Cookie 值）。"""
        return self.store.summary()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def health_check(self) -> dict:
        """健康检查（生命周期钩子）：返回结构化状态。"""
        report = launcher.health_check(self.config,
                                       self._driver if self.is_open() else None)
        report["session_open"] = self.is_open()
        report["cookie_total"] = len(self.store.cookies)
        report["cookie_path"] = self.store.path
        report["last_check"] = (self.last_check.to_dict()
                               if self.last_check is not None else None)
        return report

    def close(self) -> None:
        """关闭浏览器、释放请求层资源（可重复调用，不抛异常）。"""
        if self._closed:
            return
        self._closed = True
        for closer in (self.bridge.close,):
            try:
                closer()
            except Exception:
                continue
        if self._fetcher is not None:
            try:
                self._fetcher.close()
            except Exception:
                pass
        if self._driver is not None:
            try:
                self._driver.quit()
            except Exception:
                pass
            self._driver = None

    def __enter__(self) -> "SessionHandle":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------
def load_browser_session(config: "SessionConfig | dict | None" = None, *,
                         driver=None, autostart: bool = False,
                         cookie_path: str = "", progress=None,
                         **overrides) -> SessionHandle:
    """加载一个带本地浏览器登录态的会话（本模块的**统一入口**）。

    :param config: :class:`SessionConfig`、框架配置字典，或 ``None``（全默认）
    :param driver: 复用外部已创建的驱动（如框架里已有的 Playwright 上下文）；
        传了它就不会再启动浏览器
    :param autostart: 是否立即启动浏览器（默认 False = 惰性启动）
    :param cookie_path: 顺带从该文件载入 Cookie（``cookie_only`` 场景的快捷方式）
    :param progress: 可选回调 ``progress(消息)``，把关键步骤写进界面日志
    :param overrides: 直接覆盖的配置字段（如 ``domain="example.com"``）
    :return: :class:`SessionHandle`

    :raises UserDataDirNotFound: User Data 目录不存在
    :raises ProfileNotFound: 指定 Profile 不存在
    :raises BrowserAlreadyRunning: User Data 被运行中的浏览器占用（**必须先关闭浏览器**）
    :raises ValueError: 配置取值非法（浏览器类型 / 驱动 / Cookie 策略）

    典型场景::

        # 1) 用本地已登录的 Chrome（默认惰性启动，首次取 driver 时才真正拉起）
        handle = load_browser_session({"domain": "example.com"})

        # 2) 只复用导出的 Cookie（不需要 User Data，也不需要关浏览器）
        handle = load_browser_session({"domain": "example.com"},
                                      cookie_path="resources/sessions/example.json")

        # 3) 明确要求"启动即带登录态"并立刻校验
        handle = load_browser_session({"domain": "example.com"}, autostart=True)
        assert handle.check_login()
    """
    resolved = SessionConfig.resolve(config, **overrides)
    resolved.finalize()
    # 只在"确实要用 User Data"时才校验其路径：显式给了 cookie_path 或复用了
    # 外部驱动（driver 参数）时，没装浏览器 / User Data 不存在也应能跑通
    requires_user_data = driver is None and not cookie_path
    resolved.validate(require_user_data=requires_user_data)

    if progress:
        progress(f"浏览器登录态模块已就绪：{resolved.browser} / {resolved.profile}"
                 f"（domain={resolved.domain or '未设置'}）")
    handle = SessionHandle(resolved, driver=driver, autostart=autostart)
    logger.info("load_browser_session：%s", resolved.describe().replace("\n", " | "))

    # Cookie 恢复：优先用显式路径；否则在"已有导出文件"时静默恢复上次登录态
    path = cookie_path or (resolved.cookie_store_path
                           if _store_exists(resolved) else "")
    if path:
        try:
            applied = handle.load_cookies(path, inject=driver is not None)
            if progress:
                progress(f"已从 {path} 载入 {len(handle.store.cookies)} 条 Cookie"
                         + (f"，注入浏览器 {applied} 条" if applied else ""))
        except FileNotFoundError:
            logger.info("尚未导出过 Cookie（%s），将直接复用浏览器 User Data 登录态", path)
    return handle


def _store_exists(config: SessionConfig) -> bool:
    """Cookie 存储文件是否已存在（用于"静默恢复上次登录态"）。"""
    return bool(config.cookie_store_path) and os.path.isfile(config.cookie_store_path)


def session_from_cookies(config: "SessionConfig | dict | None" = None,
                         cookie_path: str = "", **overrides) -> SessionHandle:
    """**仅用 Cookie** 的会话（不启动浏览器）——User Data 被占用时的回退方案。

    与 :func:`load_browser_session` 的区别：不要求 User Data 目录存在、不要求
    关闭浏览器，直接用导出的 Cookie 构造 requests 会话。
    """
    resolved = SessionConfig.resolve(config, **overrides).finalize()
    if not resolved.domain:
        raise ValueError("仅用 Cookie 的会话必须指定目标域名（domain）")
    handle = SessionHandle(resolved)
    handle.load_cookies(cookie_path or resolved.cookie_store_path, inject=False)
    logger.info("已创建纯 Cookie 会话：domain=%s，%d 条 Cookie",
                resolved.domain, len(handle.store.cookies))
    return handle


def __getattr__(name: str):
    """模块级 ``__getattr__``：把子模块按需暴露，避免导入顺序造成的循环依赖。"""
    if name in ("config", "cookie_store", "launcher", "session_bridge",
                "login_state", "anti_detect", "adapters", "registry",
                "exceptions"):
        import importlib
        return importlib.import_module(f"core.browser_session.{name}")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "__version__",
    # 入口
    "load_browser_session", "session_from_cookies", "SessionHandle",
    # 子模块
    "SessionConfig", "CookieStore", "SessionBridge", "PlaywrightHandoff",
    "LoginStateChecker", "CheckRule", "CheckResult",
    "BrowserDriver", "SeleniumDriver", "PlaywrightDriver", "create_driver",
    "register_launcher",
    # 工具与探测
    "default_user_data_dir", "detect_installed_browsers", "list_profiles",
    "normalize_domain", "target_domains",
    "anti_detect", "cookie_store", "launcher", "session_bridge",
    "login_state", "MODULE_NAME",
    # 注册与生命周期
    "MODULE_CORE_MODULES", "MODULE_DESCRIPTORS", "MODULE_NAMESPACE",
    "ModuleDescriptor", "get_descriptor", "lifecycle", "public_api",
    "register_browser_session_module", "registered_modules",
    # 异常
    "BrowserSessionError", "SessionUnavailable", "BrowserNotInstalled",
    "UserDataDirNotFound", "ProfileNotFound", "BrowserAlreadyRunning",
    "BrowserLaunchFailed", "HeadlessNotSupported", "LauncherNotAvailable",
    "CookieError", "CookieExportError", "CookieFileNotFound", "CookieFileInvalid",
    "CookieDecryptError", "CookieInjectError", "LoginStateError", "NotLoggedIn",
]
