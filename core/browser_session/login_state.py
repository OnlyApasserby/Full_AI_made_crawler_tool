"""LoginStateChecker 子模块：登录态有效性判断与失效重登入口。

探测规则（按优先级从上到下，命中即返回）
----------------------------------------
1. **登出关键字/选择器**（``logout_markers`` / ``logout_selector``）——最强信号：
   页面出现"请登录/登录后查看"即判定失效（优先于任何正向信号）
2. **登录关键字/选择器**（``login_markers`` / ``login_selector``）——出现即有效
3. **状态码**（``login_status``）——如"未记录到 302 跳登录页"即视为有效；
   期望状态码列表命中即有效
4. **兜底**（``cookie_assume_valid``）——以上都没配时，用"Cookie 是否过期"
   这一静态判据给出结论，并在结果里注明这是兜底推断

两种检测通道
------------
- **browser**：真实浏览器（能跑选择器、能看最终落点 URL，最准）
- **http**：requests（快、可离线复用 Cookie，但没有 JS 渲染）

返回值统一为 :class:`CheckResult`（``bool(check_result)`` 即"是否已登录"），
同时保留判定依据与证据片段，便于日志留痕与界面提示。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from core.browser_session.config import SessionConfig, normalize_domain
from core.browser_session.exceptions import LoginStateError
from core.browser_session.session_bridge import SessionBridge
from utils.logging_setup import get_logger

logger = get_logger("core.browser_session.login_state")

#: 判定所依据的通道
CHANNEL_BROWSER = "browser"
CHANNEL_HTTP = "http"
CHANNEL_COOKIE = "cookie"

#: 未登录页面里高频出现的兜底关键字（仅在用户未配置 logout_markers 时启用）
_FALLBACK_LOGOUT_MARKERS = (
    "请先登录", "请登录", "立即登录", "登录后查看", "登录后可见", "您尚未登录",
    "未登录", "登录已过期", "会话已过期", "重新登录", "sign in", "log in",
    "please log in", "please sign in", "session expired", "not logged in",
    "unauthorized",
)
#: 已登录页面里高频出现的兜底关键字（仅在用户未配置 login_markers 时启用）
#: 注意不要放"退出"这类孤立词——它也是"退出活动/退出全屏"的常见开头，
#: 会产生误判；宁可用更长的具体词。
_FALLBACK_LOGIN_MARKERS = (
    "退出登录", "注销登录", "我的账户", "个人中心", "我的主页", "会员中心",
    "我的订单", "欢迎回来", "已登录", "log out", "logout", "sign out",
    "my account", "dashboard", "profile", "welcome back",
)

#: 文本证据在结果里的截断长度（避免把整页 HTML 灌进日志）
_EVIDENCE_CHARS = 160


def _normalize_markers(values) -> list[str]:
    """把关键字配置归一为小写字符串列表（支持中英文、逗号分隔字符串）。"""
    if isinstance(values, str):
        items = [item.strip() for item in values.split(",")]
    else:
        items = [str(item).strip() for item in (values or [])]
    return [item.lower() for item in items if item]


def _find_marker(text: str, markers: list[str]) -> str:
    """返回命中的第一个关键字（未命中返回空串）。"""
    lowered = (text or "").lower()
    for marker in markers:
        if marker and marker in lowered:
            return marker
    return ""


def _snippet(text: str, marker: str) -> str:
    """截取关键字附近的文本片段作为证据。"""
    lowered = (text or "").lower()
    index = lowered.find(marker) if marker else -1
    if index < 0:
        return (text or "")[:_EVIDENCE_CHARS]
    start = max(0, index - 40)
    return (text or "")[start:start + _EVIDENCE_CHARS].replace("\n", " ")


def _same_site(url: str, domain: str) -> bool:
    """URL 是否仍停留在目标站点（跳转到登录页/第三方域名是失效的强信号）。"""
    host = normalize_domain(url)
    target = normalize_domain(domain)
    if not host or not target:
        return True
    return host == target or host.endswith("." + target) or target.endswith("." + host)


@dataclass
class CheckRule:
    """登录态探测规则（可由配置构造，也可单独用于测试）。"""

    url: str = ""
    login_status: list = field(default_factory=lambda: [200])
    login_markers: list = field(default_factory=list)
    logout_markers: list = field(default_factory=list)
    login_selector: str = ""
    logout_selector: str = ""
    timeout: float = 10.0
    domain: str = ""
    assume_valid: bool = True

    @classmethod
    def from_config(cls, config: SessionConfig) -> "CheckRule":
        """由 :class:`SessionConfig` 构造规则（自动补全探测 URL）。"""
        url = config.login_url or (f"https://{config.domain}/" if config.domain else "")
        return cls(
            url=url,
            login_status=list(config.login_status or [200]),
            login_markers=_normalize_markers(config.login_markers),
            logout_markers=_normalize_markers(config.logout_markers),
            login_selector=config.login_selector or "",
            logout_selector=config.logout_selector or "",
            timeout=float(config.check_timeout or 10.0),
            domain=config.domain,
            assume_valid=bool(config.cookie_assume_valid),
        )

    def resolved_logout_markers(self) -> list[str]:
        """实际使用的登出关键字（未配置时启用内置兜底清单）。"""
        return self.logout_markers or list(_FALLBACK_LOGOUT_MARKERS)

    def resolved_login_markers(self) -> list[str]:
        """实际使用的登录关键字（未配置时启用内置兜底清单）。"""
        return self.login_markers or list(_FALLBACK_LOGIN_MARKERS)

    def describe(self) -> str:
        """规则的可读摘要（写日志用，不含 Cookie）。"""
        return (f"URL={self.url or '（未配置）'} | 状态码={self.login_status} | "
                f"登出关键字={len(self.logout_markers)}(内置 "
                f"{len(_FALLBACK_LOGOUT_MARKERS)}) | "
                f"选择器={self.logout_selector or self.login_selector or '（无）'}")


@dataclass
class CheckResult:
    """登录态检查结果（``bool(result)`` 即"是否已登录"）。"""

    logged_in: bool
    reason: str = ""
    evidence: str = ""
    channel: str = ""
    url: str = ""
    status: int = 0
    final_url: str = ""
    checked_at: str = ""
    cookie_total: int = 0
    expired: int = 0
    fallback: bool = False          # 是否走了"Cookie 静态兜底"推断
    elapsed: float = 0.0
    warnings: list = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.logged_in)

    def describe(self) -> str:
        """单行可读结论（可直接写入界面日志）。"""
        verdict = "已登录" if self.logged_in else "未登录"
        detail = f"（{self.channel} 通道" + ("，兜底推断" if self.fallback else "") + "）"
        return f"登录态检查：{verdict}{detail} — {self.reason}"

    def to_dict(self) -> dict:
        """结构化输出（供界面 / 报告使用；不含 Cookie 值）。"""
        return {
            "logged_in": self.logged_in, "reason": self.reason,
            "evidence": self.evidence, "channel": self.channel, "url": self.url,
            "status": self.status, "final_url": self.final_url,
            "checked_at": self.checked_at, "cookie_total": self.cookie_total,
            "expired": self.expired, "fallback": self.fallback,
            "elapsed": round(self.elapsed, 3), "warnings": list(self.warnings),
        }


def expired_cookies(cookies, now: float | None = None) -> list[dict]:
    """返回已过期的 Cookie 列表（无 ``expiry`` 的会话 Cookie 不算过期）。"""
    moment = time.time() if now is None else float(now)
    return [item for item in (cookies or [])
            if item.get("expiry") and float(item["expiry"]) <= moment]


class LoginStateChecker:
    """登录态检查器：判断当前会话是否仍然有效，并给出失效后的处理入口。"""

    def __init__(self, config: SessionConfig, driver=None,
                 bridge: SessionBridge | None = None, rule: CheckRule | None = None):
        self.config = config
        self.driver = driver
        self.bridge = bridge
        self.rule = rule or CheckRule.from_config(config)

    # ------------------------------------------------------------------
    # 对外主入口
    # ------------------------------------------------------------------
    def check(self, *, channel: str = "", url: str = "") -> CheckResult:
        """检查登录态：优先用浏览器通道，不可用时退回 http / cookie 通道。

        :param channel: 强制指定 ``browser`` / ``http`` / ``cookie``
        :param url: 覆盖探测 URL
        :raises LoginStateError: 探测过程本身失败（没有可用通道、URL 未配置等）
        """
        started = time.time()
        target = url or self.rule.url
        if channel:
            result = self._run_channel(channel, target)
        elif self.driver is not None:
            result = self._run_channel(CHANNEL_BROWSER, target)
        elif self._has_http_credentials():
            result = self._run_channel(CHANNEL_HTTP, target)
        else:
            result = self._run_channel(CHANNEL_COOKIE, target)
        result.elapsed = time.time() - started
        result.checked_at = time.strftime("%Y-%m-%d %H:%M:%S")
        if result.cookie_total == 0 and self.bridge is not None:
            result.cookie_total = len(self.bridge.store.cookies)
        logger.info("登录态检查结果：%s", result.describe())
        for warning in result.warnings:
            logger.warning("登录态检查提示：%s", warning)
        return result

    def is_logged_in(self, **kwargs) -> bool:
        """只要布尔结论的便捷入口。"""
        return bool(self.check(**kwargs))

    # ------------------------------------------------------------------
    # 通道实现
    # ------------------------------------------------------------------
    def _run_channel(self, channel: str, url: str) -> CheckResult:
        if channel == CHANNEL_BROWSER:
            return self._check_browser(url)
        if channel == CHANNEL_HTTP:
            return self._check_http(url)
        if channel == CHANNEL_COOKIE:
            return self._check_cookies()
        raise ValueError(f"未知的检查通道：{channel!r}，"
                         f"可选：{CHANNEL_BROWSER} / {CHANNEL_HTTP} / {CHANNEL_COOKIE}")

    def _base_result(self, channel: str, url: str = "") -> CheckResult:
        cookies = self.bridge.store.cookies if self.bridge is not None else []
        expired = expired_cookies(cookies)
        return CheckResult(logged_in=False, channel=channel, url=url,
                           cookie_total=len(cookies), expired=len(expired))

    def _check_browser(self, url: str) -> CheckResult:
        """浏览器通道：能跑选择器、能看最终落点，最准确。"""
        result = self._base_result(CHANNEL_BROWSER, url)
        if not url:
            raise LoginStateError(
                "未配置登录态探测 URL：请设置 login_url 或 domain"
                "（有 domain 时会自动用 https://<domain>/）")
        try:
            self.driver.get(url)
        except Exception as exc:
            raise LoginStateError(
                f"打开探测页面失败：{url}（{type(exc).__name__}: {str(exc)[:120]}）\n"
                "请确认网络/代理可用，或把 login_url 换成更轻量的受保护页面") from exc

        result.final_url = self.driver.current_url()
        if not _same_site(result.final_url, self.rule.domain or self.config.domain):
            result.logged_in = False
            result.reason = f"被重定向到站外或登录页：{result.final_url}"
            result.evidence = result.final_url[:_EVIDENCE_CHARS]
            return result

        # 1) 登出选择器（最强负信号）
        if self.rule.logout_selector and self._selector_present(self.rule.logout_selector):
            result.logged_in = False
            result.reason = f"命中登出选择器：{self.rule.logout_selector}"
            result.evidence = self.rule.logout_selector
            return result
        # 2) 登录选择器（强正信号）
        if self.rule.login_selector and self._selector_present(self.rule.login_selector):
            result.logged_in = True
            result.reason = f"命中登录选择器：{self.rule.login_selector}"
            result.evidence = self.rule.login_selector
            return result

        try:
            text = self.driver.text()
        except Exception:
            text = self.driver.page_source()
        return self._judge_text(result, text, self.rule.login_status, self.rule.assume_valid)

    def _check_http(self, url: str) -> CheckResult:
        """http 通道：用 requests + 已导入的 Cookie 探测（无 JS 渲染）。"""
        result = self._base_result(CHANNEL_HTTP, url)
        if not url:
            raise LoginStateError(
                "未配置登录态探测 URL：请设置 login_url 或 domain")
        try:
            session = self.bridge.get_requests_session()
        except Exception as exc:
            raise LoginStateError(
                f"无法构造带登录态的请求会话：{exc}") from exc
        try:
            response = session.get(url, timeout=float(self.rule.timeout),
                                   allow_redirects=True)
        except Exception as exc:
            raise LoginStateError(
                f"探测请求失败：{url}（{type(exc).__name__}: {str(exc)[:120]}）") from exc
        finally:
            try:
                session.close()
            except Exception:
                pass

        result.status = int(response.status_code)
        result.final_url = str(response.url or url)
        if not _same_site(result.final_url, self.rule.domain or self.config.domain):
            result.logged_in = False
            result.reason = f"被重定向到站外或登录页：{result.final_url}"
            result.evidence = result.final_url[:_EVIDENCE_CHARS]
            return result
        text = response.text or ""
        return self._judge_text(result, text, self.rule.login_status, self.rule.assume_valid)

    def _check_cookies(self) -> CheckResult:
        """cookie 通道：只看本地 Cookie 的静态状态（最简单的兜底）。"""
        result = self._base_result(CHANNEL_COOKIE)
        result.fallback = True
        if result.cookie_total == 0:
            result.logged_in = False
            result.reason = "没有任何可用的 Cookie（未导出登录态，或已全部被清理）"
            return result
        if result.expired == result.cookie_total:
            result.logged_in = False
            result.reason = f"全部 {result.cookie_total} 条 Cookie 均已过期"
            return result
        if result.expired:
            result.warnings.append(
                f"有 {result.expired}/{result.cookie_total} 条 Cookie 已过期，"
                "剩余部分可能仍可用；建议重新导出一次")
        logged_in = bool(self.rule.assume_valid)
        result.logged_in = logged_in
        result.reason = (
            f"本地有 {result.cookie_total - result.expired} 条未过期 Cookie，"
            "按兜底策略判定为有效（未配置探测规则，建议配置 login_markers 或探测 URL 以提高准确度）"
            if logged_in else
            "本地有未过期 Cookie，但按兜底策略不判定为已登录（cookie_assume_valid=False）")
        return result

    def _selector_present(self, selector: str) -> bool:
        """选择器是否存在于当前页面（不支持时视为不存在，不抛异常）。"""
        if not selector:
            return False
        try:
            return bool(self.driver.wait_for_selector(selector, timeout=3.0))
        except NotImplementedError:
            try:
                from bs4 import BeautifulSoup
            except ImportError:
                return False
            try:
                soup = BeautifulSoup(self.driver.page_source(), "html.parser")
                return soup.select_one(selector) is not None
            except Exception:
                return False
        except Exception:
            return False

    def _judge_text(self, result: CheckResult, text: str, statuses, assume_valid: bool) -> CheckResult:
        """文本 + 状态码判定（浏览器与 http 通道共用）。"""
        # 1) 登出关键字优先：页面出现"请登录"这类字样时，任何正向信号都不足采信
        logout_markers = self.rule.resolved_logout_markers()
        marker = _find_marker(text, logout_markers)
        if marker:
            result.logged_in = False
            result.reason = f"页面出现未登录关键字：{marker!r}"
            result.evidence = _snippet(text, marker)
            # 若同一页面也命中"已登录"关键字，说明规则需要收紧——给出冲突提示。
            # 注意这里用**实际参与判定**的关键字集合（含内置兜底词），
            # 否则产出"没有配置"时冲突永远检测不出来。
            conflict = _find_marker(text, self.rule.resolved_login_markers())
            if conflict:
                result.warnings.append(
                    f"同一页面同时命中登出关键字 {marker!r} 与登录关键字 {conflict!r}，"
                    "已按「登出优先」判定；建议把关键字写得更具体")
            return result
        # 2) 登录关键字
        marker = _find_marker(text, self.rule.resolved_login_markers())
        if marker:
            result.logged_in = True
            result.reason = f"页面出现已登录关键字：{marker!r}"
            result.evidence = _snippet(text, marker)
            return result
        # 3) 状态码
        if statuses:
            if result.status:
                if result.status in statuses:
                    result.logged_in = True
                    result.reason = f"HTTP {result.status} 命中预期状态码 {statuses}"
                    return result
                result.logged_in = False
                result.reason = f"HTTP {result.status} 不在预期状态码 {statuses} 内"
                return result
            # 浏览器通道拿不到状态码：用"页面是否非空"做弱判据
            if text.strip():
                result.logged_in = True
                result.reason = ("浏览器通道无法读取状态码，页面内容非空，"
                                 "按配置的预期状态码视为有效（建议配置 login_markers 提高准确度）")
                result.warnings.append(
                    "浏览器通道下 status=0，状态码规则未生效；"
                    "建议改用 logout_markers / logout_selector 判定")
                return result
        # 4) 兜底
        result.fallback = True
        result.logged_in = bool(assume_valid)
        result.reason = ("未命中任何探测规则；已按 cookie_assume_valid="
                         f"{assume_valid} 兜底判定（建议配置 logout_markers 或探测 URL）")
        return result

    def _has_http_credentials(self) -> bool:
        """是否具备 http 通道的条件（有 bridge 且有 Cookie）。"""
        return self.bridge is not None and bool(self.bridge.store.cookies)

    # ------------------------------------------------------------------
    # 失效重登入口
    # ------------------------------------------------------------------
    def relogin_steps(self, result: CheckResult | None = None) -> list[str]:
        """返回"手动登录一次并重新导出"的可操作步骤（界面可直接展示）。"""
        steps = [
            "1) 关闭所有正在使用该 User Data 目录的浏览器窗口"
            "（Chromium 对同一 User Data 是独占的，开着会启动失败）",
            f"2) 启动带登录态的浏览器：profile={self.config.profile}，"
            f"User Data={self.config.user_data_dir}",
            f"3) 在弹出的窗口里手动访问 {self.rule.url or '目标站点'} 并完成登录"
            "（含短信/扫码等人工验证步骤——本模块不做任何验证码绕过）",
            "4) 登录成功后回到本工具执行「导出 Cookie」，把新登录态落盘",
            "5) 再次执行登录态检查，确认返回 True 后继续抓取",
        ]
        if result is not None and not result.logged_in:
            steps.append(f"（本次判定依据：{result.reason}）")
        return steps

    def relogin_hint(self, result: CheckResult | None = None) -> str:
        """把重登步骤拼成一段文本（用于日志 / 提示框）。"""
        header = "登录态已失效，需要人工登录一次后重新导出 Cookie："
        return header + "\n" + "\n".join(self.relogin_steps(result))

    def require_login(self, **kwargs) -> CheckResult:
        """检查并要求已登录：未登录时抛 :class:`LoginStateError`（带重登步骤）。

        适用于"抓取前必须确认已登录"的执行器场景；只想拿布尔值时用
        :meth:`check`。
        """
        result = self.check(**kwargs)
        if not result.logged_in:
            raise LoginStateError(self.relogin_hint(result))
        return result


__all__ = [
    "CHANNEL_BROWSER", "CHANNEL_HTTP", "CHANNEL_COOKIE",
    "CheckRule", "CheckResult", "LoginStateChecker", "expired_cookies",
]
