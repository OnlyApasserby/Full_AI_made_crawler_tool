"""集成测试：真实浏览器 + 真实 User Data（默认跳过，需显式开启）。

运行方式（缺少条件时用例会自动 ``skip``，不会失败）::

    pytest tests/test_browser_session_integration.py -m integration -v

用例覆盖的验收点：
1. 本机已登录的浏览器能被模块识别（User Data / Profile / 内核齐备）
2. **浏览器未关闭时启动模块 → 明确报错**（``BrowserAlreadyRunning``，不是静默失败）
3. 浏览器可用时能启动带登录态的实例（Selenium 或 Playwright）
4. 启动后 ``check_login()`` 能给出确定结论（True / False 都算通过）
5. Cookie 导出 → 落盘 → 重新载入 → 被 requests 会话复用
6. 登录态失效时 ``check_login()`` 返回 False

安全约束：所有用例都使用**只读探测**；只有 ``CS_INTEGRATION_LAUNCH=1``
时才会真正启动浏览器，避免在开发机上意外弹窗。
"""

from __future__ import annotations

import os
import unittest

from core.browser_session import (
    SessionConfig, SessionHandle, detect_installed_browsers,
    load_browser_session, session_from_cookies,
)
from core.browser_session import launcher as launcher_module
from core.browser_session.cookie_store import CookieStore, normalize_cookie
from core.browser_session.exceptions import BrowserAlreadyRunning

#: 集成测试使用的浏览器（可用环境变量切换，默认优先 Edge：多数 Windows 机器有）
BROWSER = os.environ.get("CS_INTEGRATION_BROWSER", "edge")
#: 显式指定 User Data（不填则用平台默认目录）
USER_DATA = os.environ.get("CS_INTEGRATION_USER_DATA_DIR", "")
#: 是否允许真正启动浏览器（默认关闭，避免意外弹窗）
ALLOW_LAUNCH = os.environ.get("CS_INTEGRATION_LAUNCH", "") == "1"
#: 探测用目标站点（默认 example.com：稳定、无需登录、作为"非登录态"样本）
PROBE_DOMAIN = os.environ.get("CS_INTEGRATION_DOMAIN", "example.com")


def _installed_dir(browser: str = BROWSER) -> str:
    """本机该浏览器的 User Data 目录（显式配置优先）。"""
    if USER_DATA and os.path.isdir(USER_DATA):
        return USER_DATA
    return detect_installed_browsers().get(browser, "")


def _make_config(**overrides) -> SessionConfig:
    config = SessionConfig(browser=BROWSER, domain=PROBE_DOMAIN,
                           user_data_dir=_installed_dir(),
                           cookie_encrypt=False).with_overrides(**overrides)
    return config.finalize()


@unittest.skipUnless(_installed_dir(), f"本机未安装 {BROWSER}（或用 CS_INTEGRATION_USER_DATA_DIR 指定）")
class RealEnvironmentDetectionTest(unittest.TestCase):
    """真实环境探测：只读，不启动浏览器。"""

    def test_user_data_and_profile_are_detected(self):
        config = _make_config()
        self.assertTrue(os.path.isdir(config.user_data_dir))
        report = launcher_module.detect_installation(config)
        self.assertTrue(report["user_data_exists"])
        self.assertTrue(report["profile_exists"])

    def test_profiles_are_listed(self):
        from core.browser_session.config import list_profiles
        profiles = list_profiles(_installed_dir())
        self.assertTrue(profiles, "未列出任何 Profile，User Data 目录结构可能变化了")
        self.assertIn("Default", profiles)

    def test_browser_executable_is_found(self):
        config = _make_config()
        executable = launcher_module.find_executable(config)
        if not executable:
            self.skipTest("未找到浏览器内核（可用 CRAWLER_CHROME_PATH 指定）")
        self.assertTrue(os.path.isfile(executable))

    def test_health_check_is_serializable(self):
        config = _make_config()
        report = launcher_module.health_check(config)
        self.assertIn("ok", report)
        self.assertIsInstance(report["stale_locks"], list)


class BusyBrowserErrorTest(unittest.TestCase):
    """验收点 2：浏览器未关闭时必须给出**明确报错**而非静默失败。

    本用例不需要真的浏览器在运行——它用进程替身模拟"占用"，
    因此只要有 User Data 目录就能跑。
    """

    def setUp(self):
        if not _installed_dir():
            self.skipTest(f"本机未安装 {BROWSER}")
        self.config = _make_config()
        if not os.path.isdir(self.config.profile_dir):
            self.skipTest("未找到可用的 Profile")

    def test_clear_error_when_profile_is_busy(self):
        original = launcher_module.list_browser_processes
        try:
            launcher_module.list_browser_processes = lambda: [
                f'chrome.exe --user-data-dir="{self.config.user_data_dir}" '
                f'--profile-directory="{self.config.profile}"']
            handle = SessionHandle(self.config)
            with self.assertRaises(BrowserAlreadyRunning) as ctx:
                handle.get_driver()
        finally:
            launcher_module.list_browser_processes = original
            handle.close()
        message = str(ctx.exception)
        # 报错必须说清"发生了什么 + 怎么解决"
        self.assertIn("User Data", message)
        self.assertIn("必须先完全退出该浏览器", message)
        self.assertIn("任务管理器", message)

    def test_load_does_not_raise_until_driver_is_requested(self):
        """惰性启动：仅加载配置不应因为浏览器占用而失败。"""
        handle = load_browser_session(self.config)
        try:
            self.assertFalse(handle.is_open())
        finally:
            handle.close()


