"""BrowserLauncher 子模块：按配置启动浏览器实例，并加载本地 User Data 登录态。

核心行为
--------
1. 解析浏览器可执行文件（优先复用 :mod:`core.fetcher.chrome_setup` 的查找链，
   即 ``CRAWLER_CHROME_PATH`` → ``resources/browsers`` → 系统安装 → PATH）
2. **启动前检测 User Data 是否被运行中的浏览器占用**，占用则抛
   :class:`~core.browser_session.exceptions.BrowserAlreadyRunning` 并给出处理步骤
3. 通过 ``--user-data-dir`` + ``--profile-directory`` 让浏览器直接带着登录态启动
4. 返回统一驱动对象（Selenium / Playwright 由 :mod:`.adapters` 适配同一接口）

为什么"必须先关闭浏览器"不能靠猜
--------------------------------
Chromium 对同一个 ``--user-data-dir`` 是**独占**的。若已有实例在跑，新进程会
把启动请求转交给它然后**自己退出**——自动化侧只能拿到一个空壳或直接超时，
这正是"静默失败"最典型的来源。本模块因此做了两层检测：

- **锁文件**：``SingletonLock``（macOS/Linux）+ ``lockfile`` / ``SingletonCookie``
  （全平台；Windows 上 ``lockfile`` 由运行中的实例持有并阻止删除）
- **进程命令行**：在 Windows 上用 ``Get-CimInstance Win32_Process``（WMI，无需
  额外依赖），在 POSIX 上用 ``ps -eo args``，匹配 ``--user-data-dir=<目标目录>``
  以及带目标 Profile 的浏览器进程

两层都**只读**，不会修改用户的 Profile；任何检测异常都降级为"未占用"，
避免检测器本身成为新的失败点。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time

from core.browser_session.adapters import (
    BrowserDriver, create_driver,
)
# 注意：adapters 在导入时即完成默认启动器注册（register_launcher），
# 因此这里只需要 create_driver，不需要显式再注册一次。
from core.browser_session.config import (
    BROWSER_EXECUTABLES, SessionConfig, normalize_domain,
)
from core.browser_session.exceptions import (
    BrowserAlreadyRunning, BrowserNotInstalled, ProfileNotFound,
    UserDataDirNotFound,
)
from utils.logging_setup import get_logger

logger = get_logger("core.browser_session.launcher")

#: Chromium 系在同一 User Data 目录下的锁/哨兵文件（存在其一即认为目录被占用）
LOCK_FILENAMES = ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile")
#: 进程扫描时识别浏览器进程的关键词（小写）
_BROWSER_PROCESS_HINTS = ("chrome", "msedge", "chromium", "edge", "google-chrome")
#: 各浏览器的进程名特征（用于"锁文件 + 同名进程"兜底判定与提示）
_BROWSER_PROCESS_NAMES: dict[str, tuple[str, ...]] = {
    "chrome": ("chrome", "chromium", "google-chrome"),
    "edge": ("msedge", "microsoft-edge"),
    "chromium": ("chromium", "chrome"),
}
#: 明确要排除的进程名：WebView2 宿主（msedgewebview2）、驱动、崩溃处理器等
#: ——它们携带 "edge"/"chrome" 字样，但**不是**占用 User Data 的浏览器主体
_EXCLUDED_PROCESS_NAMES = ("webview", "chromedriver", "msedgedriver", "crashpad",
                           "elevation_service", "notification_helper")
#: 进程扫描超时（秒）——检测器自身不能拖慢启动
_SCAN_TIMEOUT = 8.0
#: 进程扫描结果缓存时长（秒）：一次交互里"启动检查 + 健康检查"会多次调用，
#: 每次起一个 PowerShell 太慢；结果只在极短时间内复用，不会掩盖"用户刚关掉浏览器"
_PROCESS_CACHE_TTL = 1.5
_process_cache: tuple[float, list[str]] | None = None
_process_cache_lock = threading.Lock()
#: 依赖 CDP/自动化端口的进程参数（出现即认定为自动化实例）
_AUTOMATION_HINTS = ("--remote-debugging-port", "--enable-automation")


# ---------------------------------------------------------------------------
# 路径解析
# ---------------------------------------------------------------------------
def find_executable(config: SessionConfig) -> str:
    """解析浏览器可执行文件路径，未找到返回空串。

    查找顺序：显式配置 → 框架既有查找链（``CRAWLER_CHROME_PATH`` /
    ``resources/browsers`` / 系统安装 / PATH）。
    """
    if config.executable_path and os.path.isfile(config.executable_path):
        return config.executable_path
    if config.executable_path:
        logger.warning("配置的可执行文件不存在，改为自动查找：%s", config.executable_path)
    try:
        from core.fetcher import chrome_setup
    except ImportError:  # 独立使用时也允许（浏览器登录态模块不强依赖抓取层）
        chrome_setup = None
    if chrome_setup is not None:
        found = chrome_setup.find_executable()
        if found:
            return found
    names = BROWSER_EXECUTABLES.get(config.browser, BROWSER_EXECUTABLES["chrome"])
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return ""


def detect_installation(config: SessionConfig) -> dict:
    """探测浏览器安装情况，返回可读报告（供界面展示与自检使用）。"""
    executable = find_executable(config)
    report = {
        "browser": config.browser,
        "executable": executable,
        "user_data_dir": config.user_data_dir,
        "profile_dir": config.profile_dir,
        "user_data_exists": bool(config.user_data_dir) and os.path.isdir(config.user_data_dir),
        "profile_exists": bool(config.profile_dir) and os.path.isdir(config.profile_dir),
        "in_use": False,
        "reason": "",
    }
    if config.user_data_dir and os.path.isdir(config.user_data_dir):
        busy, reason = profile_in_use(config.user_data_dir, config.profile_dir,
                                  config.browser)
        report["in_use"] = busy
        report["reason"] = reason
    return report


# ---------------------------------------------------------------------------
# 占用检测
# ---------------------------------------------------------------------------
def lock_files(user_data_dir: str, profile_dir: str = "") -> list[str]:
    """返回 User Data / Profile 目录下已存在的锁文件路径（只读检查）。"""
    found: list[str] = []
    for base in (user_data_dir, profile_dir):
        if not base or not os.path.isdir(base):
            continue
        for name in LOCK_FILENAMES:
            path = os.path.join(base, name)
            if os.path.lexists(path):
                found.append(path)
    return found


def list_browser_processes(use_cache: bool = True) -> list[str]:
    """返回当前运行的浏览器进程命令行列表（失败时返回空列表，绝不抛异常）。

    依次尝试（越靠前信息越完整）：

    1. Windows：``Get-CimInstance Win32_Process``（能拿到**命令行**，最准）
    2. Windows：``wmic process get commandline``（老系统兜底）
    3. POSIX：``ps -eo args``

    受限环境下（如企业策略或沙箱禁止 WMI）可能拿不到命令行，此时返回空列表并
    记 debug 日志，由 :func:`profile_in_use` 的"锁文件 + 进程名"兜底判断接手。

    结果带极短 TTL 缓存（见 :data:`_PROCESS_CACHE_TTL`），避免"启动检查 + 健康
    检查"在同一个操作里反复拉起 PowerShell。

    :param use_cache: 传 False 强制重新扫描（用户手动重试时用）
    """
    global _process_cache
    now = time.monotonic()
    if use_cache and _process_cache is not None \
            and now - _process_cache[0] < _PROCESS_CACHE_TTL:
        return list(_process_cache[1])
    commands = _scan_browser_processes()
    with _process_cache_lock:
        _process_cache = (now, list(commands))
    return commands


def _scan_browser_processes() -> list[str]:
    """真正执行进程扫描（不带缓存）。"""
    try:
        if sys.platform.startswith("win"):
            return _windows_process_commands()
        result = subprocess.run(["ps", "-eo", "args"], capture_output=True,
                                text=True, timeout=_SCAN_TIMEOUT)
        return [line.strip() for line in (result.stdout or "").splitlines()
                if line.strip() and any(hint in line.lower() for hint in _BROWSER_PROCESS_HINTS)]
    except Exception as exc:  # 检测失败不应影响启动流程
        logger.debug("进程扫描失败（按未占用处理）：%s", exc)
        return []


def reset_process_cache() -> None:
    """清空进程扫描缓存（测试隔离；也用于"我刚刚关了浏览器"后强制重扫）。"""
    global _process_cache
    with _process_cache_lock:
        _process_cache = None


def _windows_process_commands() -> list[str]:
    """Windows 下获取浏览器进程命令行（多策略，全部失败返回空列表）。"""
    patterns = "|".join(("chrome", "msedge", "chromium", "edge"))
    attempts = (
        # PowerShell 7 / Windows PowerShell 都支持 CIM
        ("powershell", ["-NoProfile", "-NonInteractive", "-Command",
                        "$ErrorActionPreference='SilentlyContinue';"
                        "Get-CimInstance Win32_Process | "
                        f"Where-Object {{ $_.Name -match '{patterns}' }} | "
                        "Select-Object -ExpandProperty CommandLine"]),
        # 老系统兜底：wmic（Win11 24H2 起可能已移除，失败就跳过）
        ("wmic", ["process", "get", "commandline"]),
    )
    for executable, arguments in attempts:
        if not shutil.which(executable):
            continue
        try:
            result = subprocess.run(
                [executable, *arguments], capture_output=True, text=True,
                timeout=_SCAN_TIMEOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as exc:
            logger.debug("%s 进程扫描失败：%s", executable, exc)
            continue
        lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
        matched = [line for line in lines
                   if any(hint in line.lower() for hint in _BROWSER_PROCESS_HINTS)]
        if matched:
            return matched
    logger.debug("未获取到浏览器进程命令行（WMI/wmic 均不可用，改用进程名+锁文件兜底判断）")
    return []


def running_browser_names(browser: str = "") -> list[str]:
    """返回按进程名探测到的浏览器进程名（拿不到命令行时的兜底信号）。

    :param browser: 限定浏览器家族（``chrome`` / ``edge``），留空返回全部
    """
    names: list[str] = []
    if not sys.platform.startswith("win"):
        try:
            result = subprocess.run(["ps", "-eo", "comm"], capture_output=True,
                                    text=True, timeout=_SCAN_TIMEOUT)
            names = [line.strip() for line in (result.stdout or "").splitlines()]
        except Exception as exc:
            logger.debug("按进程名探测浏览器失败：%s", exc)
            return []
    else:
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "Get-Process | Select-Object -ExpandProperty ProcessName"],
                capture_output=True, text=True, timeout=_SCAN_TIMEOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            names = [line.strip() for line in (result.stdout or "").splitlines()]
        except Exception as exc:
            logger.debug("按进程名探测浏览器失败：%s", exc)
            return []

    wanted = _BROWSER_PROCESS_NAMES.get((browser or "").lower())
    matched = []
    for name in names:
        lowered = name.lower()
        if not lowered or any(bad in lowered for bad in _EXCLUDED_PROCESS_NAMES):
            continue
        if wanted:
            if not any(hint in lowered for hint in wanted):
                continue
        elif not any(hint in lowered for hint in _BROWSER_PROCESS_HINTS):
            continue
        matched.append(name)
    return sorted(set(matched))


def _same_path(left: str, right: str) -> bool:
    """路径等价比较（统一分隔符、大小写不敏感，兼容末尾斜杠差异）。"""
    if not left or not right:
        return False
    return os.path.normcase(os.path.normpath(left)) == os.path.normcase(os.path.normpath(right))


#: 解析命令行参数值：支持 ``--arg=value`` / ``--arg="value"`` / ``--arg='value'``
#: （值里含空格的情况很常见，例如 Windows 的 ``--profile-directory="Profile 3"``，
#: 用 ``str.split()`` 会把它拆成两段，因此必须按引号解析）
_ARG_PATTERN = r"""--{name}=(?:"([^"]*)"|'([^']*)'|(\S+))"""


