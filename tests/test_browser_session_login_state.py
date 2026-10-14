"""LoginStateChecker 子模块单元测试：规则判定、通道选择与重登入口。

全部使用驱动替身与假 requests 会话，不访问网络。
"""

from __future__ import annotations

import unittest

from core.browser_session.config import SessionConfig
from core.browser_session.login_state import (
    CHANNEL_BROWSER, CHANNEL_COOKIE, CHANNEL_HTTP, CheckResult, CheckRule,
    LoginStateChecker, expired_cookies,
)
from core.browser_session.cookie_store import normalize_cookie
from core.browser_session.exceptions import LoginStateError
from core.browser_session.session_bridge import SessionBridge


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------
class FakeTextDriver:
    """文本型驱动替身（页面内容与最终 URL 可控）。"""

    def __init__(self, text: str = "", url: str = "https://example.com/",
                 title: str = "", selectors: set | None = None,
                 fail_navigation: bool = False):
        self._text = text
        self._url = url
        self._title = title
        self._selectors = set(selectors or ())
        self._fail = fail_navigation
        self.visited: list[str] = []

    def get(self, url: str) -> None:
        if self._fail:
            raise RuntimeError("net::ERR_CONNECTION_REFUSED")
        self.visited.append(url)

    def current_url(self) -> str:
        return self._url

    def page_source(self) -> str:
        return f"<html><body>{self._text}</body></html>"

    def text(self) -> str:
        return self._text

    def title(self) -> str:
        return self._title

    def refresh(self) -> None:
        pass

    def wait_for_selector(self, selector: str, timeout: float = 10.0) -> bool:
        return selector in self._selectors

    def execute_script(self, script: str, *args):
        return None

    def get_cookies(self) -> list[dict]:
        return []

    def add_cookies(self, cookies: list[dict]):
        return len(cookies), []

    def delete_all_cookies(self) -> None:
        pass

    def quit(self) -> None:
        pass

    def alive(self) -> bool:
        return True

    def kind(self) -> str:
        return "fake"


class FakeResponse:
    def __init__(self, text: str = "", status_code: int = 200, url: str = ""):
        self.text = text
        self.status_code = status_code
        self.url = url


class FakeRequestsSession:
    """假的 requests.Session：按预设响应返回，记录请求过的 URL。"""

    def __init__(self, response: FakeResponse):
        self.response = response
        self.requested: list[str] = []
        self.closed = False

    def get(self, url: str, **kwargs):
        self.requested.append(url)
        if not self.response.url:
            self.response.url = url
        return self.response

    def close(self) -> None:
        self.closed = True


def bridge_with(config: SessionConfig, cookies: list[dict] | None = None):
    """构造一个"有 Cookie、http 通道可控"的 SessionBridge 替身。"""
    bridge = SessionBridge(config)
    bridge.store.cookies = list(cookies if cookies is not None else
                                [normalize_cookie({"name": "sid", "value": "1",
                                                   "domain": "example.com"})])
    return bridge


# ---------------------------------------------------------------------------
# 规则
# ---------------------------------------------------------------------------
class CheckRuleTest(unittest.TestCase):
    """规则构造与兜底关键字。"""

    def test_from_config_builds_url_and_normalizes_markers(self):
        config = SessionConfig(domain="example.com", login_url="",
                               logout_markers=["请登录", "登录已过期"],
                               login_status=[200, 302]).finalize()
        rule = CheckRule.from_config(config)
        self.assertEqual(rule.url, "https://example.com/")
        self.assertEqual(rule.logout_markers, ["请登录", "登录已过期"])
        self.assertEqual(rule.login_status, [200, 302])

    def test_explicit_login_url_wins(self):
        config = SessionConfig(domain="example.com",
                               login_url="https://example.com/me/profile").finalize()
        self.assertEqual(CheckRule.from_config(config).url,
                         "https://example.com/me/profile")

    def test_builtin_fallbacks_used_when_unconfigured(self):
        rule = CheckRule(domain="example.com")
        self.assertTrue(rule.resolved_logout_markers())
        self.assertTrue(rule.resolved_login_markers())
        self.assertIn("请先登录", rule.resolved_logout_markers())

    def test_describe_mentions_url_and_status(self):
        rule = CheckRule(url="https://a.com/", login_status=[200])
        self.assertIn("https://a.com/", rule.describe())


