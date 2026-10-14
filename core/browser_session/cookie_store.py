"""CookieStore 子模块：Cookie 的导出、持久化、加载与注入。

数据流
------
::

    已登录浏览器 ──export_from_driver()──▶ 规范化 Cookie 列表
                                              │
                                        save() ─┴─ 加密/明文落盘（JSON）
                                              │
    新会话浏览器 ◀──inject_into_driver()── load()
                                              │
                                  as_requests_cookies() ──▶ requests / httpx

规范化（本模块最容易被忽视、却最容易出错的地方）
------------------------------------------------
不同来源的 Cookie 字段天差地别，这里统一转成**一份**规范格式：

- ``expiry``：Selenium 给 float/int 秒、某些导出文件给毫秒、浏览器
  ``Cookies`` 库给 Chromium 微秒时间戳、手写 JSON 还可能给 ISO 字符串
  → 一律换算为「Unix 秒（int）」，超出合理范围直接丢弃该字段而不是塞一个
  非法值（否则 ``add_cookie`` 会整体失败）
- ``sameSite``：Selenium 要 ``Strict``/``Lax``/``None``，Playwright 要
  ``Strict``/``Lax``/``None``（小写也接受），未知值一律置 None 而非原样透传
- ``domain``：缺失时由当前页面 host 推导；``.example.com`` 与 ``example.com``
  视为同一域名族（见 :func:`domain_of`）
- 非法（无 name / 无 domain / 值为 None / name 前有 ``__Host-`` 却带 domain）
  的 Cookie 会被丢弃并记录原因，**不会**让整批注入失败

加密存储（``cryptography``，可选依赖）
--------------------------------------
``cookie_encrypt=True`` 且能拿到密钥时，文件写成"文本头 + 一段 base64url 密文"
（Fernet 认证加密），**不暴露**任何 Cookie 名。缺少 ``cryptography`` 时自动降级
为明文并在日志与返回值中明确标注「未加密」，绝不静默。
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

import config as app_config
from core.browser_session.config import (
    COOKIE_ACCEPT_ALL, COOKIE_ACCEPT_SAME_SITE, SessionConfig, normalize_domain,
)
from core.browser_session.exceptions import (
    CookieDecryptError, CookieExportError, CookieFileInvalid, CookieFileNotFound,
    CookieInjectError,
)
from utils.logging_setup import get_logger

logger = get_logger("core.browser_session.cookie_store")

#: 存储格式版本（升级时便于向后兼容读取）
STORE_VERSION = 1
#: 加密文件头（明文 JSON 头 + 空行 + 密文，便于人工辨认"这是加密的"）
ENCRYPTED_HEADER = "#!"
_UNENCRYPTED_HEADER = "{"
#: 加密文件头里标注的算法名
FERNET = "fernet"

#: Selenium ``add_cookie`` 接受的字段白名单（多传字段会直接抛异常）
SELENIUM_FIELDS = ("name", "value", "path", "domain", "secure", "httpOnly",
                   "expiry", "sameSite")
#: Playwright ``add_cookies`` 接受的字段白名单
PLAYWRIGHT_FIELDS = ("name", "value", "url", "domain", "path", "expires",
                     "httpOnly", "secure", "sameSite")

#: Chromium 的 ``__Host-`` 前缀 Cookie 不允许带 domain 字段（必须 host-only）
_HOST_PREFIX = "__Host-"
#: Unix 秒的合理区间（2001-09-09 ~ 2096-10-02），超出即认定为"单位搞错了"
_EXPIRY_MIN, _EXPIRY_MAX = 1_000_000_000, 4_000_000_000

#: 常见时间格式（导出的 Cookie 文件里出现过的写法）
_DATE_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%a, %d %b %Y %H:%M:%S %Z",
    "%a, %d-%b-%Y %H:%M:%S %Z",
)


# ---------------------------------------------------------------------------
# 时间与字段规范化
# ---------------------------------------------------------------------------
def _now() -> float:
    return time.time()


def normalize_expiry(value) -> int | None:
    """把各种写法的过期时间统一换算为 Unix 秒（int）；无法识别或超出合理区间返回 None。

    支持：Unix 秒 / Unix 毫秒 / Chromium 微秒（WebKit 时间，1601 纪元）/ ISO8601 /
    常见日期字符串 / ``datetime`` 对象。
    """
    if value in (None, "", 0, "0"):
        return None
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        # 会话性 Cookie（10000-01-01 之类的哨兵值）不写 expiry
        if moment.year >= 9999:
            return None
        return int(moment.timestamp())
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number <= 0:
            return None
        # Chromium 的 Cookies 库用"1601 纪元微秒"，换算到 Unix 秒约需减 11644473600
        if number > 1e16:
            number = number / 1_000_000 - 11_644_473_600
        elif number > 1e13:      # 毫秒
            number = number / 1000.0
        elif number > _EXPIRY_MAX:   # 已经是秒但显然超范围，仍按毫秒再试
            number = number / 1000.0
        seconds = int(number)
        return seconds if _EXPIRY_MIN <= seconds <= _EXPIRY_MAX else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if re.fullmatch(r"\d+(\.\d+)?", text):
            return normalize_expiry(float(text))
        candidate = text.replace("Z", "+0000") if text.endswith("Z") else text
        for fmt in _DATE_FORMATS:
            for probe in (candidate, text):
                try:
                    moment = datetime.strptime(probe, fmt)
                except ValueError:
                    continue
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=timezone.utc)
                seconds = int(moment.timestamp())
                return seconds if _EXPIRY_MIN <= seconds <= _EXPIRY_MAX else None
        try:
            moment = datetime.fromisoformat(text)
            return normalize_expiry(moment)
        except ValueError:
            logger.debug("无法识别的 Cookie 过期时间：%r", value)
            return None
    return None


def normalize_same_site(value) -> str | None:
    """归一化 SameSite：只保留 ``Strict`` / ``Lax`` / ``None``，其余返回 None。"""
    text = str(value or "").strip().lower()
    if text in ("strict", "lax", "none"):
        return text.capitalize() if text != "none" else "None"
    if text in ("unspecified", "no_restriction", "unspecifiedsamesite"):
        return "None"
    return None


def normalize_cookie(raw: dict, page_url: str = "", default_host: str = "") -> dict | None:
    """把任意来源的 Cookie 归一为规范格式；非法条目返回 ``None``。

    :param page_url: 当前页面地址（用于补齐缺失的 ``domain``）
    :param default_host: 兜底域名（页面地址也不可用时使用，通常是目标站点）
    """
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    value = raw.get("value")
    if not isinstance(name, str) or not name.strip():
        return None
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)

    domain = normalize_domain(raw.get("domain") or "")
    if not domain:
        domain = normalize_domain(page_url) or normalize_domain(default_host)
    if not domain:
        return None

    path = str(raw.get("path") or "/").strip() or "/"
    if not path.startswith("/"):
        path = "/" + path

    expiry = normalize_expiry(raw.get("expiry", raw.get("expires",
                                                         raw.get("expirationDate"))))
    if name.startswith(_HOST_PREFIX):
        domain = ""      # __Host- 前缀 Cookie 必须 host-only，带上 domain 会被拒
    cookie = {
        "name": name.strip(),
        "value": value,
        "domain": domain,
        "path": path,
        "secure": bool(raw.get("secure", raw.get("isSecure", False))),
        "httpOnly": bool(raw.get("httpOnly", raw.get("isHttpOnly", False))),
    }
    if expiry is not None:
        cookie["expiry"] = expiry
    same_site = normalize_same_site(raw.get("sameSite", raw.get("same_site")))
    if same_site:
        cookie["sameSite"] = same_site
    elif raw.get("sameSite") not in (None, ""):
        logger.debug("Cookie %s 的 sameSite 值无法识别，已忽略：%r",
                     cookie["name"], raw.get("sameSite"))
    return cookie


def normalize_cookies(items: Iterable[dict], page_url: str = "",
                     default_host: str = "") -> tuple[list[dict], list[str]]:
    """批量归一化，返回 ``(规范 Cookie 列表, 被丢弃条目的原因)``。

    同名同域同路径的重复条目**后者覆盖前者**——浏览器的真实语义就是"后写入的
    生效"（如服务端轮换 session 后，旧值不应盖住新值）。
    """
    kept: list[dict] = []
    dropped: list[str] = []
    index: dict[tuple, int] = {}
    for raw in items or []:
        cookie = normalize_cookie(raw, page_url, default_host)
        if cookie is None:
            label = (raw or {}).get("name") if isinstance(raw, dict) else repr(raw)[:40]
            dropped.append(f"字段不完整或非法，已丢弃：{label}")
            continue
        key = (cookie["name"], cookie["domain"], cookie["path"])
        if key in index:
            kept[index[key]] = cookie
            continue
        index[key] = len(kept)
        kept.append(cookie)
    return kept, dropped


def domain_of(cookie: dict) -> str:
    """Cookie 的归属域名（去掉前导点，统一小写）。"""
    return normalize_domain(cookie.get("domain") or "")


def domain_family(host: str) -> set[str]:
    """返回 host 及其父域集合（用于按目标域名筛选 Cookie）。

    只保留含至少两段的名字（``example.com``），不列顶级域 ``com``——
    与 :func:`core.browser_session.config.target_domains` 的口径保持一致。
    """
    value = normalize_domain(host)
    if not value:
        return set()
    parts = value.split(".")
    if len(parts) == 1:          # 单段主机名（localhost 等）
        return {value}
    return {".".join(parts[index:]) for index in range(len(parts) - 1)}


def filter_by_domain(cookies: Iterable[dict], host: str,
                     mode: str = COOKIE_ACCEPT_ALL) -> list[dict]:
    """按接收策略筛选 Cookie。

    - ``all``：全部保留（同站登录常见：SSO 域、CDN 域都要带上）
    - ``same-site``：只保留归属域名属于目标 host 域名族的条目（更克制，
      不把无关第三方跟踪 Cookie 带进自动化会话）
    """
    items = list(cookies or [])
    if (mode or COOKIE_ACCEPT_ALL) != COOKIE_ACCEPT_SAME_SITE:
        return items
    family = domain_family(host)
    if not family:
        return items
    kept = []
    for cookie in items:
        owner = domain_of(cookie)
        # 归属域与目标域互为父/子域，或目标域是归属域的子域（.example.com 覆盖 www.example.com）
        if owner in family or any(owner.endswith("." + item) for item in family):
            kept.append(cookie)
    return kept


def group_by_domain(cookies: Iterable[dict]) -> dict[str, list[dict]]:
    """按归属域名分组（注入时需要"先导航到该域名"再逐条 add_cookie）。"""
    grouped: dict[str, list[dict]] = {}
    for cookie in cookies or []:
        grouped.setdefault(domain_of(cookie) or "", []).append(cookie)
    return grouped


def cookies_to_dict(cookies: Iterable[dict]) -> dict[str, str]:
    """拍平成 ``{name: value}``（``requests`` 的 ``cookies=`` 参数格式）。"""
    return {cookie["name"]: cookie["value"] for cookie in cookies or []}


def cookie_fingerprint(cookies: Iterable[dict], length: int = 16) -> str:
    """计算 Cookie 集合指纹（**不泄露值**：只哈希 name/domain/path + 值的摘要）。

    用途：判断"当前登录态与上次导出时是否同一份"，便于在 Cookie 被服务端轮换后
    提示重新导出，而不是把明文 Cookie 写进日志或清单。
    """
    digest = hashlib.sha256()
    for cookie in sorted(cookies or [],
                         key=lambda item: (item.get("domain", ""), item.get("name", ""))):
        value = cookie.get("value") or ""
        digest.update(f"{cookie.get('name')}|{cookie.get('domain')}|"
                      f"{cookie.get('path')}|{len(value)}|"
                      f"{hashlib.sha256(value.encode('utf-8', 'replace')).hexdigest()[:12]}"
                      .encode("utf-8"))
    return digest.hexdigest()[:max(8, int(length))]


def to_selenium_cookie(cookie: dict, page_url: str = "") -> dict:
    """规范格式 → Selenium ``add_cookie`` 可接受的结构（白名单字段）。"""
    out = {key: cookie[key] for key in ("name", "value", "path", "secure",
                                        "httpOnly") if key in cookie}
    if cookie.get("domain"):
        out["domain"] = cookie["domain"]
    if cookie.get("expiry"):
        out["expiry"] = int(cookie["expiry"])
    if cookie.get("sameSite"):
        out["sameSite"] = cookie["sameSite"]
    return out


def to_playwright_cookie(cookie: dict, fallback_url: str = "") -> dict | None:
    """规范格式 → Playwright ``add_cookies`` 结构（``expires``，且 domain/url 二选一）。"""
    out = {key: cookie[key] for key in ("name", "value", "path", "httpOnly", "secure")
           if key in cookie}
    if cookie.get("expiry"):
        out["expires"] = int(cookie["expiry"])
    if cookie.get("sameSite"):
        out["sameSite"] = cookie["sameSite"]
    domain = cookie.get("domain") or ""
    if domain:
        out["domain"] = domain
    else:
        # host-only Cookie（如 __Host- 前缀）必须改用 url 形式注入
        url = fallback_url
        if not url:
            return None
        out["url"] = url
    return out


# ---------------------------------------------------------------------------
# 加密
# ---------------------------------------------------------------------------
def crypto_available() -> bool:
    """是否具备加密存储能力（需要可选依赖 ``cryptography``）。"""
    try:
        return importlib.util.find_spec("cryptography") is not None
    except (ImportError, ValueError):
        return False


def _derive_key(secret: str) -> bytes:
    """把任意口令派生成 Fernet 需要的 32 字节 urlsafe-base64 密钥。

    这里会用 Fernet 实际构造一次密钥，确保派生结果一定是合法密钥
    （长度/编码不对时在保存阶段就报错，而不是等到解密时才发现）。
    """
    from cryptography.fernet import Fernet
    digest = hashlib.sha256(str(secret).encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(digest)
    Fernet(key)                      # 自校验：密钥非法会在这里抛异常
    return key


@dataclass
class SessionFile:
    """Cookie 存储文件的内容结构（清单 + Cookie 本体）。"""

    version: int = STORE_VERSION
    domain: str = ""
    browser: str = ""
    profile: str = ""
    exported_at: str = ""
    encrypted: bool = False
    fingerprint: str = ""
    cookie_count: int = 0
    domain_counts: dict = field(default_factory=dict)
    cookies: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "domain": self.domain,
            "browser": self.browser,
            "profile": self.profile,
            "exported_at": self.exported_at,
            "encrypted": self.encrypted,
            "fingerprint": self.fingerprint,
            "cookie_count": self.cookie_count,
            "domain_counts": dict(self.domain_counts),
            "notes": list(self.notes),
            "cookies": [dict(item) for item in self.cookies],
        }


def _refresh_manifest(store) -> None:
    """按当前内存中的 Cookie 刷新清单字段（避免"清单与实际内容不一致"）。"""
    store.manifest.cookie_count = len(store.cookies)
    store.manifest.domain_counts = {
        key: len(value) for key, value in group_by_domain(store.cookies).items()}
    store.manifest.fingerprint = cookie_fingerprint(store.cookies)


class CookieStore:
    """Cookie 的导出 / 持久化 / 加载 / 注入。

    典型用法::

        store = CookieStore(config)
        store.export_from_driver(driver)      # 从已登录的浏览器读出
        store.save()                          # 落盘（默认加密）
        ...
        store.load()                          # 读回
        store.inject_into_driver(driver)      # 注入新会话
    """

    def __init__(self, config: SessionConfig, driver=None, path: str = ""):
        self.config = config
        self.driver = driver
        self.path = path or config.cookie_store_path
        self.cookies: list[dict] = []
        self.manifest = SessionFile(domain=config.domain, browser=config.browser,
                                    profile=config.profile)
        self.notes: list[str] = []

    # ------------------------------------------------------------------
    # 导出
    # ------------------------------------------------------------------
    def export_from_driver(self, driver=None) -> list[dict]:
        """从已登录的 driver 读取 ``get_cookies()`` 并按域名分组保存到内存。

        :raises CookieInjectError: driver 不可用
        """
        active = driver or self.driver
        if active is None:
            raise CookieInjectError(
                "没有可用的浏览器驱动：请先启动浏览器（load_browser_session），"
                "或把 driver 传给 export_from_driver(driver)")
        page_url = ""
        try:
            page_url = active.current_url()
        except Exception:
            page_url = ""
        raw = list(active.get_cookies() or [])
        logger.info("从浏览器读取到 %d 条 Cookie（当前页面：%s）",
                    len(raw), page_url or "未知")

        normalized, dropped = normalize_cookies(raw, page_url, self.config.domain)
        accepted = filter_by_domain(normalized, self.config.domain,
                                    self.config.cookie_accept)
        self.cookies = accepted
        self.notes = list(dropped)
        self.manifest.domain = self.config.domain
        self.manifest.browser = self.config.browser
        self.manifest.profile = self.config.profile
        self.manifest.exported_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.manifest.cookie_count = len(accepted)
        self.manifest.domain_counts = {
            key: len(value) for key, value in group_by_domain(accepted).items()}
        self.manifest.fingerprint = cookie_fingerprint(accepted)
        logger.info("Cookie 导出完成：有效 %d 条，按域名分组 %d 组%s",
                    len(accepted), len(self.manifest.domain_counts),
                    f"，丢弃 {len(dropped)} 条非法条目" if dropped else "")
        return list(self.cookies)

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------
    def save(self, path: str = "", *, encrypt: bool | None = None) -> str:
        """把当前 Cookie 落盘为 JSON（默认按配置加密），返回写入路径。

        :raises CookieExportError: 没有可保存的 Cookie（避免写出一个"空登录态"
            误导后续流程，也避免覆盖上一次的有效导出）
        """
        target = path or self.path
        if not self.cookies:
            raise CookieExportError(
                "当前没有可保存的 Cookie：请先 export_from_driver() 导出。\n"
                f"可能原因：1) 目标 Profile（{self.config.profile}）没有登录过 "
                f"{self.config.domain or '目标站点'}；"
                "2) 浏览器还没访问过该站点（Cookie 由页面 JS 写入，"
                "请先 driver.get('https://<domain>/') 再导出）；"
                "3) domain 配置与 Cookie 的归属域名不一致。")
        want_encrypt = self.config.cookie_encrypt if encrypt is None else bool(encrypt)
        _refresh_manifest(self)          # 清单字段与真实内容保持一致
        payload = self.manifest.to_dict()
        payload["cookies"] = [dict(item) for item in self.cookies]

        directory = os.path.dirname(os.path.abspath(target)) or "."
        os.makedirs(directory, exist_ok=True)
        text, encrypted = self._serialize(payload, want_encrypt)
        # 原子写入：先写临时文件再替换，避免中途失败留下半个 Cookie 文件
        handle, temp_path = tempfile.mkstemp(prefix=".session_", dir=directory)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(text)
            os.replace(temp_path, target)
        except OSError as exc:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise CookieFileInvalid(f"写入 Cookie 文件失败：{target}（{exc}）") from exc

        self.path = target
        self.manifest.encrypted = encrypted
        logger.info("Cookie 已保存：%s（%d 条，%s）", target, len(self.cookies),
                    "加密" if encrypted else "**明文，建议开启加密**")
        if want_encrypt and not encrypted:
            self.notes.append(
                "未安装可选依赖 cryptography，Cookie 以明文落盘；"
                "如需加密请执行：pip install cryptography")
        return target

    def _serialize(self, payload: dict, want_encrypt: bool) -> tuple[str, bool]:
        """序列化：加密可用时输出「文本头 + 空行 + 密文」，否则输出普通 JSON。"""
        if want_encrypt and self.config.cookie_key:
            if not crypto_available():
                logger.warning("已启用 Cookie 加密但未安装 cryptography，本次以明文落盘")
                return json.dumps(payload, ensure_ascii=False, indent=2), False
            key = _derive_key(self.config.cookie_key)
            if not key:
                raise CookieDecryptError(
                    "Cookie 加密密钥派生失败：请检查 cookie_key / "
                    f"{app_config.DEFAULT_SESSION_COOKIE_KEY_ENV}")
            token = self._encrypt(json.dumps(payload, ensure_ascii=False), key)
            header = json.dumps({
                "encrypted": True, "algorithm": FERNET,
                "version": STORE_VERSION, "domain": payload.get("domain", ""),
                "exported_at": payload.get("exported_at", ""),
            }, ensure_ascii=False)
            return f"{ENCRYPTED_HEADER}{header}\n{token}", True
        if want_encrypt and not self.config.cookie_key:
            logger.warning("已启用 Cookie 加密但未配置密钥，本次以明文落盘"
                           "（可设置环境变量 %s）",
                           app_config.DEFAULT_SESSION_COOKIE_KEY_ENV)
        return json.dumps(payload, ensure_ascii=False, indent=2), False

    @staticmethod
    def _encrypt(plain: str, key: bytes) -> str:
        """加密（AES-128-CBC + HMAC 的 Fernet 认证加密）。"""
        try:
            from cryptography.fernet import Fernet
        except ImportError as exc:  # pragma: no cover - 由 crypto_available 前置判断
            raise CookieDecryptError(
                "需要可选依赖 cryptography 才能加密 Cookie：pip install cryptography") from exc
        return Fernet(key).encrypt(plain.encode("utf-8")).decode("ascii")

    @staticmethod
    def _decrypt(token: str, key: bytes) -> str:
        """解密；密钥不对或文件被篡改时抛 :class:`CookieDecryptError`。"""
        try:
            from cryptography.fernet import Fernet, InvalidToken
        except ImportError as exc:  # pragma: no cover - 由 crypto_available 前置判断
            raise CookieDecryptError(
                "需要可选依赖 cryptography 才能解密 Cookie 文件："
                "pip install cryptography") from exc
        try:
            return Fernet(key).decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise CookieDecryptError(
                "Cookie 文件解密失败：密钥不正确或文件已被篡改。请确认 "
                f"{app_config.DEFAULT_SESSION_COOKIE_KEY_ENV} / cookie_key 与导出时一致") from exc
        except Exception as exc:
            raise CookieDecryptError(f"Cookie 文件解密失败：{exc}") from exc

    # ------------------------------------------------------------------
    # 加载
    # ------------------------------------------------------------------
    def load(self, path: str = "", *, key: str = "") -> list[dict]:
        """从文件读入 Cookie（自动识别明文 / 加密），返回规范 Cookie 列表。

        :raises CookieFileNotFound: 文件不存在
        :raises CookieFileInvalid: 文件损坏或格式不识别
        :raises CookieDecryptError: 加密文件无法解密（密钥缺失/错误）
        """
        target = path or self.path
        if not target or not os.path.isfile(target):
            raise CookieFileNotFound(
                f"Cookie 文件不存在：{target or '（未配置路径）'}\n"
                "请先运行 export 流程导出一次（见模块 README 的最小示例），"
                "或把 cookie_store_path 指向已有的 Cookie JSON")
        try:
            with open(target, "r", encoding="utf-8") as stream:
                text = stream.read()
        except OSError as exc:
            raise CookieFileInvalid(f"读取 Cookie 文件失败：{target}（{exc}）") from exc

        payload = self._parse(text, target, secret=key or self.config.cookie_key)
        raw_cookies = payload.get("cookies")
        if not isinstance(raw_cookies, list):
            raise CookieFileInvalid(
                f"Cookie 文件缺少 cookies 列表：{target}\n"
                "期望格式见 core/browser_session/README.md 的「存储格式」一节")

        normalized, dropped = normalize_cookies(raw_cookies)
        self.cookies = normalized
        self.notes = list(payload.get("notes") or []) + dropped
        self.manifest = SessionFile(
            version=int(payload.get("version") or STORE_VERSION),
            domain=normalize_domain(payload.get("domain") or self.config.domain),
            browser=str(payload.get("browser") or ""),
            profile=str(payload.get("profile") or ""),
            exported_at=str(payload.get("exported_at") or ""),
            encrypted=bool(payload.get("encrypted")),
            fingerprint=str(payload.get("fingerprint") or cookie_fingerprint(normalized)),
            cookie_count=len(normalized),
            domain_counts=dict(payload.get("domain_counts")
                               or {k: len(v) for k, v in group_by_domain(normalized).items()}),
        )
        self.path = target
        logger.info("Cookie 已加载：%s（%d 条，导出时间 %s，指纹 %s）",
                    target, len(normalized), self.manifest.exported_at or "未知",
                    self.manifest.fingerprint[:12])
        return list(self.cookies)

    def _parse(self, text: str, path: str, secret: str = "") -> dict:
        """解析文件内容：``#!{...}`` 头视为加密文件，其余按 JSON 解析。"""
        stripped = text.lstrip()
        if stripped.startswith(ENCRYPTED_HEADER):
            return self._parse_encrypted(stripped, path, secret)
        if not stripped.startswith(_UNENCRYPTED_HEADER):
            raise CookieFileInvalid(
                f"Cookie 文件格式不识别：{path}\n"
                f"期望 JSON 对象（以 '{{' 开头）或加密文件（以 '{ENCRYPTED_HEADER}' 开头）")
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise CookieFileInvalid(f"Cookie 文件不是合法 JSON：{path}（{exc}）") from exc
        if not isinstance(payload, dict):
            raise CookieFileInvalid(f"Cookie 文件的顶层结构应为对象：{path}")
        # 兼容"裸列表"格式（部分浏览器插件的导出写法）
        if "cookies" not in payload and isinstance(payload.get("data"), list):
            payload = {"cookies": payload["data"]}
        payload.setdefault("encrypted", False)
        return payload

    def _parse_encrypted(self, text: str, path: str, secret: str) -> dict:
        """解析加密文件（头与密文），缺密钥时给出可操作提示。"""
        if not crypto_available():
            raise CookieDecryptError(
                f"Cookie 文件是加密的，但当前环境缺少可选依赖 cryptography：{path}\n"
                "请执行：pip install cryptography（或改用明文存储重新导出）")
        lines = text.split("\n", 1)
        if len(lines) < 2 or not lines[1].strip():
            raise CookieFileInvalid(f"加密 Cookie 文件内容不完整：{path}")
        if not secret:
            raise CookieDecryptError(
                f"Cookie 文件已加密，但未提供密钥：{path}\n"
                f"请设置环境变量 {app_config.DEFAULT_SESSION_COOKIE_KEY_ENV}"
                "（或配置 cookie_key / load(key=...)）")
        plain = self._decrypt(lines[1].strip(), _derive_key(secret))
        try:
            payload = json.loads(plain)
        except ValueError as exc:
            raise CookieFileInvalid(f"解密后的内容不是合法 JSON：{path}（{exc}）") from exc
        payload["encrypted"] = True
        return payload

    # ------------------------------------------------------------------
    # 注入
    # ------------------------------------------------------------------
    def inject_into_driver(self, driver=None, cookies: list[dict] | None = None,
                           *, navigate: bool = True, refresh: bool = True) -> int:
        """把 Cookie 注入浏览器上下文，返回成功条数。

        注入顺序（Chromium 的规则决定的，不能随意调换）：

        1. **先访问目标域名**（``Driver.get``）——``add_cookie`` 要求 Cookie 的
           domain 与当前页面 host 后缀匹配，否则抛 ``InvalidCookieDomainException``
        2. 按 domain 分组逐条 ``add_cookie``，单条失败只记原因不中断整批
        3. 最后**刷新页面**，让服务端下发的会话状态生效

        :raises CookieInjectError: 没有 Cookie 可注入、或所有条目都注入失败
        """
        active = driver or self.driver
        if active is None:
            raise CookieInjectError("没有可用的浏览器驱动：无法注入 Cookie")
        items = list(cookies if cookies is not None else self.cookies)
        if not items:
            raise CookieInjectError(
                "没有可注入的 Cookie：请先 export_from_driver() 或 load() 读取登录态")

        grouped = group_by_domain(items) if navigate else {"": items}
        applied, failures, navigated = 0, [], 0
        for domain, group in grouped.items():
            if navigate and domain:
                url = f"https://{domain}/"
                try:
                    active.get(url)
                    navigated += 1
                except Exception as exc:
                    failures.append(f"无法打开 {url}（{type(exc).__name__}），"
                                    f"该域名下 {len(group)} 条 Cookie 跳过注入")
                    continue
            ok, errors = active.add_cookies(group)
            applied += ok
            failures.extend(errors)

        if refresh and applied:
            try:
                active.refresh()
            except Exception as exc:
                logger.debug("注入后刷新页面失败（可忽略）：%s", exc)

        logger.info("Cookie 注入完成：成功 %d 条 / 共 %d 条（导航 %d 个域名）%s",
                    applied, len(items), navigated,
                    f"，失败 {len(failures)} 条" if failures else "")
        if failures:
            for reason in failures[:5]:
                logger.warning("注入失败：%s", reason)
        if applied == 0:
            raise CookieInjectError(
                "所有 Cookie 均注入失败，登录态未生效。常见原因：\n"
                "  1) Cookie 已过期（服务端会话失效）→ 在浏览器里手动登录后重新导出\n"
                "  2) 目标域名与 Cookie 归属域名不匹配 → 检查 config.domain\n"
                "  3) 只带 domain 未带 path，或 sameSite=None 但非 secure → 已自动规范化，"
                "若仍失败请查看上面的逐条原因\n"
                f"失败明细（前 5 条）：{failures[:5]}")
        return applied

    # ------------------------------------------------------------------
    # 导出给其他请求层
    # ------------------------------------------------------------------
    def as_requests_cookies(self, host: str = "") -> dict[str, str]:
        """返回 ``requests`` 可直接使用的 ``{name: value}`` 字典。"""
        target = host or self.config.domain
        return cookies_to_dict(filter_by_domain(self.cookies, target,
                                                self.config.cookie_accept))

    def as_cookie_jar(self, host: str = ""):
        """返回带 domain/path/secure 属性的 :class:`requests.cookies.RequestsCookieJar`。

        比"拍平成字典"更精确：能让 requests 按域名与路径自动决定是否发送，
        也更接近浏览器的真实行为（降低风控误判）。
        """
        try:
            from requests.cookies import RequestsCookieJar, create_cookie
        except ImportError as exc:
            raise CookieInjectError(
                "需要 requests 才能构建 cookie jar：pip install requests") from exc
        jar = RequestsCookieJar()
        for cookie in filter_by_domain(self.cookies, host or self.config.domain,
                                       self.config.cookie_accept):
            try:
                jar.set_cookie(create_cookie(
                    name=cookie["name"], value=cookie["value"],
                    domain=cookie.get("domain") or None,
                    path=cookie.get("path") or "/",
                    secure=bool(cookie.get("secure")),
                    expires=cookie.get("expiry"),
                ))
            except Exception as exc:  # 单条非法不影响其它 Cookie
                logger.debug("跳过无法加入 cookie jar 的条目 %s：%s",
                             cookie.get("name"), exc)
        return jar

    def summary(self) -> str:
        """当前登录态的可读摘要（不含 Cookie 值，可安全写入日志）。"""
        groups = group_by_domain(self.cookies)
        lines = [
            f"Cookie 总数：{len(self.cookies)} | 域名分组：{len(groups)}",
            f"存储文件：{self.path}（{'加密' if self.manifest.encrypted else '明文'}）",
            f"导出时间：{self.manifest.exported_at or '（未导出）'} | "
            f"指纹：{(self.manifest.fingerprint or cookie_fingerprint(self.cookies))[:16]}",
        ]
        for domain, items in sorted(groups.items()):
            names = "、".join(sorted({item["name"] for item in items})[:8])
            lines.append(f"  - {domain or '(host-only)'}：{len(items)} 条（{names}…）")
        return "\n".join(lines)

    def fingerprint(self) -> str:
        """当前内存中 Cookie 集合的指纹。"""
        return cookie_fingerprint(self.cookies)


__all__ = [
    "STORE_VERSION", "ENCRYPTED_HEADER", "FERNET", "SELENIUM_FIELDS",
    "PLAYWRIGHT_FIELDS", "SessionFile", "CookieStore",
    "normalize_cookie", "normalize_cookies", "normalize_expiry",
    "normalize_same_site", "domain_of", "domain_family", "filter_by_domain",
    "group_by_domain", "cookies_to_dict", "cookie_fingerprint",
    "to_selenium_cookie", "to_playwright_cookie", "crypto_available",
]