def _arg_value(command_line: str, name: str) -> str | None:
    """从命令行中取出 ``--<name>=<value>`` 的值；未出现返回 None。"""
    match = re.search(_ARG_PATTERN.format(name=re.escape(name)),
                      str(command_line or ""), re.IGNORECASE)
    if not match:
        return None
    for group in match.groups():
        if group is not None:
            return group
    return None


def process_uses_profile(command_line: str, user_data_dir: str, profile_dir: str = "") -> bool:
    """判断某条进程命令行是否占用了目标 User Data / Profile。

    匹配规则（任一命中即占用）：

    1. 命令行含 ``--user-data-dir=<目标目录>``（路径等价）
    2. 进程用了目标目录，且 ``--profile-directory`` 就是目标 Profile
    3. 进程用了目标目录但**未指定 Profile**，而我们要检查的正是该 User Data
       根目录（此时它可能在用任意 Profile）

    注意"同一 User Data、不同 Profile"**不算**占用：Chrome 允许同目录下并发
    运行不同 Profile，误报会让使用者白白被拦住。
    """
    line = str(command_line or "")
    if not line:
        return False
    if not any(hint in line.lower() for hint in _BROWSER_PROCESS_HINTS):
        return False

    data_dir = _arg_value(line, "user-data-dir") or ""
    if not data_dir or not _same_path(data_dir, user_data_dir):
        return False

    if not profile_dir:
        return True                      # 检查的是 User Data 根目录：目录相同即占用
    profile_name = os.path.basename(profile_dir.rstrip("\\/"))
    used = _arg_value(line, "profile-directory")
    if used is None:
        return True                      # 未指定 Profile，可能在用我们关心的那个
    return used == profile_name