# ---------------------------------------------------------------------------
# 文本判定（浏览器通道）
# ---------------------------------------------------------------------------
class BrowserChannelTest(unittest.TestCase):
    """浏览器通道：关键字、状态码、重定向与选择器的优先级。"""

    def setUp(self):
        self.config = SessionConfig(domain="example.com").finalize()

    def _checker(self, driver, **overrides):
        config = self.config.with_overrides(**overrides)
        checker = LoginStateChecker(config, driver, bridge_with(config))
        checker.rule = CheckRule.from_config(config)
        return checker

    def test_logout_marker_means_not_logged_in(self):
        driver = FakeTextDriver("请先登录后查看该内容",
                                url="https://example.com/protected")
        result = self._checker(driver).check()
        self.assertFalse(result.logged_in)
        self.assertEqual(result.channel, CHANNEL_BROWSER)
        self.assertIn("未登录关键字", result.reason)
        self.assertTrue(result.evidence)
        self.assertFalse(bool(result))

    def test_logout_marker_beats_login_marker(self):
        driver = FakeTextDriver("欢迎回来 我的账户｜请重新登录",
                                url="https://example.com/me")
        result = self._checker(driver).check()
        self.assertFalse(result.logged_in)              # 登出优先
        self.assertTrue(result.warnings)                # 并给出冲突提示

    def test_login_marker_means_logged_in(self):
        driver = FakeTextDriver("个人中心 退出登录", url="https://example.com/me")
        result = self._checker(driver).check()
        self.assertTrue(result.logged_in)
        self.assertIn("已登录关键字", result.reason)

    def test_redirect_offsite_means_not_logged_in(self):
        driver = FakeTextDriver("个人中心 退出登录",
                                url="https://login.example.com/signin")
        result = self._checker(driver, domain="example.com").check()
        # login.example.com 属于 example.com 的子域，仍算同站
        self.assertTrue(result.logged_in)

        driver = FakeTextDriver("任意内容", url="https://auth.other-site.net/login")
        result = self._checker(driver).check()
        self.assertFalse(result.logged_in)
        self.assertIn("重定向", result.reason)

    def test_selector_rules_take_priority_over_text(self):
        driver = FakeTextDriver("页面里有'请登录'字样", url="https://example.com/",
                                selectors={".user-avatar"})
        result = self._checker(driver, logout_markers=[],
                               login_selector=".user-avatar").check()
        self.assertTrue(result.logged_in)
        self.assertIn("登录选择器", result.reason)

    def test_logout_selector_means_not_logged_in(self):
        driver = FakeTextDriver("一切正常", url="https://example.com/",
                                selectors={".login-form"})
        result = self._checker(driver, logout_selector=".login-form").check()
        self.assertFalse(result.logged_in)
        self.assertIn("登出选择器", result.reason)

    def test_navigation_failure_raises_login_state_error(self):
        driver = FakeTextDriver(fail_navigation=True)
        with self.assertRaises(LoginStateError) as ctx:
            self._checker(driver).check()
        self.assertIn("打开探测页面失败", str(ctx.exception))

    def test_missing_url_raises(self):
        driver = FakeTextDriver()
        checker = LoginStateChecker(self.config, driver, bridge_with(self.config))
        checker.rule = CheckRule(url="")
        with self.assertRaises(LoginStateError) as ctx:
            checker.check()
        self.assertIn("未配置登录态探测 URL", str(ctx.exception))

    def test_status_fallback_for_browser_channel_without_status(self):
        """浏览器通道拿不到状态码时，用"页面非空"做弱判据并给出提示。"""
        driver = FakeTextDriver("随便一段不命中任何配置关键字的内容",
                                url="https://example.com/")
        checker = self._checker(driver)
        # 关键词清单为空 → 走内置兜底词；该文本不命中任何兜底词，落到状态码分支
        checker.rule = CheckRule(url="https://example.com/", login_status=[200],
                                 login_markers=["绝不出现的锚点A"],
                                 logout_markers=["绝不出现的锚点B"])
        result = checker.check()
        self.assertTrue(result.logged_in)
        self.assertIn("状态码", result.reason)
        self.assertTrue(result.warnings)          # 提示 status=0，规则未真正生效
        self.assertFalse(result.evidence)         # 状态码分支不产生文本证据


