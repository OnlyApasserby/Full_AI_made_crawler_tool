"""SessionBridge / SessionHandle / AntiDetection 的单元测试。

覆盖：登录态桥接到 requests 与框架抓取层、统一句柄的公开接口、
反检测脚本与行为拟人化工具。不启动浏览器、不访问网络。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest

from core.browser_session import (
    SessionHandle, anti_detect, load_browser_session, session_from_cookies,
)
from core.browser_session.adapters import BrowserDriver, normalize_proxy
from core.browser_session.config import SessionConfig
from core.browser_session.cookie_store import CookieStore, normalize_cookie
from core.browser_session.exceptions import LoginStateError
from core.browser_session.session_bridge import PlaywrightHandoff, SessionBridge


class FakeDriver(BrowserDriver):
    """满足统一驱动契约的替身（同时用于桥接与句柄测试）。"""

    name = "fake"

    def __init__(self, config, url="https://example.com/me", ua="FakeChrome/1.0"):
        super().__init__(config)
        self._url = url
        self._ua = ua
        self.cookies = [{"name": "sid", "value": "abc", "domain": ".example.com",
                         "path": "/", "secure": True, "httpOnly": True}]
        self.injected: list[dict] = []
        self.visited: list[str] = []
        self.refreshed = 0
        self.quit_called = False
        self.context = object()          # 模拟 playwright 持久化上下文

    # 页面
    def get(self, url):
        self.visited.append(url)
        self._url = url

    def current_url(self):
        return self._url

    def page_source(self):
        return "<html><body>我的账户 退出登录</body></html>"

    def refresh(self):
        self.refreshed += 1

    def title(self):
        return "fake"

    def text(self):
        return "我的账户 退出登录"

    # Cookie
    def get_cookies(self):
        return [dict(item) for item in self.cookies]

    def add_cookies(self, cookies):
        self.injected.extend(cookies)
        return len(cookies), []

    def delete_all_cookies(self):
        self.injected.clear()

    # 指纹
    def user_agent(self):
        return self._ua

    def request_headers(self):
        headers = super().request_headers()                # 含 User-Agent 等指纹字段
        headers["Referer"] = "https://example.com/me"      # 应被桥接层过滤掉
        headers["Connection"] = "keep-alive"               # 同上
        return headers

    # 生命周期
    def quit(self):
        self.quit_called = True

    def alive(self):
        return not self.quit_called

    def kind(self):
        return self.name


def make_config(**overrides) -> SessionConfig:
    base = {"domain": "example.com", "cookie_encrypt": False}
    base.update(overrides)
    config = SessionConfig(**base).finalize()
    return config


# ---------------------------------------------------------------------------
# SessionBridge
# ---------------------------------------------------------------------------
class SessionBridgeTest(unittest.TestCase):
    """登录态 → requests 的桥接。"""

    def setUp(self):
        self.config = make_config()
        self.driver = FakeDriver(self.config)
        self.bridge = SessionBridge(self.config, self.driver)

    def test_headers_take_browser_fingerprint_and_drop_hop_headers(self):
        headers = self.bridge.headers()
        self.assertEqual(headers["User-Agent"], "FakeChrome/1.0")
        self.assertEqual(headers["Accept-Language"], "zh-CN")
        self.assertNotIn("Referer", headers)        # 逐请求头不应固化进会话
        self.assertNotIn("Connection", headers)

    def test_headers_cached_until_refresh(self):
        first = self.bridge.headers()
        first["User-Agent"] = "tampered"
        self.assertEqual(self.bridge.headers()["User-Agent"], "FakeChrome/1.0")
        self.assertEqual(self.bridge.headers(refresh=True)["User-Agent"],
                         "FakeChrome/1.0")

    def test_cookie_dict_from_live_driver(self):
        self.assertEqual(self.bridge.cookie_dict(), {"sid": "abc"})

    def test_requests_session_carries_cookies_headers_and_proxy(self):
        config = make_config(proxy={"https": "http://127.0.0.1:8888"})
        bridge = SessionBridge(config, FakeDriver(config))
        session = bridge.get_requests_session()
        self.assertIn("sid", session.cookies)
        self.assertEqual(session.headers["User-Agent"], "FakeChrome/1.0")
        self.assertEqual(session.proxies["https"], "http://127.0.0.1:8888")
        bridge.close()
        self.assertIsNone(bridge._session)

    def test_requests_session_without_cookies_raises(self):
        bridge = SessionBridge(self.config)
        bridge.store.cookies = []
        with self.assertRaises(LoginStateError) as ctx:
            bridge.get_requests_session()
        self.assertIn("没有可用的 Cookie", str(ctx.exception))

    def test_playwright_context_handoff(self):
        handoff = self.bridge.get_playwright_context()
        self.assertIsInstance(handoff, PlaywrightHandoff)
        self.assertIs(handoff.context, self.driver.context)
        self.assertEqual(handoff.user_agent, "FakeChrome/1.0")
        self.assertGreaterEqual(handoff.cookie_count, 1)

    def test_playwright_context_requires_context(self):
        class NoContext(FakeDriver):
            def __init__(self, config):
                super().__init__(config)
                self.context = None

        bridge = SessionBridge(self.config, NoContext(self.config))
        with self.assertRaises(LoginStateError) as ctx:
            bridge.get_playwright_context()
        self.assertIn("没有运行中的 Playwright 浏览器上下文", str(ctx.exception))

    def test_create_fetcher_uses_browser_fingerprint_and_cookie_jar(self):
        fetcher = self.bridge.create_fetcher("static")
        try:
            self.assertEqual(fetcher.user_agent, "FakeChrome/1.0")
            self.assertIsNone(fetcher.extra_headers.get("Referer"))
            session = fetcher._session()
            self.assertIn("sid", session.cookies)
            # UA 池被限制为浏览器 UA，避免随机 UA 与 Cookie 指纹不匹配
            self.assertEqual(fetcher.ua_pool.random(), "FakeChrome/1.0")
        finally:
            fetcher.close()

    def test_create_fetcher_applies_proxy(self):
        config = make_config(proxy={"server": "http://10.0.0.1:8080"})
        bridge = SessionBridge(config, FakeDriver(config))
        fetcher = bridge.create_fetcher("static")
        try:
            self.assertEqual(fetcher.proxy["http"], "http://10.0.0.1:8080")
        finally:
            fetcher.close()


class ProxyNormalizationTest(unittest.TestCase):
    """代理配置的多种写法归一（requests / playwright / selenium 共用一份）。"""

    def test_empty_variants(self):
        for value in (None, {}, {"https": ""}, "  "):
            with self.subTest(value=value):
                self.assertEqual(normalize_proxy(value), {})

    def test_framework_and_playwright_styles(self):
        self.assertEqual(normalize_proxy({"https": "http://a:1"}), {"server": "http://a:1"})
        self.assertEqual(normalize_proxy({"server": "socks5://a:1", "username": "u",
                                          "password": "p"}),
                         {"server": "socks5://a:1", "username": "u", "password": "p"})
        self.assertEqual(normalize_proxy("http://b:2"), {"server": "http://b:2"})

    def test_anti_detect_proxy_helpers(self):
        config = make_config(proxy={"https": "http://p:9", "username": "u",
                                    "password": "pw"})
        self.assertEqual(anti_detect.proxy_for_requests(config),
                         {"http": "http://p:9", "https": "http://p:9"})
        self.assertEqual(anti_detect.proxy_for_selenium(config),
                         ["--proxy-server=http://p:9"])


# ---------------------------------------------------------------------------
# SessionHandle
# ---------------------------------------------------------------------------
class SessionHandleTest(unittest.TestCase):
    """统一句柄的公开接口约定。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="handle_test_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config = make_config(cookie_store_path=os.path.join(self.tmp, "c.json"))
        self.driver = FakeDriver(self.config)

    def _handle(self, **overrides):
        config = self.config.with_overrides(**overrides)
        return SessionHandle(config, driver=self.driver)

    def test_required_public_api_exists(self):
        handle = self._handle()
        for name in ("get_driver", "get_requests_session", "check_login",
                     "export_cookies", "load_cookies", "close",
                     "get_playwright_context", "fetch"):
            self.assertTrue(callable(getattr(handle, name)), name)

    def test_repr_and_describe_do_not_leak_cookie_values(self):
        handle = self._handle()
        handle.store.export_from_driver(self.driver)
        self.assertIn("example.com", repr(handle))
        self.assertNotIn("abc", handle.describe())

    def test_get_driver_returns_injected_driver(self):
        handle = self._handle()
        self.assertIs(handle.get_driver(), self.driver)
        self.assertTrue(handle.is_open())

    def test_check_login_uses_browser_channel(self):
        handle = self._handle()
        self.assertTrue(handle.check_login())
        self.assertIsNotNone(handle.last_check)
        self.assertEqual(handle.last_check.channel, "browser")

    def test_export_then_load_cookies_roundtrip(self):
        handle = self._handle()
        path = handle.export_cookies()
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(len(handle.cookies()), 1)

        fresh = FakeDriver(self.config)
        other = SessionHandle(self.config, driver=fresh)
        applied = other.load_cookies(path)
        self.assertEqual(applied, 1)
        self.assertEqual([item["name"] for item in fresh.injected], ["sid"])

    def test_load_cookies_without_driver_defers_injection(self):
        handle = SessionHandle(self.config)              # 惰性：没有驱动
        store = CookieStore(self.config)
        store.cookies = [normalize_cookie({"name": "sid", "value": "1",
                                           "domain": "example.com"})]
        path = store.save()
        self.assertEqual(handle.load_cookies(path), 0)   # 未注入
        self.assertEqual(len(handle.cookies()), 1)       # 但已进内存

    def test_get_requests_session_export_cookies_when_driver_alive(self):
        handle = self._handle()
        session = handle.get_requests_session()
        self.assertIn("sid", session.cookies)
        self.assertEqual(handle.store.cookies[0]["name"], "sid")

    def test_get_fetcher_and_fetch_delegate(self):
        handle = self._handle()
        fetcher = handle.get_fetcher("static")
        self.assertIs(handle.get_fetcher("static"), fetcher)     # 复用同一实例
        self.assertIn("sid", fetcher._session().cookies)

    def test_health_check_shape(self):
        handle = self._handle()
        report = handle.health_check()
        for key in ("session_open", "cookie_total", "cookie_path", "driver_alive",
                    "last_check", "ok"):
            self.assertIn(key, report)
        self.assertTrue(report["session_open"])

    def test_close_is_idempotent_and_marks_state(self):
        handle = self._handle()
        handle.close()
        handle.close()                                   # 重复调用不抛异常
        self.assertTrue(self.driver.quit_called)
        self.assertFalse(handle.is_open())
        self.assertIn("已关闭", repr(handle))

    def test_context_manager_closes(self):
        with self._handle() as handle:
            self.assertTrue(handle.is_open())
        self.assertTrue(self.driver.quit_called)

    def test_closed_handle_refuses_to_restart(self):
        handle = self._handle()
        handle.close()
        with self.assertRaises(Exception):
            handle.ensure_driver()