def profile_in_use(user_data_dir: str, profile_dir: str = "",
                   browser: str = "") -> tuple[bool, str]:
    """检测 User Data / Profile 是否被**运行中的浏览器**占用。

    判定分两级（越靠前越准）：

    1. **命令行匹配**（权威）：进程参数里出现 ``--user-data-dir=<目标目录>``
    2. **锁文件 + 同名进程**（兜底）：拿不到命令行时（受限环境），若目录里存在
       ``lockfile`` / ``SingletonLock`` **且**确有该浏览器家族的进程在跑，
       则判为"疑似占用"

    单独的锁文件**不算**占用：``lockfile`` 在浏览器正常退出后仍会留在磁盘上
    （Chromium 只在运行期间持有它），把它当成"正在运行"会产生大量误报。
    WebView2 宿主（``msedgewebview2``）等进程会被排除，它们不是浏览器主体。

    :param browser: 浏览器家族（``chrome`` / ``edge``），用于兜底判定的进程名过滤
    :return: ``(是否占用, 可读原因)``；原因文本可直接展示给使用者
    """
    if not user_data_dir or not os.path.isdir(user_data_dir):
        return False, ""

    for command_line in list_browser_processes():
        if process_uses_profile(command_line, user_data_dir, profile_dir):
            short = command_line if len(command_line) <= 200 else command_line[:200] + "…"
            return True, (
                f"检测到正在运行的浏览器进程占用了该 User Data 目录：\n"
                f"  User Data：{user_data_dir}\n"
                f"  Profile  ：{profile_dir or '（未指定）'}\n"
                f"  进程命令行：{short}\n\n"
                "**必须先完全退出该浏览器再运行本工具**（自动化的浏览器实例无法与"
                "已有实例共享同一个 User Data 目录）：\n"
                "  · Windows：关闭所有浏览器窗口后，在任务管理器结束残留的 "
                "chrome.exe / msedge.exe 进程（含后台驻留）\n"
                "  · macOS：退出 Chrome/Edge（⌘Q），必要时在「活动监视器」结束进程\n"
                "  · Linux：pkill -f 'chrome|chromium|msedge'\n"
                "另一个选择：改用 Cookie 导出/导入方案（cookie_only），"
                "它不需要独占 User Data 目录。")

    # 兜底：拿不到命令行时，用"锁文件 + 浏览器进程在跑"推断疑似占用
    locks = lock_files(user_data_dir, profile_dir)
    if locks:
        names = running_browser_names(browser)
        if names:
            listing = "、".join(os.path.basename(item) for item in locks)
            return True, (
                f"疑似 User Data 目录被占用：该目录存在锁文件（{listing}），"
                f"且系统里确有浏览器进程在运行（{'、'.join(names)}）。\n"
                f"  User Data：{user_data_dir}\n"
                "注意：当前环境无法读取进程命令行，因此只能给出**疑似**结论。\n"
                "请先**完全退出**该浏览器（含任务管理器里的后台驻留进程）再重试；\n"
                "特别注意：使用 WebView2 / 基于 Chromium 内嵌的桌面应用也可能"
                "占用同一目录，需要一并退出。\n"
                "如果你确认浏览器已全部退出（例如上次崩溃留下的陈旧锁文件），"
                "删除上述锁文件后重试即可——本模块不会替你删除任何文件。")

    if locks:
        logger.debug("User Data 下存在锁文件但无浏览器进程在跑（陈旧锁，按未占用处理）：%s",
                     "、".join(os.path.basename(item) for item in locks))
    return False, ""


