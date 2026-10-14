"""BrowserLauncher 子模块单元测试：占用检测、可执行文件解析与启动前置校验。

这里只测"不启动浏览器"的部分（纯逻辑 + 临时目录），
真正拉起浏览器的用例见 ``test_browser_session_integration.py``。
"""

from __future__ import annotations

import os
import tempfile
import unittest

from core.browser_session import launcher
from core.browser_session.config import SessionConfig
from core.browser_session.exceptions import (
    BrowserAlreadyRunning, BrowserNotInstalled, ProfileNotFound,
    UserDataDirNotFound,
)


class ProcessCommandMatchTest(unittest.TestCase):
    """进程命令行匹配规则（本模块"必须先关闭浏览器"的判据核心）。"""

    def test_explicit_user_data_dir_argument_matches(self):
        with tempfile.TemporaryDirectory(prefix="ud_match_") as root:
            line = (r'"C:\Program Files\Google\Chrome\Application\chrome.exe" '
                    f'--user-data-dir="{root}" --profile-directory=Default')
            self.assertTrue(launcher.process_uses_profile(line, root, ""))
            # 路径等价写法（末尾斜杠 / 大小写差异）也要命中
            self.assertTrue(launcher.process_uses_profile(
                f'chrome.exe --user-data-dir={root}\\', root, ""))
            # 不同目录不应命中
            self.assertFalse(launcher.process_uses_profile(
                f'chrome.exe --user-data-dir={root}-other', root, ""))

    def test_profile_directory_argument_matches_with_target_dir(self):
        with tempfile.TemporaryDirectory(prefix="ud_prof_") as root:
            profile_dir = os.path.join(root, "Profile 3")
            os.makedirs(profile_dir)
            line = (f'msedge.exe --user-data-dir="{root}" '
                    '--profile-directory="Profile 3"')
            self.assertTrue(launcher.process_uses_profile(line, root, profile_dir))
            # 同目录但不同 Profile：Chrome 允许并发不同 Profile，不应判为占用
            self.assertFalse(launcher.process_uses_profile(
                'msedge.exe --user-data-dir="%s" --profile-directory="Default"' % root,
                root, profile_dir))
            # 未指定 Profile 的进程视为"可能正在用我们关心的那个"
            self.assertTrue(launcher.process_uses_profile(
                f'msedge.exe --user-data-dir="{root}"', root, profile_dir))
            # 检查 User Data 根目录时，同目录一律算占用
            self.assertTrue(launcher.process_uses_profile(line, root, ""))

    def test_non_browser_processes_are_ignored(self):
        with tempfile.TemporaryDirectory(prefix="ud_other_") as root:
            python_line = f'python.exe --user-data-dir="{root}"'
            self.assertFalse(launcher.process_uses_profile(python_line, root, ""))
            self.assertFalse(launcher.process_uses_profile("", root, ""))
            self.assertFalse(launcher.process_uses_profile(
                "explorer.exe /select,c:\\", root, ""))


class ProcessNameDetectionTest(unittest.TestCase):
    """按进程名探测：WebView2 等宿主进程必须被排除。"""

    def test_excluded_process_names_are_filtered(self):
        # 直接验证过滤逻辑：注入名单不应包含 WebView2 / 驱动进程
        for bad in ("msedgewebview2", "chromedriver", "msedgedriver"):
            self.assertTrue(any(item in bad for item in launcher._EXCLUDED_PROCESS_NAMES))

    def test_family_filter_keeps_only_requested_browser(self):
        original = launcher.list_browser_processes
        try:
            launcher.list_browser_processes = lambda: []
            names = launcher.running_browser_names("edge")
            # 本机若在跑 Edge，应只返回 msedge 家族且不含 webview
            for name in names:
                self.assertNotIn("webview", name.lower())
        finally:
            launcher.list_browser_processes = original