class SessionEntryPointTest(unittest.TestCase):
    """统一入口：load_browser_session / session_from_cookies。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="entry_test_")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _cookie_file(self) -> str:
        config = make_config(cookie_store_path=os.path.join(self.tmp, "c.json"))
        store = CookieStore(config)
        store.cookies = [normalize_cookie({"name": "sid", "value": "v",
                                           "domain": "example.com"})]
        return store.save()

    def test_load_with_cookie_path_skips_user_data_validation(self):
        path = self._cookie_file()
        handle = load_browser_session(
            {"domain": "example.com", "cookie_store_path": path,
             "user_data_dir": os.path.join(self.tmp, "no-such-ud"),
             "cookie_encrypt": False},
            cookie_path=path)
        try:
            self.assertEqual(len(handle.cookies()), 1)
            self.assertFalse(handle.is_open())            # 未启动浏览器
        finally:
            handle.close()

    def test_load_without_domain_raises_but_names_the_env_var(self):
        with self.assertRaises(ValueError) as ctx:
            load_browser_session({"cookie_encrypt": False},
                                 cookie_path=self._cookie_file())
        self.assertIn("目标域名", str(ctx.exception))

    def test_load_missing_user_data_raises_with_hint(self):
        with self.assertRaises(Exception) as ctx:
            load_browser_session({"domain": "example.com",
                                  "user_data_dir": os.path.join(self.tmp, "nope")})
        self.assertIn("User Data 目录不存在", str(ctx.exception))

    def test_overrides_take_effect(self):
        handle = load_browser_session(
            {"domain": "example.com", "cookie_encrypt": False,
             "user_data_dir": os.path.join(self.tmp, "nope")},
            cookie_path=self._cookie_file(), profile="Profile 5")
        try:
            self.assertEqual(handle.config.profile, "Profile 5")
        finally:
            handle.close()

    def test_progress_callback_receives_messages(self):
        messages: list[str] = []
        handle = load_browser_session(
            {"domain": "example.com", "cookie_encrypt": False},
            cookie_path=self._cookie_file(), progress=messages.append)
        try:
            self.assertTrue(messages)
            self.assertTrue(any("登录态" in item for item in messages))
        finally:
            handle.close()

    def test_session_from_cookies_builds_requests_session_only(self):
        path = self._cookie_file()
        handle = session_from_cookies({"domain": "example.com",
                                       "cookie_encrypt": False}, cookie_path=path)
        try:
            session = handle.get_requests_session()
            self.assertIn("sid", session.cookies)
            self.assertFalse(handle.is_open())
        finally:
            handle.close()

    def test_session_from_cookies_requires_domain(self):
        with self.assertRaises(ValueError):
            session_from_cookies({"cookie_encrypt": False},
                                 cookie_path=self._cookie_file())


# ---------------------------------------------------------------------------
# AntiDetection
# ---------------------------------------------------------------------------
class HumanBehaviourTest(unittest.TestCase):
    """行为拟人化工具（可测试的确定性部分）。"""

    def test_bezier_path_starts_and_ends_at_endpoints(self):
        path = anti_detect.bezier_path((0, 0), (100, 50), steps=12)
        self.assertEqual(len(path), 13)
        self.assertAlmostEqual(path[0][0], 0, delta=0.01)
        self.assertAlmostEqual(path[0][1], 0, delta=0.01)
        self.assertAlmostEqual(path[-1][0], 100, delta=0.01)
        self.assertAlmostEqual(path[-1][1], 50, delta=0.01)

    def test_bezier_path_is_not_a_straight_line(self):
        """带曲率的轨迹应当偏离直线（直线是明显的机器特征）。"""
        path = anti_detect.bezier_path((0, 0), (200, 0), steps=20, curvature=0.3)
        deviations = [abs(y) for _, y in path[1:-1]]
        self.assertGreater(max(deviations), 1.0)

    def test_bezier_steps_lower_bound(self):
        self.assertEqual(len(anti_detect.bezier_path((0, 0), (1, 1), steps=0)), 3)

    def test_rate_limiter_disabled_is_noop(self):
        limiter = anti_detect.RateLimiter(0, 0)
        self.assertFalse(limiter.enabled)
        started = time.monotonic()
        self.assertEqual(limiter.wait(), 0.0)
        self.assertLess(time.monotonic() - started, 0.05)

    def test_rate_limiter_enforces_min_interval(self):
        limiter = anti_detect.RateLimiter(0, 0.2)
        self.assertTrue(limiter.enabled)
        limiter.wait()
        started = time.monotonic()
        limiter.wait()
        self.assertGreaterEqual(time.monotonic() - started, 0.15)

    def test_rate_limiter_reset(self):
        limiter = anti_detect.RateLimiter(60, 0)
        limiter.wait()
        limiter.reset()
        self.assertEqual(limiter._events, [])

    def test_keystroke_delays_shape(self):
        delays = anti_detect.keystroke_delays("abc", mean=0.1)
        self.assertEqual(len(delays), 3)
        self.assertTrue(all(item > 0 for item in delays))

    def test_random_delay_disabled_returns_zero(self):
        self.assertEqual(anti_detect.random_delay(0, 0.5), 0.0)

    def test_human_scroll_calls_driver(self):
        class ScrollDriver:
            def __init__(self):
                self.scripts = []

            def execute_script(self, script):
                self.scripts.append(script)

        driver = ScrollDriver()
        # 把随机停顿压到最小，避免测试变慢
        original = anti_detect.rand_delay
        anti_detect.rand_delay = lambda low, high: 0.0
        try:
            done = anti_detect.human_scroll(driver, times=3, pixels=(10, 20))
        finally:
            anti_detect.rand_delay = original
        self.assertEqual(done, 3)
        self.assertTrue(all("scrollBy" in script for script in driver.scripts))


class StealthScriptTest(unittest.TestCase):
    """反检测脚本内容：覆盖常见指纹项，且**不含**任何破解逻辑。"""

    def test_script_covers_common_fingerprints(self):
        script = anti_detect.stealth_script(make_config())
        for marker in ("webdriver", "plugins", "languages", "chrome.runtime",
                       "permissions", "WebGLRenderingContext", "mimeTypes"):
            self.assertIn(marker, script)

    def test_script_includes_timezone_and_geolocation_when_configured(self):
        script = anti_detect.stealth_script(make_config(
            timezone="America/New_York", geolocation=(40.71, -74.01)))
        self.assertIn("America/New_York", script)
        self.assertIn("40.71", script)
        self.assertIn("-74.01", script)

    def test_script_omits_timezone_block_when_unset(self):
        script = anti_detect.stealth_script(make_config(timezone="", geolocation=None))
        self.assertNotIn("getTimezoneOffset", script)

    def test_script_contains_no_captcha_bypass(self):
        """合规边界：脚本里不应出现验证码/破解相关代码。"""
        import re
        script = anti_detect.stealth_script(make_config()).lower()
        for forbidden in ("captcha", "recaptcha", "hcaptcha", "turnstile", "bypass"):
            self.assertNotIn(forbidden, script)
        # 按词边界匹配，避免把 resolve() 之类正常代码误判为 "solve"
        self.assertIsNone(re.search(r"\bsolve", script))

    def test_apply_stealth_reports_success_and_failure(self):
        class Ok:
            def execute_script(self, script):
                return 1

        class Bad:
            def execute_script(self, script):
                raise RuntimeError("no js")

        self.assertTrue(anti_detect.apply_stealth(Ok(), make_config()))
        self.assertFalse(anti_detect.apply_stealth(Bad(), make_config()))


if __name__ == "__main__":
    unittest.main()