def assert_profile_free(user_data_dir: str, profile_dir: str = "",
                        browser: str = "") -> None:
    """占用时抛 :class:`BrowserAlreadyRunning`（供适配器启动前调用）。"""
    busy, reason = profile_in_use(user_data_dir, profile_dir, browser)
    if busy:
        raise BrowserAlreadyRunning(reason)


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------
def _assert_paths(config: SessionConfig) -> None:
    """启动前校验 User Data / Profile 存在，缺失时抛带提示的异常。"""
    if not config.user_data_dir or not os.path.isdir(config.user_data_dir):
        raise UserDataDirNotFound(
            f"User Data 目录不存在：{config.user_data_dir or '（未探测到）'}\n"
            f"请确认 {config.browser} 已安装并至少启动过一次（该目录由浏览器首次"
            "运行创建），或在配置中显式指定 user_data_dir（也可用环境变量 "
            "CRAWLER_SESSION_USER_DATA_DIR）")
    if not os.path.isdir(config.profile_dir):
        from core.browser_session.config import list_profiles
        available = list_profiles(config.user_data_dir)
        hint = "、".join(available) if available else "（该 User Data 下没有可用 Profile）"
        raise ProfileNotFound(
            f"Profile 不存在：{config.profile_dir}\n可用 Profile：{hint}\n"
            "请把配置中的 profile 改为上列之一（如 'Default' 或 'Profile 1'）")