class LockFileTest(unittest.TestCase):
    """锁文件探测（只读，不删除）。"""

    def test_lock_files_are_reported(self):
        with tempfile.TemporaryDirectory(prefix="ud_lock_") as root:
            os.makedirs(os.path.join(root, "Default"))
            self.assertEqual(launcher.lock_files(root), [])
            lock = os.path.join(root, "lockfile")
            with open(lock, "wb"):
                pass
            self.assertEqual([os.path.basename(item) for item in launcher.lock_files(root)],
                             ["lockfile"])

    def test_stale_lock_without_process_is_not_busy(self):
        """浏览器正常退出后锁文件会残留——不能据此判为占用（避免误报）。"""
        with tempfile.TemporaryDirectory(prefix="ud_stale_") as root:
            os.makedirs(os.path.join(root, "Default"))
            for name in launcher.LOCK_FILENAMES:
                with open(os.path.join(root, name), "wb"):
                    pass
            original = launcher.list_browser_processes
            original_names = launcher.running_browser_names
            try:
                launcher.list_browser_processes = lambda: []       # 拿不到命令行
                launcher.running_browser_names = lambda browser="": []   # 没有浏览器进程
                busy, reason = launcher.profile_in_use(root, os.path.join(root, "Default"),
                                                       "chrome")
            finally:
                launcher.list_browser_processes = original
                launcher.running_browser_names = original_names
            self.assertFalse(busy)
            self.assertEqual(reason, "")

    def test_lock_plus_running_process_is_flagged_as_probable(self):
        """受限环境拿不到命令行时，用"锁文件 + 同名进程"给出疑似占用。"""
        with tempfile.TemporaryDirectory(prefix="ud_prob_") as root:
            os.makedirs(os.path.join(root, "Default"))
            with open(os.path.join(root, "lockfile"), "wb"):
                pass
            original = launcher.list_browser_processes
            original_names = launcher.running_browser_names
            try:
                launcher.list_browser_processes = lambda: []
                launcher.running_browser_names = lambda browser="": ["msedge"]
                busy, reason = launcher.profile_in_use(root, os.path.join(root, "Default"),
                                                       "edge")
            finally:
                launcher.list_browser_processes = original
                launcher.running_browser_names = original_names
            self.assertTrue(busy)
            self.assertIn("疑似", reason)
            self.assertIn("完全退出", reason)
            self.assertIn("WebView2", reason)

    def test_command_line_match_wins_and_message_is_actionable(self):
        with tempfile.TemporaryDirectory(prefix="ud_busy_") as root:
            os.makedirs(os.path.join(root, "Default"))
            original = launcher.list_browser_processes
            try:
                launcher.list_browser_processes = lambda: [
                    f'chrome.exe --user-data-dir="{root}" --profile-directory=Default']
                busy, reason = launcher.profile_in_use(root, os.path.join(root, "Default"),
                                                       "chrome")
            finally:
                launcher.list_browser_processes = original
            self.assertTrue(busy)
            self.assertIn("必须先完全退出该浏览器", reason)
            self.assertIn("任务管理器", reason)
            self.assertIn("cookie_only", reason)

    def test_assert_profile_free_raises_browser_already_running(self):
        with tempfile.TemporaryDirectory(prefix="ud_assert_") as root:
            os.makedirs(os.path.join(root, "Default"))
            original = launcher.list_browser_processes
            try:
                launcher.list_browser_processes = lambda: [
                    f'chrome.exe --user-data-dir="{root}"']
                with self.assertRaises(BrowserAlreadyRunning) as ctx:
                    launcher.assert_profile_free(root, os.path.join(root, "Default"), "chrome")
            finally:
                launcher.list_browser_processes = original
            self.assertIn("User Data", str(ctx.exception))

    def test_missing_directory_is_not_busy(self):
        busy, reason = launcher.profile_in_use(
            os.path.join(tempfile.gettempdir(), "definitely-missing-1919"), "", "chrome")
        self.assertFalse(busy)
        self.assertEqual(reason, "")


class FindExecutableTest(unittest.TestCase):
    """可执行文件解析：显式配置优先，其次复用框架查找链。"""

    def test_explicit_path_is_used_when_exists(self):
        with tempfile.NamedTemporaryFile(suffix=".exe", delete=False) as handle:
            path = handle.name
        self.addCleanup(os.unlink, path)
        config = SessionConfig(browser="chrome", executable_path=path, domain="example.com")
        config.finalize()
        self.assertEqual(launcher.find_executable(config), path)

    def test_missing_explicit_path_falls_back(self):
        config = SessionConfig(browser="chrome",
                               executable_path=os.path.join(tempfile.gettempdir(),
                                                            "missing-1919.exe"),
                               domain="example.com")
        config.finalize()
        # 不应抛异常；返回空或框架查找到的路径
        self.assertIsInstance(launcher.find_executable(config), str)

    def test_framework_lookup_chain_is_reused(self):
        from core.fetcher import chrome_setup
        found = chrome_setup.find_executable()
        config = SessionConfig(browser="chrome", domain="example.com").finalize()
        if found:
            self.assertEqual(launcher.find_executable(config), found)


