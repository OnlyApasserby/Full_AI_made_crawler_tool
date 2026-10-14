"""Config 子模块单元测试：默认值、环境变量覆盖、路径探测与校验。

这些用例**不依赖**本机是否安装浏览器，也不访问网络。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

from core.browser_session.config import (
    COOKIE_ACCEPT_SAME_SITE, HEADLESS_NEW, HEADLESS_OFF, SessionConfig,
    default_profile_dir, default_user_data_dir, detect_installed_browsers,
    has_login_data, list_profiles, normalize_domain, sanitize_filename,
    target_domains,
)
from core.browser_session.exceptions import ProfileNotFound, UserDataDirNotFound


class DomainHelpersTest(unittest.TestCase):
    """域名归一化与父域推导。"""

    def test_normalize_domain_accepts_url_host_and_port(self):
        for value, expected in (
            ("https://WWW.Example.com/path?q=1", "www.example.com"),
            ("example.com:8443", "example.com"),
            ("user:pw@example.com", "example.com"),
            (".example.com", "example.com"),
            ("example.com/", "example.com"),
            ("", ""),
        ):
            with self.subTest(value=value):
                self.assertEqual(normalize_domain(value), expected)

    def test_target_domains_walks_up_parent_domains(self):
        self.assertEqual(target_domains("www.a.example.com"),
                         ["www.a.example.com", "a.example.com", "example.com"])
        self.assertEqual(target_domains("example.com"), ["example.com"])
        self.assertEqual(target_domains(""), [])

    def test_sanitize_filename_strips_unsafe_characters(self):
        self.assertEqual(sanitize_filename("a/b:c*d"), "a_b_c_d")
        self.assertEqual(sanitize_filename(""), "session")


class DefaultPathDetectionTest(unittest.TestCase):
    """平台默认目录探测（只读，不创建目录）。"""

    def test_default_user_data_dir_matches_platform_convention(self):
        path = default_user_data_dir("chrome")
        if not path:                      # 未安装 Chrome 的环境直接跳过
            self.skipTest("本机未安装 Chrome")
        self.assertTrue(os.path.isdir(path))
        if sys.platform.startswith("win"):
            # 正式 Chrome 与 Chrome for Testing 都算合规（后者优先级更低）
            self.assertTrue(path.endswith(os.path.join("Google", "Chrome", "User Data"))
                            or path.endswith(os.path.join("Google", "Chrome for Testing",
                                                          "User Data")))
        elif sys.platform == "darwin":
            self.assertIn("Application Support", path)
        else:
            self.assertTrue(".config" in path or "snap" in path)

    def test_chrome_for_testing_has_lower_priority_than_chrome(self):
        """正式 Chrome 存在时不应被 Chrome for Testing 抢占（后者几乎没有登录态）。"""
        detected = default_user_data_dir("chrome")
        if not detected:
            self.skipTest("本机未安装任何 Chrome")
        with_testing = os.path.join("Google", "Chrome for Testing", "User Data")
        system_chrome = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                                     "Google", "Chrome", "User Data")
        if os.path.isdir(system_chrome) and detected.endswith(with_testing):
            self.fail("正式 Chrome 存在时却选中了 Chrome for Testing")

    def test_default_user_data_dir_edge_and_unknown_kind(self):
        edge = default_user_data_dir("edge")
        if edge:
            self.assertTrue(os.path.isdir(edge))
        # 未知浏览器类型按 chrome 处理，不应抛异常
        self.assertIsInstance(default_user_data_dir("unknown-browser"), str)

    def test_detect_installed_browsers_returns_existing_dirs_only(self):
        found = detect_installed_browsers()
        self.assertIsInstance(found, dict)
        for kind, path in found.items():
            with self.subTest(kind=kind):
                self.assertTrue(os.path.isdir(path))

    def test_list_profiles_reports_only_profile_dirs(self):
        root = tempfile.mkdtemp(prefix="ud_test_")
        self.addCleanup(_rmtree, root)
        # 造一个"像 Profile"的目录与一个无关目录
        os.makedirs(os.path.join(root, "Default"))
        open(os.path.join(root, "Default", "Preferences"), "w").close()
        os.makedirs(os.path.join(root, "Profile 1"))
        open(os.path.join(root, "Profile 1", "History"), "w").close()
        os.makedirs(os.path.join(root, "Crashpad"))
        self.assertEqual(list_profiles(root), ["Default", "Profile 1"])
        # 目录不存在时返回空列表
        self.assertEqual(list_profiles(os.path.join(root, "nope")), [])

    def test_list_profiles_require_login_filters_empty_profiles(self):
        root = tempfile.mkdtemp(prefix="ud_login_")
        self.addCleanup(_rmtree, root)
        profile = os.path.join(root, "Default", "Network")
        os.makedirs(profile)
        cookies = os.path.join(profile, "Cookies")
        with open(cookies, "wb") as stream:
            stream.write(b"SQLite format 3\x00")     # 非空即可
        self.assertTrue(has_login_data(os.path.join(root, "Default")))
        self.assertEqual(list_profiles(root, require_login=True), ["Default"])
        # 清空 Cookies 后应被过滤掉
        with open(cookies, "wb"):
            pass
        self.assertFalse(has_login_data(os.path.join(root, "Default")))
        self.assertEqual(list_profiles(root, require_login=True), [])

    def test_default_profile_dir_prefers_default_then_first(self):
        root = tempfile.mkdtemp(prefix="ud_profile_")
        self.addCleanup(_rmtree, root)
        os.makedirs(os.path.join(root, "Profile 2"))
        open(os.path.join(root, "Profile 2", "Preferences"), "w").close()
        # 没有 Default 时挑第一个可用 Profile
        self.assertEqual(default_profile_dir(root),
                         os.path.join(root, "Profile 2"))
        os.makedirs(os.path.join(root, "Default"))
        open(os.path.join(root, "Default", "Preferences"), "w").close()
        self.assertEqual(default_profile_dir(root), os.path.join(root, "Default"))
        # 显式指定优先于自动挑选
        self.assertEqual(default_profile_dir(root, "Profile 2"),
                         os.path.join(root, "Profile 2"))
        # 空目录退化为 <root>/Default（不抛异常）
        empty = os.path.join(root, "empty")
        os.makedirs(empty)
        self.assertEqual(default_profile_dir(empty), os.path.join(empty, "Default"))


class SessionConfigDefaultsTest(unittest.TestCase):
    """默认值与字段规范化。"""

    def test_defaults_come_from_framework_config(self):
        import config as app_config
        cfg = SessionConfig().finalize()
        self.assertEqual(cfg.browser, app_config.DEFAULT_SESSION_BROWSER)
        self.assertEqual(cfg.profile, app_config.DEFAULT_SESSION_PROFILE)
        self.assertEqual(cfg.launcher, app_config.DEFAULT_SESSION_LAUNCHER)
        self.assertTrue(cfg.cookie_encrypt)
        # Cookie 路径默认落在 resources/sessions/<domain>.json
        self.assertTrue(cfg.cookie_store_path.endswith(".json"))
        self.assertIn(app_config.SESSIONS_DIRNAME, cfg.cookie_store_path)

    def test_finalize_builds_login_url_and_domains(self):
        cfg = SessionConfig(domain="https://www.example.com/x").finalize()
        self.assertEqual(cfg.domain, "www.example.com")
        self.assertEqual(cfg.login_url, "https://www.example.com/")
        self.assertEqual(cfg.domains[0], "www.example.com")
        self.assertIn("example.com", cfg.domains)

    def test_headless_mode_normalization(self):
        self.assertEqual(SessionConfig(headless=False).headless_mode, HEADLESS_OFF)
        self.assertEqual(SessionConfig(headless=True).headless_mode, HEADLESS_NEW)
        self.assertEqual(SessionConfig(headless="new").headless_mode, HEADLESS_NEW)
        self.assertEqual(SessionConfig(headless="off").headless_mode, HEADLESS_OFF)

    def test_coerce_converts_string_values(self):
        cfg = SessionConfig.from_mapping({
            "headless": "true", "cookie_encrypt": "0", "viewport": "1280x720",
            "check_timeout": "7.5", "proxy": "http://127.0.0.1:8080",
            "logout_markers": "请登录, 登录已过期", "login_status": "200,302",
        })
        self.assertTrue(cfg.headless)
        self.assertFalse(cfg.cookie_encrypt)
        self.assertEqual(cfg.viewport, (1280, 720))
        self.assertEqual(cfg.check_timeout, 7.5)
        self.assertEqual(cfg.proxy["https"], "http://127.0.0.1:8080")
        self.assertEqual(cfg.logout_markers, ["请登录", "登录已过期"])
        self.assertEqual(cfg.login_status, [200, 302])

    def test_from_mapping_accepts_aliases_and_ignores_unknown(self):
        cfg = SessionConfig.from_mapping({
            "user_data": r"C:\tmp\ud", "profile_directory": "Profile 3",
            "cookies_path": r"C:\tmp\c.json", "target_domain": "example.com",
            "not_a_field": 1,
        })
        self.assertEqual(cfg.user_data_dir, os.path.abspath(r"C:\tmp\ud"))
        self.assertEqual(cfg.profile, "Profile 3")
        self.assertTrue(cfg.cookie_store_path.endswith("c.json"))
        self.assertEqual(cfg.domain, "example.com")

    def test_with_overrides_does_not_mutate_original(self):
        base = SessionConfig(domain="a.com").finalize()
        derived = base.with_overrides(domain="b.com", profile="Profile 9")
        self.assertEqual(base.domain, "a.com")
        self.assertEqual(base.profile, "Default")
        self.assertEqual(derived.domain, "b.com")
        self.assertEqual(derived.profile, "Profile 9")

    def test_describe_never_leaks_key(self):
        cfg = SessionConfig(domain="example.com", cookie_key="super-secret").finalize()
        self.assertNotIn("super-secret", cfg.describe())
        self.assertNotIn("cookie_key", cfg.to_dict())
        self.assertIn("已配置", cfg.cookie_key_source)


class SessionConfigEnvOverrideTest(unittest.TestCase):
    """环境变量覆盖（含框架既有的 CRAWLER_CHROME_PATH）。"""

    def test_apply_env_overrides_known_fields(self):
        prefix = "CRAWLER_SESSION_"
        env = {
            prefix + "BROWSER": "EDGE",
            prefix + "PROFILE": "Profile 2",
            prefix + "DOMAIN": "https://Sub.Example.com/a",
            prefix + "HEADLESS": "1",
            prefix + "CHECK_TIMEOUT": "3",
            prefix + "LOGIN_MARKERS": "已登录,欢迎",
            prefix + "COOKIE_STORE": os.path.join(tempfile.gettempdir(), "c.json"),
        }
        cfg = SessionConfig().apply_env(env).finalize()
        self.assertEqual(cfg.browser, "edge")            # 大小写归一
        self.assertEqual(cfg.profile, "Profile 2")
        self.assertEqual(cfg.domain, "sub.example.com")
        self.assertEqual(cfg.headless_mode, HEADLESS_NEW)
        self.assertEqual(cfg.check_timeout, 3.0)
        self.assertEqual(cfg.login_markers, ["已登录", "欢迎"])
        self.assertTrue(cfg.cookie_store_path.endswith("c.json"))

    def test_resolve_priority_env_then_explicit(self):
        prefix = "CRAWLER_SESSION_"
        env = {prefix + "PROFILE": "FromEnv"}
        original = os.environ.copy()
        try:
            os.environ.update(env)
            cfg = SessionConfig.resolve({"profile": "FromDict"})
            self.assertEqual(cfg.profile, "FromEnv")      # 环境变量优先于字典
            cfg = SessionConfig.resolve({"profile": "FromDict"}, profile="FromArg")
            self.assertEqual(cfg.profile, "FromArg")      # 显式传参优先于环境变量
        finally:
            os.environ.clear()
            os.environ.update(original)

    def test_cookie_key_read_from_dedicated_env(self):
        import config as app_config
        original = os.environ.get(app_config.DEFAULT_SESSION_COOKIE_KEY_ENV)
        try:
            os.environ[app_config.DEFAULT_SESSION_COOKIE_KEY_ENV] = "env-key"
            cfg = SessionConfig().apply_env().finalize()
            self.assertEqual(cfg.cookie_key, "env-key")
        finally:
            if original is None:
                os.environ.pop(app_config.DEFAULT_SESSION_COOKIE_KEY_ENV, None)
            else:
                os.environ[app_config.DEFAULT_SESSION_COOKIE_KEY_ENV] = original

    def test_executable_path_falls_back_to_framework_env(self):
        import config as app_config
        original = os.environ.get(app_config.BROWSER_EXECUTABLE_ENV)
        try:
            os.environ[app_config.BROWSER_EXECUTABLE_ENV] = r"C:\fake\chrome.exe"
            cfg = SessionConfig().apply_env().finalize()
            self.assertEqual(cfg.executable_path, os.path.abspath(r"C:\fake\chrome.exe"))
        finally:
            if original is None:
                os.environ.pop(app_config.BROWSER_EXECUTABLE_ENV, None)
            else:
                os.environ[app_config.BROWSER_EXECUTABLE_ENV] = original


class SessionConfigValidationTest(unittest.TestCase):
    """校验：取值非法、目录不存在、Profile 不存在都要给出明确异常。"""

    def test_invalid_enum_values_raise_value_error(self):
        with self.assertRaises(ValueError):
            SessionConfig(browser="firefox", domain="example.com").validate(
                require_user_data=False)
        with self.assertRaises(ValueError):
            SessionConfig(launcher="puppeteer", domain="example.com").validate(
                require_user_data=False)
        with self.assertRaises(ValueError):
            SessionConfig(cookie_accept="whatever", domain="example.com").validate(
                require_user_data=False)

    def test_missing_domain_raises_value_error(self):
        with self.assertRaises(ValueError) as ctx:
            SessionConfig().validate(require_user_data=False)
        self.assertIn("目标域名", str(ctx.exception))

    def test_missing_user_data_dir_raises_with_hint(self):
        cfg = SessionConfig(domain="example.com",
                            user_data_dir=os.path.join(tempfile.gettempdir(),
                                                       "definitely-not-here-1919"),
                            profile="Default")
        with self.assertRaises(UserDataDirNotFound) as ctx:
            cfg.validate()
        message = str(ctx.exception)
        self.assertIn("User Data 目录不存在", message)
        self.assertIn("至少启动过一次", message)

    def test_missing_profile_lists_available_profiles(self):
        root = tempfile.mkdtemp(prefix="ud_validate_")
        self.addCleanup(_rmtree, root)
        os.makedirs(os.path.join(root, "Default"))
        open(os.path.join(root, "Default", "Preferences"), "w").close()
        cfg = SessionConfig(domain="example.com", user_data_dir=root,
                            profile="Profile 7").finalize()
        with self.assertRaises(ProfileNotFound) as ctx:
            cfg.validate()
        message = str(ctx.exception)
        self.assertIn("Profile 不存在", message)
        self.assertIn("Default", message)          # 提示里列出可用 Profile

    def test_valid_temp_profile_passes(self):
        root = tempfile.mkdtemp(prefix="ud_ok_")
        self.addCleanup(_rmtree, root)
        os.makedirs(os.path.join(root, "Default"))
        # 造出"用过的 Profile"的特征文件，否则会被判为"浏览器从未启动过"
        open(os.path.join(root, "Default", "Preferences"), "w").close()
        cfg = SessionConfig(domain="example.com", user_data_dir=root).finalize()
        cfg.validate()                              # 不应抛异常
        self.assertTrue(cfg.profile_dir.endswith("Default"))

    def test_empty_profile_dir_reports_never_started(self):
        """Profile 名对但内容为空（如 Chrome for Testing 装好没用过）应给出专门提示。"""
        root = tempfile.mkdtemp(prefix="ud_empty_")
        self.addCleanup(_rmtree, root)
        # 目录存在、能被识别为 Profile，但没有任何 Preferences/Cookies 内容
        os.makedirs(os.path.join(root, "Default", "Network"))
        cfg = SessionConfig(domain="example.com", user_data_dir=root).finalize()
        with self.assertRaises(ProfileNotFound) as ctx:
            cfg.validate()
        message = str(ctx.exception)
        self.assertIn("内容为空", message)
        self.assertIn("还没有被正常启动过", message)
        self.assertIn("先手动启动一次", message)


class CookieAcceptModeTest(unittest.TestCase):
    """接收策略归一化。"""

    def test_same_site_mode_preserved(self):
        cfg = SessionConfig(domain="example.com",
                            cookie_accept=COOKIE_ACCEPT_SAME_SITE).finalize()
        self.assertEqual(cfg.cookie_accept, COOKIE_ACCEPT_SAME_SITE)


def _rmtree(path: str) -> None:
    import shutil
    shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