def launch(config: SessionConfig, *, progress=None) -> BrowserDriver:
    """按配置启动带登录态的浏览器实例，返回统一驱动对象。

    :param progress: 可选回调 ``progress(消息文本)``，用于把关键步骤写入界面日志
    :raises BrowserNotInstalled: 未找到浏览器内核
    :raises UserDataDirNotFound: User Data 目录不存在
    :raises ProfileNotFound: 指定 Profile 不存在
    :raises BrowserAlreadyRunning: User Data 被运行中的浏览器占用（**必须先关闭浏览器**）
    :raises LauncherNotAvailable: 未安装所选的驱动库
    :raises BrowserLaunchFailed: 其它启动失败
    """
    def note(message: str) -> None:
        logger.info(message)
        if progress:
            progress(message)

    config.finalize()
    _assert_paths(config)
    busy, reason = profile_in_use(config.user_data_dir, config.profile_dir,
                                  config.browser)
    if busy:
        raise BrowserAlreadyRunning(reason)

    executable = find_executable(config)
    if not executable:
        raise BrowserNotInstalled(
            f"未找到 {config.browser} 的可执行文件。可任选其一：\n"
            "  1) 设置环境变量 CRAWLER_CHROME_PATH 指向 chrome.exe / msedge.exe\n"
            "  2) 把 chrome-win64.zip 放到项目根目录（程序会自动解压）\n"
            "  3) 安装 Chrome 或 Edge 后重试\n"
            "  4) 在配置中设置 executable_path")
    if not config.executable_path:
        config.executable_path = executable
    note(f"浏览器内核：{executable}")
    note(f"复用本地登录态：{config.user_data_dir} → {config.profile}")
    if config.headless_mode != "off":
        note("警告：复用了 User Data 又开启无头模式，容易与图形实例争抢 Profile 锁")

    driver = create_driver(config, presence_check=profile_in_use, progress=progress)
    note(f"浏览器已启动（{driver.kind()}），当前地址：{driver.current_url() or '空白页'}")
    return driver


def open_login_page(config: SessionConfig, driver: BrowserDriver,
                    url: str = "", *, progress=None) -> str:
    """打开目标站点首页（供使用者手动登录一次后重新导出 Cookie）。

    :return: 实际打开的 URL
    """
    target = url or config.login_url
    if not target:
        domain = normalize_domain(config.domain)
        if not domain:
            raise ValueError("未指定目标站点：请配置 domain 或传入 url")
        target = f"https://{domain}/"
    if progress:
        progress(f"请在弹出的浏览器窗口中完成登录（{target}），"
                 "登录完成后回到本工具点击「导出 Cookie」")
    driver.get(target)
    logger.info("已打开登录页：%s", target)
    return target


def health_check(config: SessionConfig, driver: BrowserDriver | None = None) -> dict:
    """健康检查（生命周期钩子）：返回结构化状态，供界面/自检使用。"""
    report = detect_installation(config)
    report["driver_alive"] = bool(driver is not None and driver.alive())
    report["driver_kind"] = driver.kind() if driver is not None else ""
    report["executable_found"] = bool(report["executable"])
    report["stale_locks"] = [os.path.basename(item)
                             for item in lock_files(config.user_data_dir, config.profile_dir)]
    report["ok"] = bool(report["user_data_exists"] and report["executable_found"]
                         and not report["in_use"]
                         and (driver is None or report["driver_alive"]))
    return report


__all__ = [
    "LOCK_FILENAMES", "find_executable", "detect_installation", "lock_files",
    "list_browser_processes", "running_browser_names", "process_uses_profile",
    "profile_in_use", "assert_profile_free", "reset_process_cache",
    "launch", "open_login_page", "health_check",
]
