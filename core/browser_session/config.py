"""Config 子模块：浏览器登录态复用的配置结构与路径自动探测。

三层来源，优先级由低到高
------------------------
1. 框架默认值（:mod:`config` 的 ``DEFAULT_SESSION_*``）
2. 环境变量（``CRAWLER_SESSION_*``）——适合 CI / 多账号切换，不必改代码
3. 显式传参 / 框架配置系统的字典（界面控件、YAML/JSON 配置、:func:`load_browser_session`
   的 ``config`` 参数）

本模块只依赖标准库，可被任意层引用（与 :mod:`core.fetcher.chrome_setup` 的定位一致）。

路径自动探测
------------
:func:`default_user_data_dir` 覆盖 Windows / macOS / Linux 下 Chrome 与 Edge
的默认 User Data 根目录；:func:`default_profile_dir` 在根目录下挑选一个真实
存在的 Profile（优先 ``Default``），避免"猜错 Profile 导致登录态为空"。
"""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from urllib.parse import urlparse

import config as app_config
from core.browser_session.exceptions import (
    ProfileNotFound, UserDataDirNotFound,
)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
#: 支持的浏览器类型 → 可执行文件名（Windows 名在前，POSIX 名在后）
BROWSER_EXECUTABLES: dict[str, tuple[str, ...]] = {
    "chrome": ("chrome.exe", "chrome", "google-chrome", "chromium", "chromium-browser"),
    "edge": ("msedge.exe", "msedge", "microsoft-edge"),
    "chromium": ("chromium.exe", "chromium", "chrome", "chrome.exe"),
}

#: 驱动实现：selenium / playwright
LAUNCHERS = ("playwright", "selenium")

#: Cookie 接收策略
COOKIE_ACCEPT_ALL = "all"
COOKIE_ACCEPT_SAME_SITE = "same-site"
COOKIE_ACCEPT_MODES = (COOKIE_ACCEPT_ALL, COOKIE_ACCEPT_SAME_SITE)

#: 无头模式的取值（字符串统一入口，避免各处硬编码）
HEADLESS_OFF = "off"
HEADLESS_NEW = "new"
HEADLESS_MODES = (HEADLESS_OFF, HEADLESS_NEW)


def _expand(path: str) -> str:
    """展开 ``~`` 与 ``%VAR%`` / ``$VAR``，返回绝对路径（空串原样返回）。"""
    if not path:
        return ""
    expanded = os.path.expandvars(os.path.expanduser(str(path)))
    return os.path.abspath(expanded) if expanded else ""