# ---------------------------------------------------------------------------
# http / cookie 通道
# ---------------------------------------------------------------------------
class HttpChannelTest(unittest.TestCase):
    """http 通道：状态码规则与原因文本。"""

    def setUp(self):
        self.config = SessionConfig(domain="example.com").finalize()

    def _checker(self, response: FakeResponse, **overrides):
        config = self.config.with_overrides(**overrides)
        bridge = bridge_with(config)
        session = FakeRequestsSession(response)
        bridge.get_requests_session = lambda *a, **k: session      # 注入假会话
        checker = LoginStateChecker(config, None, bridge)
        checker.rule = CheckRule.from_config(config)
        return checker, session

    def test_expired_session_detected_by_marker(self):
        checker, session = self._checker(FakeResponse(
            "登录已过期，请重新登录", url="https://example.com/me"))
        result = checker.check()
        self.assertFalse(result.logged_in)
        self.assertEqual(result.channel, CHANNEL_HTTP)
        self.assertEqual(session.requested, ["https://example.com/"])
        self.assertTrue(session.closed)              # 用完即关，避免连接泄漏

    def test_logged_in_detected_by_marker(self):
        checker, _ = self._checker(FakeResponse("我的账户 退出登录",
                                                url="https://example.com/me"))
        result = checker.check()
        self.assertTrue(result.logged_in)
        self.assertEqual(result.status, 200)

    def test_status_code_rule_detects_login(self):
        checker, _ = self._checker(FakeResponse("ok", status_code=200,
                                                url="https://example.com/me"),
                                   login_markers=[], logout_markers=[])
        result = checker.check()
        self.assertTrue(result.logged_in)
        self.assertIn("状态码", result.reason)

    def test_unexpected_status_code_means_not_logged_in(self):
        checker, _ = self._checker(FakeResponse("ok", status_code=403,
                                                url="https://example.com/me"),
                                   login_markers=[], logout_markers=[])
        result = checker.check()
        self.assertFalse(result.logged_in)
        self.assertEqual(result.status, 403)

    def test_redirect_to_login_page_detected(self):
        checker, _ = self._checker(FakeResponse(
            "whatever", status_code=200,
            url="https://auth.other-site.net/login"))
        result = checker.check()
        self.assertFalse(result.logged_in)
        self.assertIn("重定向", result.reason)

    def test_no_cookies_raises_from_bridge(self):
        config = self.config
        bridge = bridge_with(config, cookies=[])
        checker = LoginStateChecker(config, None, bridge)
        with self.assertRaises(LoginStateError) as ctx:
            checker.check(channel=CHANNEL_HTTP)
        self.assertIn("无法构造带登录态的请求会话", str(ctx.exception))


class CookieChannelTest(unittest.TestCase):
    """cookie 通道：没有浏览器也没有 http 条件时的静态兜底。"""

    def setUp(self):
        self.config = SessionConfig(domain="example.com").finalize()

    def test_no_cookies_means_not_logged_in(self):
        bridge = bridge_with(self.config, cookies=[])
        result = LoginStateChecker(self.config, None, bridge).check()
        self.assertFalse(result.logged_in)
        self.assertEqual(result.channel, CHANNEL_COOKIE)
        self.assertTrue(result.fallback)
        self.assertIn("没有任何可用的 Cookie", result.reason)

    def test_unexpired_cookies_assume_valid(self):
        cookies = [normalize_cookie({"name": "sid", "value": "1",
                                     "domain": "example.com"})]
        result = LoginStateChecker(self.config, None,
                                   bridge_with(self.config, cookies)).check(channel=CHANNEL_COOKIE)
        self.assertTrue(result.logged_in)
        self.assertTrue(result.fallback)
        self.assertIn("兜底策略", result.reason)

    def test_all_expired_means_not_logged_in(self):
        import time
        cookies = [normalize_cookie({"name": "sid", "value": "1",
                                     "domain": "example.com",
                                     "expiry": int(time.time()) - 10})]
        result = LoginStateChecker(self.config, None,
                                   bridge_with(self.config, cookies)).check(channel=CHANNEL_COOKIE)
        self.assertFalse(result.logged_in)
        self.assertIn("均已过期", result.reason)

    def test_assume_valid_false_respected(self):
        cookies = [normalize_cookie({"name": "sid", "value": "1",
                                     "domain": "example.com"})]
        config = self.config.with_overrides(cookie_assume_valid=False)
        result = LoginStateChecker(config, None,
                                   bridge_with(config, cookies)).check(channel=CHANNEL_COOKIE)
        self.assertFalse(result.logged_in)

    def test_partially_expired_warns_but_keeps_verdict(self):
        import time
        cookies = [
            normalize_cookie({"name": "old", "value": "1", "domain": "example.com",
                              "expiry": int(time.time()) - 10}),
            normalize_cookie({"name": "new", "value": "2", "domain": "example.com"}),
        ]
        result = LoginStateChecker(self.config, None,
                                   bridge_with(self.config, cookies)).check(channel=CHANNEL_COOKIE)
        self.assertTrue(result.logged_in)
        self.assertEqual(result.expired, 1)
        self.assertTrue(result.warnings)

    def test_expired_cookies_helper(self):
        now = 1_000_000_000.0
        cookies = [{"name": "a", "expiry": now - 1}, {"name": "b", "expiry": now + 1},
                   {"name": "c"}]
        self.assertEqual([item["name"] for item in expired_cookies(cookies, now)],
                         ["a"])