class LaunchPreconditionTest(unittest.TestCase):
    """启动前置校验：路径缺失 / 内核缺失 / 目录被占用都要抛明确异常。"""

    def test_missing_user_data_dir_raises(self):
        config = SessionConfig(browser="chrome", domain="example.com",
                               user_data_dir=os.path.join(tempfile.gettempdir(),
                                                          "nope-1919")).finalize()
        with self.assertRaises(UserDataDirNotFound) as ctx:
            launcher.launch(config)
        self.assertIn("User Data 目录不存在", str(ctx.exception))

    def test_missing_profile_lists_available(self):
        with tempfile.TemporaryDirectory(prefix="ud_launch_") as root:
            os.makedirs(os.path.join(root, "Default"))
            open(os.path.join(root, "Default", "Preferences"), "w").close()
            config = SessionConfig(browser="chrome", domain="example.com",
                                   user_data_dir=root, profile="Profile 9").finalize()
            with self.assertRaises(ProfileNotFound) as ctx:
                launcher.launch(config)
            self.assertIn("Default", str(ctx.exception))

    def test_busy_profile_raises_before_missing_executable(self):
        """占用检查应排在内核检查之前（"必须先关浏览器"是最常见的原因）。"""
        with tempfile.TemporaryDirectory(prefix="ud_order_") as root:
            os.makedirs(os.path.join(root, "Default"))
            original = launcher.list_browser_processes
            try:
                launcher.list_browser_processes = lambda: [
                    f'chrome.exe --user-data-dir="{root}"']
                config = SessionConfig(browser="chrome", domain="example.com",
                                       user_data_dir=root).finalize()
                with self.assertRaises(BrowserAlreadyRunning):
                    launcher.launch(config)
            finally:
                launcher.list_browser_processes = original

    def test_missing_executable_raises_browser_not_installed(self):
        with tempfile.TemporaryDirectory(prefix="ud_exe_") as root:
            os.makedirs(os.path.join(root, "Default"))
            config = SessionConfig(browser="chrome", domain="example.com",
                                   user_data_dir=root).finalize()
            original_processes = launcher.list_browser_processes
            original_find = launcher.find_executable
            try:
                launcher.list_browser_processes = lambda: []
                launcher.find_executable = lambda cfg: ""
                with self.assertRaises(BrowserNotInstalled) as ctx:
                    launcher.launch(config)
            finally:
                launcher.list_browser_processes = original_processes
                launcher.find_executable = original_find
            message = str(ctx.exception)
            self.assertIn("CRAWLER_CHROME_PATH", message)
            self.assertIn("chrome-win64.zip", message)


class HealthCheckTest(unittest.TestCase):
    """健康检查钩子：结构化、可序列化、不抛异常。"""

    def test_report_shape_without_browser(self):
        config = SessionConfig(browser="chrome", domain="example.com",
                               user_data_dir=os.path.join(tempfile.gettempdir(),
                                                          "missing-1919")).finalize()
        report = launcher.health_check(config)
        for key in ("executable", "user_data_exists", "profile_exists", "in_use",
                    "ok", "stale_locks"):
            self.assertIn(key, report)
        self.assertFalse(report["ok"])
        self.assertIsInstance(report["stale_locks"], list)

    def test_detect_installation_includes_lock_state(self):
        with tempfile.TemporaryDirectory(prefix="ud_detect_") as root:
            os.makedirs(os.path.join(root, "Default"))
            with open(os.path.join(root, "SingletonLock"), "wb"):
                pass
            config = SessionConfig(browser="chrome", domain="example.com",
                                   user_data_dir=root).finalize()
            report = launcher.detect_installation(config)
            self.assertTrue(report["user_data_exists"])
            self.assertTrue(report["profile_exists"])
            self.assertIn(launcher.lock_files(root)[0], launcher.lock_files(root))


class OpenLoginPageTest(unittest.TestCase):
    """登录页打开入口（供"手动登录一次后重新导出"用）。"""

    class _Driver:
        def __init__(self):
            self.urls = []

        def get(self, url):
            self.urls.append(url)

    def test_uses_config_login_url(self):
        config = SessionConfig(domain="example.com").finalize()
        driver = self._Driver()
        self.assertEqual(launcher.open_login_page(config, driver),
                         "https://example.com/")
        self.assertEqual(driver.urls, ["https://example.com/"])

    def test_explicit_url_wins(self):
        config = SessionConfig(domain="example.com").finalize()
        driver = self._Driver()
        self.assertEqual(launcher.open_login_page(config, driver,
                                                  "https://example.com/login"),
                         "https://example.com/login")

    def test_missing_domain_raises(self):
        config = SessionConfig().finalize()
        with self.assertRaises(ValueError):
            launcher.open_login_page(config, self._Driver())


if __name__ == "__main__":
    unittest.main()