@unittest.skipUnless(ALLOW_LAUNCH, "设置 CS_INTEGRATION_LAUNCH=1 才会真正启动浏览器")
@unittest.skipUnless(_installed_dir(), f"本机未安装 {BROWSER}")
class RealLaunchTest(unittest.TestCase):
    """真实启动（需 CS_INTEGRATION_LAUNCH=1，且浏览器必须先关闭）。"""

    def setUp(self):
        self.config = _make_config()
        busy, reason = launcher_module.profile_in_use(
            self.config.user_data_dir, self.config.profile_dir, self.config.browser)
        if busy:
            self.skipTest("目标 User Data 正被占用（请先关闭浏览器）：\n" + reason)

    def test_launch_export_check_and_reuse_with_requests(self):
        """端到端：启动带登录态的浏览器 → 检查登录 → 导出 Cookie → requests 复用。"""
        import tempfile
        with tempfile.TemporaryDirectory(prefix="cs_e2e_") as tmp:
            cookie_path = os.path.join(tmp, "cookies.json")
            handle = load_browser_session(self.config, autostart=False)
            try:
                driver = handle.get_driver()
                self.assertTrue(handle.is_open())
                self.assertTrue(driver.current_url() is not None)

                # 登录态检查：两个结论都算通过，只要**确定**给出结论即可
                result = handle.check_login_detail()
                self.assertIn(result.channel, ("browser", "http", "cookie"))
                self.assertIsInstance(result.logged_in, bool)

                # 导出 Cookie（可能为空：example.com 本身不设置 Cookie）
                path = handle.export_cookies(cookie_path)
                self.assertTrue(os.path.isfile(path))
                exported = handle.cookies()
            finally:
                handle.close()
            self.assertFalse(handle.is_open())        # 关闭后必须真的不再可用

            # Cookie 文件可被独立载入
            store = CookieStore(self.config, path=cookie_path)
            if exported:
                loaded = store.load()
                self.assertEqual(len(loaded), len(exported))
                # 用纯 Cookie 会话复用（不需要浏览器）
                reuse = session_from_cookies(self.config, cookie_path=cookie_path)
                try:
                    session = reuse.get_requests_session()
                    self.assertEqual(len(session.cookies), len(loaded))
                finally:
                    reuse.close()

    def test_driver_options_include_user_data_dir(self):
        """启动参数必须真的带上 --user-data-dir / --profile-directory。"""
        handle = load_browser_session(self.config, autostart=False)
        try:
            driver = handle.get_driver()
            args = getattr(getattr(driver, "driver", None), "capabilities", None)
            # Selenium：从 capabilities 里取 chromeOptions.args
            if isinstance(args, dict):
                options = args.get("goog:chromeOptions", {}).get("args", [])
                joined = " ".join(options)
                self.assertIn("--user-data-dir=", joined)
                self.assertIn("--profile-directory=", joined)
                self.assertIn("AutomationControlled", joined)
            else:
                # Playwright：直接用 launch_persistent_context，User Data 生效于上下文
                self.assertIsNotNone(getattr(driver, "context", None))
        finally:
            handle.close()


class ExpiredLoginStateTest(unittest.TestCase):
    """验收点 6：登录态失效时 check_login() 必须返回 False。"""

    def test_expired_cookies_yield_false(self):
        import time
        config = _make_config()
        expired = normalize_cookie({"name": "sid", "value": "x",
                                    "domain": config.domain,
                                    "expiry": int(time.time()) - 3600})
        handle = SessionHandle(config)
        handle.store.cookies = [expired]
        try:
            self.assertFalse(handle.check_login(channel="cookie"))
        finally:
            handle.close()

    def test_protected_marker_rule_yields_false(self):
        """配置了登出关键字时，命中即判定失效（无需真实站点）。"""
        from core.browser_session.login_state import CheckRule, LoginStateChecker
        from core.browser_session.session_bridge import SessionBridge

        class RedirectingSession:
            def get(self, url, **kwargs):
                class Response:
                    status_code = 200
                    url = "https://example.com/login"
                    text = "请先登录后再访问"
                return Response()

            def close(self):
                pass

        config = _make_config(logout_markers=["请先登录"])
        bridge = SessionBridge(config)
        bridge.store.cookies = [normalize_cookie({"name": "sid", "value": "x",
                                                  "domain": config.domain})]
        bridge.get_requests_session = lambda *a, **k: RedirectingSession()
        checker = LoginStateChecker(config, None, bridge)
        checker.rule = CheckRule.from_config(config)
        result = checker.check(channel="http")
        self.assertFalse(result.logged_in)
        self.assertIn("未登录关键字", result.reason)


if __name__ == "__main__":
    unittest.main()