def normalize_domain(value: str) -> str:
    """把 URL / host / 带端口的地址统一成小写 host（无端口、无 scheme）。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if "//" in text:
        text = urlparse(text).netloc or text
    else:
        text = text.split("/", 1)[0]
    text = text.split("@")[-1]        # 去掉 user:pass@
    text = text.split(":", 1)[0]      # 去掉端口
    return text.strip().strip(".").lower()


def target_domains(domain: str) -> list[str]:
    """返回 Cookie 注入需要覆盖的域名列表（自身 + 逐级父域）。

    例：``www.a.example.com`` → ``["www.a.example.com", "a.example.com", "example.com"]``。
    Chromium 的 ``add_cookie`` 要求 Cookie 的 domain 是当前页面 host 的**后缀匹配**，
    因此注入子域 Cookie 时必须先停在对应页面（见 :mod:`.cookie_store`）。

    顶级域（``com`` / ``cn`` 这类只有一段的名字）不会被列出——它既没有 Cookie
    归属意义，访问 ``https://com/`` 也没有任何意义。
    """
    host = normalize_domain(domain)
    if not host:
        return []
    parts = host.split(".")
    out = [".".join(parts[index:]) for index in range(len(parts) - 1)]
    if len(parts) == 1:          # 单段主机名（localhost 等）只保留自身
        out = [host]
    return [item for item in out if item and not item.startswith(".")]


def sanitize_filename(text: str) -> str:
    """把域名等文本清洗为可安全用作文件名的形式。"""
    cleaned = re.sub(r"[^\w.\-]+", "_", str(text or "")).strip("._")
    return cleaned[:120] or "session"


def default_user_data_dir(browser: str = "chrome") -> str:
    """按平台返回浏览器默认 User Data 根目录（不存在时返回空串）。

    - Windows：``%LOCALAPPDATA%\\Google\\Chrome\\User Data`` / ``...\\Microsoft\\Edge\\User Data``
    - macOS：``~/Library/Application Support/Google/Chrome`` / ``.../Microsoft Edge``
    - Linux：``~/.config/google-chrome`` / ``~/.config/microsoft-edge``
    """
    kind = (browser or "chrome").lower()
    home = os.path.expanduser("~")
    local = os.environ.get("LOCALAPPDATA", "")
    candidates: list[str] = []

    if kind == "edge":
        candidates += [
            os.path.join(local, "Microsoft", "Edge", "User Data") if local else "",
            os.path.join(home, "AppData", "Local", "Microsoft", "Edge", "User Data"),
            os.path.join(home, "Library", "Application Support", "Microsoft Edge"),
            os.path.join(home, ".config", "microsoft-edge"),
            os.path.join(home, "snap", "microsoft-edge", "current", ".config", "microsoft-edge"),
        ]
    elif kind == "chromium":
        candidates += [
            os.path.join(local, "Chromium", "User Data") if local else "",
            os.path.join(home, "Library", "Application Support", "Chromium"),
            os.path.join(home, ".config", "chromium"),
        ]
    else:  # chrome
        candidates += [
            os.path.join(local, "Google", "Chrome", "User Data") if local else "",
            os.path.join(home, "AppData", "Local", "Google", "Chrome", "User Data"),
            os.path.join(home, "Library", "Application Support", "Google", "Chrome"),
            os.path.join(home, ".config", "google-chrome"),
            # Chrome for Testing / Chromium 的通用目录放在"正式 Chrome"之后：
            # 它们通常几乎没有登录态，优先级应当更低
            os.path.join(local, "Google", "Chrome for Testing", "User Data") if local else "",
            os.path.join(home, "Library", "Application Support", "Google", "Chrome for Testing"),
            os.path.join(home, ".config", "chrome-for-testing"),
            os.path.join(home, "snap", "chromium", "current", ".config", "chromium"),
        ]

    for candidate in candidates:
        if candidate and os.path.isdir(candidate):
            return os.path.abspath(candidate)
    return ""


def detect_installed_browsers() -> dict[str, str]:
    """探测本机已安装浏览器的 User Data 目录，返回 ``{浏览器: 目录}``。

    只做**只读**探测，不创建任何目录，供界面下拉与文档示例使用。
    """
    found: dict[str, str] = {}
    for kind in ("chrome", "edge", "chromium"):
        path = default_user_data_dir(kind)
        if path:
            found[kind] = path
    return found


def list_profiles(user_data_dir: str, *, require_login: bool = False) -> list[str]:
    """列出 User Data 根目录下的 Profile 名称（``Default`` / ``Profile 1`` …）。

    :param require_login: 为 True 时只返回**看起来已登录**的 Profile
        （目录内存在非空 ``Cookies`` 文件）；用于"自动挑一个有登录态的 Profile"
    """
    if not os.path.isdir(user_data_dir):
        return []
    names: list[str] = []
    for entry in sorted(os.listdir(user_data_dir)):
        full = os.path.join(user_data_dir, entry)
        if not os.path.isdir(full):
            continue
        # Profile 的命名规律：Default 或 "Profile N"（含自定义名字如 "Profile 3"）
        if entry != "Default" and not entry.lower().startswith("profile"):
            continue
        if require_login and not has_login_data(full):
            continue
        names.append(entry)
    # Default 优先，便于"零配置直接用"
    names.sort(key=lambda name: (name != "Default", name))
    return names


def has_login_data(profile_dir: str) -> bool:
    """该 Profile 是否可能带有登录态（存在非空的 Cookies 库）。

    Chromium 把 Cookie 存在 ``<Profile>/Network/Cookies``（新版）或
    ``<Profile>/Cookies``（旧版），SQLite 文件大于 0 字节即认为"有数据"。
    """
    for relative in (os.path.join("Network", "Cookies"), "Cookies"):
        path = os.path.join(profile_dir, relative)
        try:
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                return True
        except OSError:
            continue
    return False


def _looks_like_profile(profile_dir: str) -> bool:
    """目录内容是否像"用过的 Profile"（浏览器首次运行会写入 Preferences 等）。

    只认**文件**（Preferences / Cookies / History / Login Data …）：像 ``Network``
    这样的空子目录在浏览器"刚启动一半"时就会出现，用它判断会产生误报。

    用于区分两种相似但原因完全不同的报错：
    「Profile 不存在」（路径/名字写错）与「Profile 是空的」（浏览器从未正常启动过）。
    """
    if not os.path.isdir(profile_dir):
        return False
    for name in ("Preferences", "Cookies", "History", "Bookmarks",
                 "Login Data", "Web Data", "Local State", "Secure Preferences"):
        try:
            if os.path.isfile(os.path.join(profile_dir, name)):
                return True
        except OSError:
            continue
    # 新版 Chromium 把 Cookie 放在 <Profile>/Network/Cookies
    try:
        cookies = os.path.join(profile_dir, "Network", "Cookies")
        if os.path.isfile(cookies):
            return True
    except OSError:
        pass
    return False


def default_profile_dir(user_data_dir: str, profile: str = "") -> str:
    """解析 Profile 的**绝对路径**（不要求存在，由调用方决定是否校验）。

    :param profile: Profile 名称；为空时自动挑一个（优先 ``Default``）
    """
    root = _expand(user_data_dir)
    if not root:
        return ""
    if profile:
        return os.path.join(root, profile)
    if os.path.isdir(os.path.join(root, "Default")):
        return os.path.join(root, "Default")
    profiles = list_profiles(root)
    if profiles:
        return os.path.join(root, profiles[0])
    return os.path.join(root, "Default")


# ---------------------------------------------------------------------------
# 配置对象
# ---------------------------------------------------------------------------
@dataclass
class SessionConfig:
    """浏览器登录态复用的完整配置（字段即所有子模块共享的"唯一事实来源"）。

    只读约定：子模块不修改传入的配置对象，需要派生时用 :meth:`with_overrides`。
    """

    # ---- 浏览器与 Profile ----
    browser: str = app_config.DEFAULT_SESSION_BROWSER
    user_data_dir: str = ""                # 留空 = default_user_data_dir(browser)
    profile: str = app_config.DEFAULT_SESSION_PROFILE
    launcher: str = app_config.DEFAULT_SESSION_LAUNCHER
    executable_path: str = ""              # 留空 = 复用 core.fetcher.chrome_setup 的查找链

    # ---- 目标站点 ----
    domain: str = app_config.DEFAULT_SESSION_DOMAIN

    # ---- 持久化 ----
    cookie_store_path: str = app_config.DEFAULT_SESSION_COOKIE_STORE_PATH
    cookie_key: str = ""                   # 加密口令；留空则读环境变量
    cookie_encrypt: bool = app_config.DEFAULT_SESSION_COOKIE_ENCRYPT
    cookie_accept: str = app_config.DEFAULT_SESSION_COOKIE_ACCEPT

    # ---- 网络 ----
    proxy: dict = field(default_factory=dict)
    timeout: float = 30.0

    # ---- 窗口与指纹 ----
    headless: bool = app_config.DEFAULT_SESSION_HEADLESS
    impersonate: bool = app_config.DEFAULT_SESSION_IMPERSONATE
    user_agent: str = ""
    locale: str = "zh-CN"
    viewport: tuple = (1440, 900)
    timezone: str = ""                     # 与代理 IP 地理位置保持一致时填写
    geolocation: tuple | None = None       # (纬度, 经度)

    # ---- 登录态检查 ----
    check_login: bool = app_config.DEFAULT_SESSION_CHECK_LOGIN
    login_url: str = app_config.DEFAULT_SESSION_LOGIN_URL
    logout_markers: list = field(default_factory=list)
    login_markers: list = field(default_factory=list)
    login_status: list = field(default_factory=list)
    login_selector: str = app_config.DEFAULT_SESSION_LOGIN_SELECTOR
    logout_selector: str = app_config.DEFAULT_SESSION_LOGOUT_SELECTOR
    check_timeout: float = app_config.DEFAULT_SESSION_CHECK_TIMEOUT
    cookie_assume_valid: bool = app_config.DEFAULT_SESSION_COOKIE_ASSUME_VALID

    # ---- 反检测 / 行为拟人化 ----
    anti_detect: bool = app_config.DEFAULT_SESSION_ANTI_DETECT
    delay_mean: float = app_config.DEFAULT_SESSION_DELAY_MEAN
    delay_jitter: float = app_config.DEFAULT_SESSION_DELAY_JITTER
    action_interval: float = app_config.DEFAULT_SESSION_MIN_ACTION_INTERVAL
    rate_limit: float = app_config.DEFAULT_SESSION_RATE_LIMIT
    mouse_steps: int = app_config.DEFAULT_SESSION_MOUSE_STEPS

    # ------------------------------------------------------------------
    # 构造
    # ------------------------------------------------------------------
    @classmethod
    def resolve(cls, source: "SessionConfig | dict | None" = None,
                **overrides) -> "SessionConfig":
        """按「默认值 → 环境变量 → 显式传参」合成配置（推荐入口）。

        :param source: 已有配置对象或框架配置字典（``None`` 表示只用默认值）
        :param overrides: 直接覆盖的字段（在 :meth:`apply_env` 之后生效）
        """
        base: dict = {}
        if isinstance(source, SessionConfig):
            base = {key: value for key, value in asdict(source).items()}
            base["geolocation"] = source.geolocation
        elif isinstance(source, dict):
            base = cls.from_mapping(source).to_dict()
        resolved = cls(**cls._coerce(base)) if base else cls()
        resolved.apply_env()
        if overrides:
            resolved.update(overrides)
        resolved.finalize()
        return resolved

    @classmethod
    def from_mapping(cls, mapping: dict | None) -> "SessionConfig":
        """从字典构造（键名与字段同名；未知键忽略，便于直接吃 YAML/JSON）。

        兼容 ``webdriver`` / ``user_data`` 等别名，方便直接粘贴常见配置片段。
        """
        aliases = {"user_data": "user_data_dir", "user_data_path": "user_data_dir",
                   "profile_directory": "profile", "profile_name": "profile",
                   "cookies_path": "cookie_store_path", "cookie_path": "cookie_store_path",
                   "cookie_key_env": "cookie_key", "target_domain": "domain",
                   "domain_name": "domain"}
        data: dict = {}
        for key, value in dict(mapping or {}).items():
            name = aliases.get(str(key).strip().lower(), str(key).strip().lower())
            if name in cls.__dataclass_fields__:
                data[name] = value
        return cls(**cls._coerce(data))

    @staticmethod
    def _coerce(data: dict) -> dict:
        """把字符串形式的配置值转换为字段期望的类型（YAML / 环境变量场景）。"""
        bool_fields = {"cookie_encrypt", "headless", "impersonate", "check_login",
                       "anti_detect", "cookie_assume_valid"}
        float_fields = {"timeout", "check_timeout", "delay_mean", "delay_jitter",
                        "action_interval", "rate_limit"}
        int_fields = {"mouse_steps"}
        marker_fields = {"login_markers", "logout_markers"}

        def as_bool(value):
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in ("1", "true", "yes", "on", "y")

        out: dict = {}
        for key, value in data.items():
            if value is None:
                continue
            if key in bool_fields:
                out[key] = as_bool(value)
            elif key in float_fields:
                try:
                    out[key] = float(value)
                except (TypeError, ValueError):
                    continue
            elif key in int_fields:
                try:
                    out[key] = int(value)
                except (TypeError, ValueError):
                    continue
            elif key in marker_fields:
                if isinstance(value, str):
                    out[key] = [item.strip() for item in value.split(",") if item.strip()]
                elif isinstance(value, (list, tuple)):
                    out[key] = [str(item) for item in value]
            elif key == "login_status":
                # 状态码：字符串 / 列表都接受，统一转 int 列表（非法项丢弃）
                items = ([item.strip() for item in value.split(",")] if isinstance(value, str)
                         else list(value or []))
                codes = []
                for item in items:
                    try:
                        codes.append(int(item))
                    except (TypeError, ValueError):
                        continue
                if codes:
                    out[key] = codes
            elif key == "viewport":
                if isinstance(value, str):
                    parts = re.split(r"[x×,\s]+", value.strip())
                else:
                    parts = list(value or ())
                try:
                    if len(parts) == 2:
                        out[key] = (int(parts[0]), int(parts[1]))
                except (TypeError, ValueError):
                    continue
            elif key == "geolocation":
                if value in ("", None):
                    continue
                try:
                    out[key] = (float(value[0]), float(value[1]))
                except (TypeError, ValueError, IndexError):
                    continue
            elif key == "proxy" and isinstance(value, str):
                out[key] = {"https": value, "http": value} if value else {}
            else:
                out[key] = value
        return out

    def update(self, values: dict) -> "SessionConfig":
        """就地覆盖若干字段（只接受已知字段名，返回自身便于链式调用）。"""
        for key, value in self._coerce(dict(values or {})).items():
            if key in self.__dataclass_fields__:
                setattr(self, key, value)
        return self

    def with_overrides(self, **overrides) -> "SessionConfig":
        """派生一份新配置（不改动原对象）。"""
        clone = SessionConfig.from_mapping(self.to_dict())
        clone.geolocation = self.geolocation
        clone.update(overrides)
        clone.finalize()
        return clone

    # ------------------------------------------------------------------
    # 环境变量与规范化
    # ------------------------------------------------------------------
    def apply_env(self, environ: dict | None = None) -> "SessionConfig":
        """用 ``CRAWLER_SESSION_*`` 环境变量覆盖当前配置（密钥类变量优先）。

        支持的变量（前缀见 :data:`config.BROWSER_SESSION_ENV_PREFIX`）：

        ``BROWSER`` / ``USER_DATA_DIR`` / ``PROFILE`` / ``LAUNCHER`` /
        ``EXECUTABLE_PATH`` / ``DOMAIN`` / ``COOKIE_STORE`` / ``COOKIE_KEY`` /
        ``COOKIE_ENCRYPT`` / ``COOKIE_ACCEPT`` / ``HEADLESS`` / ``PROXY`` /
        ``LOGIN_URL`` / ``LOGIN_MARKERS`` / ``CHECK_LOGIN`` / ``CHECK_TIMEOUT``
        """
        env = os.environ if environ is None else environ
        prefix = app_config.BROWSER_SESSION_ENV_PREFIX
        mapping = {
            "browser": "BROWSER",
            "user_data_dir": "USER_DATA_DIR",
            "profile": "PROFILE",
            "launcher": "LAUNCHER",
            "executable_path": "EXECUTABLE_PATH",
            "domain": "DOMAIN",
            "cookie_store_path": "COOKIE_STORE",
            "cookie_key": "COOKIE_KEY",
            "cookie_encrypt": "COOKIE_ENCRYPT",
            "cookie_accept": "COOKIE_ACCEPT",
            "headless": "HEADLESS",
            "proxy": "PROXY",
            "login_url": "LOGIN_URL",
            "login_markers": "LOGIN_MARKERS",
            "logout_markers": "LOGOUT_MARKERS",
            "login_selector": "LOGIN_SELECTOR",
            "logout_selector": "LOGOUT_SELECTOR",
            "check_login": "CHECK_LOGIN",
            "check_timeout": "CHECK_TIMEOUT",
            "timezone": "TIMEZONE",
            "user_agent": "USER_AGENT",
        }
        values: dict = {}
        for field_name, suffix in mapping.items():
            raw = env.get(prefix + suffix, "")
            if raw not in ("", None):
                values[field_name] = raw
        if values:
            self.update(values)
        # 加密密钥：显式字段为空时读专用环境变量（避免把密钥写进配置文件）
        if not self.cookie_key:
            self.cookie_key = str(env.get(app_config.DEFAULT_SESSION_COOKIE_KEY_ENV, "") or "")
        # 浏览器可执行文件复用框架既有环境变量，保持"一处指定，处处生效"
        if not self.executable_path:
            self.executable_path = str(env.get(app_config.BROWSER_EXECUTABLE_ENV, "") or "")
        return self

    def finalize(self) -> "SessionConfig":
        """补全派生字段并做轻量规范化（不校验路径是否存在）。"""
        self.browser = (self.browser or app_config.DEFAULT_SESSION_BROWSER).strip().lower()
        if self.browser == "msedge":
            self.browser = "edge"
        self.launcher = (self.launcher or app_config.DEFAULT_SESSION_LAUNCHER).strip().lower()
        self.cookie_accept = (self.cookie_accept or COOKIE_ACCEPT_ALL).strip().lower()
        self.domain = normalize_domain(self.domain)

        if not self.user_data_dir:
            self.user_data_dir = default_user_data_dir(self.browser)
        else:
            self.user_data_dir = _expand(self.user_data_dir)
        if not self.executable_path:
            self.executable_path = ""
        else:
            self.executable_path = _expand(self.executable_path)

        if not self.profile:
            self.profile = app_config.DEFAULT_SESSION_PROFILE
        if not self.cookie_store_path:
            name = self.domain or f"{self.browser}_session"
            self.cookie_store_path = os.path.join(app_config.ensure_sessions_dir(),
                                                  f"{sanitize_filename(name)}.json")
        else:
            self.cookie_store_path = _expand(self.cookie_store_path)

        if not self.login_url and self.domain:
            scheme = "https"
            self.login_url = f"{scheme}://{self.domain}/"
        if isinstance(self.login_status, str):
            self.login_status = self._coerce({"login_status": self.login_status})["login_status"]
        try:
            self.login_status = [int(item) for item in (self.login_status or [])] or [200]
        except (TypeError, ValueError):
            self.login_status = [200]
        self.viewport = tuple(int(v) for v in (self.viewport or (1440, 900)))
        self.timeout = max(1.0, float(self.timeout or 30.0))
        self.check_timeout = max(1.0, float(self.check_timeout or 10.0))
        return self

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    @property
    def profile_dir(self) -> str:
        """当前 Profile 的绝对路径。"""
        return default_profile_dir(self.user_data_dir, self.profile)

    @property
    def domains(self) -> list[str]:
        """Cookie 注入需要覆盖的域名列表（自身 + 父域）。"""
        return target_domains(self.domain)

    @property
    def headless_mode(self) -> str:
        """归一化后的无头取值：``off`` / ``new``。"""
        value = self.headless
        if isinstance(value, bool):
            return HEADLESS_NEW if value else HEADLESS_OFF
        text = str(value).strip().lower()
        if text in ("1", "true", "yes", "on", "new", "headless"):
            return HEADLESS_NEW
        return HEADLESS_OFF

    @property
    def cookie_key_source(self) -> str:
        """加密口令来源描述（用于日志，绝不输出密钥本身）。"""
        if not self.cookie_key:
            return "未配置"
        return f"已配置（{len(self.cookie_key)} 字符，来源：配置或 {app_config.DEFAULT_SESSION_COOKIE_KEY_ENV}）"

    def proxy_server(self) -> str:
        """代理服务器地址（``server``/``https``/``http``/``all`` 依次取）。"""
        for key in ("server", "https", "http", "all"):
            value = (self.proxy or {}).get(key)
            if value:
                return str(value)
        return ""

    # ------------------------------------------------------------------
    # 落盘与展示
    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        """转为普通字典（排除明文密钥；``geolocation`` 保持元组）。"""
        data = asdict(self)
        data.pop("cookie_key", None)
        return data

    def describe(self) -> str:
        """多行可读摘要（用于日志打点与界面提示，不含任何密钥）。"""
        lines = [
            f"浏览器：{self.browser} | 驱动：{self.launcher} | 无头：{self.headless_mode}",
            f"User Data：{self.user_data_dir or '（未探测到，请显式指定）'}",
            f"Profile：{self.profile} → {self.profile_dir}",
            f"目标域名：{self.domain or '（未设置）'} | Cookie 路径：{self.cookie_store_path}",
            f"Cookie 加密：{'开' if self.cookie_encrypt else '关'}（密钥{self.cookie_key_source}）"
            f" | 接收策略：{self.cookie_accept}",
        ]
        if self.proxy_server():
            lines.append(f"代理：{self.proxy_server()}（住宅代理优先）")
        if self.timezone or self.geolocation:
            lines.append(f"时区：{self.timezone or '（未设置）'} | 地理位置：{self.geolocation or '（未设置）'}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------
    def validate(self, *, require_user_data: bool = True,
                 require_domain: bool = True) -> None:
        """校验配置可用性，不满足时抛出**带明确提示**的异常。

        :raises UserDataDirNotFound: User Data 目录不存在
        :raises ProfileNotFound: 指定 Profile 不存在（消息会列出可用 Profile）
        :raises ValueError: 浏览器类型 / 驱动 / 策略取值非法
        """
        if self.browser not in BROWSER_EXECUTABLES:
            raise ValueError(
                f"不支持的浏览器类型：{self.browser!r}，可选：{', '.join(BROWSER_EXECUTABLES)}")
        if self.launcher not in LAUNCHERS:
            raise ValueError(f"不支持的驱动实现：{self.launcher!r}，可选：{', '.join(LAUNCHERS)}")
        if self.cookie_accept not in COOKIE_ACCEPT_MODES:
            raise ValueError(f"不支持的 Cookie 接收策略：{self.cookie_accept!r}，"
                             f"可选：{', '.join(COOKIE_ACCEPT_MODES)}")
        if require_domain and not self.domain:
            raise ValueError("未设置目标域名（config.domain / 环境变量 "
                             f"{app_config.BROWSER_SESSION_ENV_PREFIX}DOMAIN）——"
                             "Cookie 分组与登录态探测都依赖它")
        if require_user_data:
            self.validate_paths()

    def validate_paths(self) -> None:
        """校验 User Data 与 Profile 路径存在。

        :raises UserDataDirNotFound: 目录不存在（含"该浏览器从未启动过"的提示）
        :raises ProfileNotFound: Profile 不存在（消息列出可用项）
        """
        root = self.user_data_dir
        if not root or not os.path.isdir(root):
            raise UserDataDirNotFound(
                f"User Data 目录不存在：{root or '（未探测到）'}\n"
                f"请检查：1) {self.browser} 是否安装并至少启动过一次"
                "（目录由浏览器首次运行创建）；"
                "2) 路径是否写错；3) 或在配置中显式指定 user_data_dir / "
                f"环境变量 {app_config.BROWSER_SESSION_ENV_PREFIX}USER_DATA_DIR")
        profile_dir = self.profile_dir
        available = list_profiles(root)
        # 两种相似但原因完全不同的情况要分开报：
        #   · 目录不存在    → Profile 名字 / 路径写错
        #   · 目录在但为空  → 浏览器从未正常启动过（Chrome for Testing 等常见）
        if not os.path.isdir(profile_dir) or not _looks_like_profile(profile_dir):
            if os.path.isdir(profile_dir):
                # 目录存在但内容为空：常见于"装好浏览器却从未正常启动过"
                # （Chrome for Testing / 一次都没打开过的 Chrome 都是这样）。
                # 注意 list_profiles() 只列出"有内容"的 Profile，因此这里不能
                # 用它来判断，否则会给出误导性的"Profile 不存在"。
                others = [item for item in available if item != self.profile]
                tail = (f"\n同一 User Data 下其它 Profile：{'、'.join(others)}"
                        if others else "")
                raise ProfileNotFound(
                    f"Profile「{self.profile}」的目录存在但内容为空：{profile_dir}\n"
                    f"这说明「{self.browser}」安装后**还没有被正常启动过**"
                    "（Profile 内容由浏览器首次运行写入）。\n"
                    "解决方法（任选其一）：\n"
                    f"  1) 先手动启动一次「{self.browser}」，登录目标站点后关闭，再运行本工具；\n"
                    "  2) 换一个确实用过的浏览器或 Profile（--browser / --profile 参数）；\n"
                    "  3) 若浏览器装在非默认位置，用 user_data_dir 显式指定其 User Data 目录。"
                    + tail)
            hint = ("可用 Profile：" + "、".join(available)) if available else \
                ("该 User Data 目录下没有任何 Profile——"
                 "Profile（如 Default）要等浏览器成功运行一次后才会创建")
            raise ProfileNotFound(
                f"Profile 不存在：{profile_dir}\n{hint}\n"
                f"请在配置中把 profile 改为上列之一（如 'Default' 或 'Profile 1'）；"
                f"若这是 {self.browser} 第一次使用，请先手动启动它一次再运行本工具。")


__all__ = [
    "BROWSER_EXECUTABLES", "LAUNCHERS", "COOKIE_ACCEPT_ALL",
    "COOKIE_ACCEPT_SAME_SITE", "COOKIE_ACCEPT_MODES",
    "HEADLESS_OFF", "HEADLESS_NEW", "HEADLESS_MODES",
    "SessionConfig", "normalize_domain", "target_domains", "sanitize_filename",
    "default_user_data_dir", "default_profile_dir", "detect_installed_browsers",
    "list_profiles", "has_login_data",
]