# ---------------------------------------------------------------------------
# 结果对象与重登入口
# ---------------------------------------------------------------------------
class CheckResultTest(unittest.TestCase):
    """结果对象：布尔语义、可读输出与结构化输出。"""

    def test_bool_protocol(self):
        self.assertTrue(CheckResult(logged_in=True))
        self.assertFalse(CheckResult(logged_in=False))
        self.assertFalse(bool(CheckResult(logged_in=False)))

    def test_describe_and_to_dict(self):
        result = CheckResult(logged_in=True, reason="命中关键字", channel=CHANNEL_BROWSER,
                             evidence="个人中心", url="https://example.com/",
                             status=200, cookie_total=3)
        self.assertIn("已登录", result.describe())
        payload = result.to_dict()
        self.assertTrue(payload["logged_in"])
        self.assertEqual(payload["channel"], CHANNEL_BROWSER)
        self.assertNotIn("cookies", payload)          # 不外泄 Cookie 内容


class ReloginFlowTest(unittest.TestCase):
    """失效重登入口：给出可执行步骤，且明确不做验证码绕过。"""

    def setUp(self):
        self.config = SessionConfig(domain="example.com",
                                    profile="Profile 1").finalize()

    def test_history_steps_are_actionable(self):
        checker = LoginStateChecker(self.config, None, bridge_with(self.config))
        hint = checker.relogin_hint(CheckResult(logged_in=False, reason="Cookie 过期"))
        self.assertIn("关闭所有正在使用该 User Data 目录的浏览器", hint)
        self.assertIn("Profile 1", hint)
        self.assertIn("导出 Cookie", hint)
        self.assertIn("不做任何验证码绕过", hint)
        self.assertIn("Cookie 过期", hint)

    def test_require_login_raises_with_hint(self):
        cookies = [normalize_cookie({"name": "sid", "value": "1",
                                     "domain": "example.com"})]
        config = self.config.with_overrides(cookie_assume_valid=False)
        checker = LoginStateChecker(config, None, bridge_with(config, cookies))
        with self.assertRaises(LoginStateError) as ctx:
            checker.require_login(channel=CHANNEL_COOKIE)
        self.assertIn("需要人工登录一次", str(ctx.exception))

    def test_require_login_returns_result_when_ok(self):
        cookies = [normalize_cookie({"name": "sid", "value": "1",
                                     "domain": "example.com"})]
        checker = LoginStateChecker(self.config, None,
                                    bridge_with(self.config, cookies))
        result = checker.require_login(channel=CHANNEL_COOKIE)
        self.assertTrue(result.logged_in)

    def test_is_logged_in_shortcut(self):
        cookies = [normalize_cookie({"name": "sid", "value": "1",
                                     "domain": "example.com"})]
        checker = LoginStateChecker(self.config, None,
                                    bridge_with(self.config, cookies))
        self.assertTrue(checker.is_logged_in(channel=CHANNEL_COOKIE))
        # 关闭兜底推断后应返回 False（不再"有 Cookie 就算已登录"）
        strict = LoginStateChecker(
            self.config.with_overrides(cookie_assume_valid=False), None,
            bridge_with(self.config, cookies))
        self.assertFalse(strict.is_logged_in(channel=CHANNEL_COOKIE))


class ChannelSelectionTest(unittest.TestCase):
    """通道自动选择：有驱动用 browser，无驱动有 Cookie 用 http。"""

    def test_browser_channel_chosen_when_driver_present(self):
        config = SessionConfig(domain="example.com").finalize()
        driver = FakeTextDriver("我的账户 退出登录", url="https://example.com/me")
        checker = LoginStateChecker(config, driver, bridge_with(config))
        result = checker.check()
        self.assertEqual(result.channel, CHANNEL_BROWSER)

    def test_explicit_channel_override(self):
        config = SessionConfig(domain="example.com").finalize()
        cookies = [normalize_cookie({"name": "sid", "value": "1",
                                     "domain": "example.com"})]
        checker = LoginStateChecker(config, None, bridge_with(config, cookies))
        result = checker.check(channel=CHANNEL_COOKIE)
        self.assertEqual(result.channel, CHANNEL_COOKIE)

    def test_unknown_channel_raises_value_error(self):
        config = SessionConfig(domain="example.com").finalize()
        checker = LoginStateChecker(config, None, bridge_with(config))
        with self.assertRaises(ValueError):
            checker.check(channel="telnet")


if __name__ == "__main__":
    unittest.main()
