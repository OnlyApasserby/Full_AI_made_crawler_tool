"""AntiDetection 子模块：指纹一致性与行为拟人化（**不实现任何破解逻辑**）。

能力边界（务必先读）
--------------------
本模块只做两件事：

1. **指纹一致性**：让自动化浏览器的可见特征（``navigator.webdriver``、
   ``plugins``、``languages``、``chrome.runtime``、WebGL 厂商、时区、地理位置）
   与一个真实浏览器保持一致，并且与所用代理 IP 的地理位置匹配
2. **行为拟人化**：随机化操作间隔、用贝塞尔曲线模拟鼠标轨迹、按人类节奏输入、
   限制请求速率

**不做**的事：识别/绕过验证码、破解加密、伪造他人身份、规避站点的访问控制。
所有手段都只作用于"降低被风控误判的概率"，**不承诺绕过任何审核**。
"""

from __future__ import annotations

import math
import random
import threading
import time

from core.browser_session.config import SessionConfig

#: 注入脚本里用到的默认视口
DEFAULT_LANGUAGES = ("zh-CN", "zh", "en-US", "en")


# ---------------------------------------------------------------------------
# 行为拟人化工具
# ---------------------------------------------------------------------------
def rand_delay(low: float, high: float) -> float:
    """在 ``[low, high]`` 内随机等待并返回实际等待秒数（区间非法时按较小值处理）。"""
    low, high = float(low or 0.0), float(high or 0.0)
    if high < low:
        low, high = high, low
    seconds = random.uniform(low, high) if high > low else max(0.0, low)
    if seconds > 0:
        time.sleep(seconds)
    return seconds


def random_delay(mean: float = 0.0, jitter: float = 0.5) -> float:
    """按「均值 + 抖动」生成拟人化停顿（指数分布，长尾更接近真人操作节奏）。

    :param mean: 平均停顿秒数（<=0 表示关闭，直接返回 0）
    :param jitter: 抖动幅度（秒），在 ``mean ± jitter`` 范围内取值
    """
    if mean is None or float(mean) <= 0:
        return 0.0
    mean = float(mean)
    spread = max(0.0, float(jitter or 0.0))
    # 指数分布：多数停顿较短，偶尔出现较长停顿——比均匀分布更像真人
    seconds = min(random.expovariate(1.0 / mean), mean + spread * 2)
    seconds = max(0.05, seconds)
    time.sleep(seconds)
    return seconds


def keystroke_delays(text: str, mean: float = 0.12) -> list[float]:
    """为一段文本生成逐字符的打字间隔（供驱动层逐字输入时使用）。"""
    return [max(0.02, random.expovariate(1.0 / max(0.02, float(mean))))
            for _ in str(text or "")]


def bezier_path(start: tuple, end: tuple, steps: int = 24,
                curvature: float = 0.25) -> list[tuple]:
    """生成两点之间的三次贝塞尔轨迹（带随机控制点，模拟真人鼠标移动）。

    :param steps: 插值点数（越大越平滑，建议 16–40）
    :param curvature: 控制点偏移比例（0 为直线，0.25 左右最像人工）
    :return: 轨迹点列表，含起点与终点
    """
    try:
        count = 24 if steps is None else int(steps)
    except (TypeError, ValueError):
        count = 24
    steps = min(400, max(2, count))
    (x0, y0), (x1, y1) = (float(start[0]), float(start[1])), (float(end[0]), float(end[1]))
    dx, dy = x1 - x0, y1 - y0
    distance = math.hypot(dx, dy) or 1.0
    # 与运动方向垂直的单位向量，用来把控制点推向"侧面"
    normal = (-dy / distance, dx / distance)
    bend1 = distance * float(curvature) * random.uniform(0.4, 1.0)
    bend2 = distance * float(curvature) * random.uniform(0.4, 1.0)
    control1 = (x0 + dx * 0.3 + normal[0] * bend1, y0 + dy * 0.3 + normal[1] * bend1)
    control2 = (x0 + dx * 0.7 + normal[0] * bend2, y0 + dy * 0.7 + normal[1] * bend2)

    points: list[tuple] = []
    for index in range(steps + 1):
        t = index / steps
        # ease-in-out：起步慢、中段快、收尾慢
        eased = 3 * t ** 2 - 2 * t ** 3
        inv = 1 - eased
        x = (inv ** 3) * x0 + 3 * (inv ** 2) * eased * control1[0] \
            + 3 * inv * (eased ** 2) * control2[0] + (eased ** 3) * x1
        y = (inv ** 3) * y0 + 3 * (inv ** 2) * eased * control1[1] \
            + 3 * inv * (eased ** 2) * control2[1] + (eased ** 3) * y1
        points.append((round(x, 2), round(y, 2)))
    return points


def human_scroll(driver, times: int = 1, pixels: tuple = (200, 700)) -> int:
    """模拟真人滚动（分步 + 随机停顿），返回实际滚动次数。

    懒加载页面依赖滚动触发；一次性 ``scrollTo(底部)`` 是很明显的自动化特征。
    """
    done = 0
    for _ in range(max(0, int(times or 0))):
        step = random.randint(int(min(pixels)), int(max(pixels)))
        try:
            driver.execute_script(f"window.scrollBy({{top: {step}, behavior: 'smooth'}});")
        except Exception:
            return done
        done += 1
        rand_delay(0.3, 0.9)
    return done


class RateLimiter:
    """滑动窗口限速器（线程安全）：确保每分钟请求数不超过配置上限。

    ``rate_limit <= 0`` 表示不限速，此时所有等待方法都是空操作。
    """

    def __init__(self, per_minute: float = 0.0, min_interval: float = 0.0):
        self.per_minute = max(0.0, float(per_minute or 0.0))
        self.min_interval = max(0.0, float(min_interval or 0.0))
        self._lock = threading.Lock()
        self._events: list[float] = []
        self._last: float = 0.0

    @property
    def enabled(self) -> bool:
        return self.per_minute > 0 or self.min_interval > 0

    def wait(self) -> float:
        """阻塞到允许发送下一个请求，返回实际等待秒数。"""
        if not self.enabled:
            return 0.0
        waited = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                delay = 0.0
                if self.min_interval > 0 and self._last:
                    delay = max(delay, self._last + self.min_interval - now)
                if self.per_minute > 0:
                    window = now - 60.0
                    self._events = [item for item in self._events if item > window]
                    if len(self._events) >= self.per_minute:
                        delay = max(delay, self._events[0] + 60.0 - now)
                if delay <= 0:
                    self._events.append(now)
                    self._last = now
                    return waited
            # 释放锁后再睡，避免长时间持锁阻塞其它线程
            time.sleep(min(delay, 5.0))
            waited += min(delay, 5.0)

    def reset(self) -> None:
        """清空窗口记录（切换任务/账号时调用）。"""
        with self._lock:
            self._events.clear()
            self._last = 0.0


# ---------------------------------------------------------------------------
# 指纹一致性脚本
# ---------------------------------------------------------------------------
def stealth_script(config: SessionConfig | None = None, *,
                   languages: tuple = DEFAULT_LANGUAGES,
                   timezone: str = "", geolocation: tuple | None = None,
                   vendor: str = "Google Inc.",
                   renderer: str = "ANGLE (Intel, Intel(R) UHD Graphics Direct3D11)") -> str:
    """生成注入用的反检测脚本（幂等，可安全重复注入）。

    覆盖项：``navigator.webdriver``、``plugins`` / ``mimeTypes``、
    ``languages``、``chrome.runtime``、``permissions.query``、
    WebGL 厂商与渲染器、时区、地理位置。

    .. note::
       时区与地理位置**必须与代理 IP 的地理位置一致**，否则"IP 在美国、
       时区是亚洲/上海"本身就是强风控特征。本函数只负责把配置写进浏览器，
       两者是否匹配需要使用者自行确认。
    """
    config = config or SessionConfig()
    lang_list = list(languages or DEFAULT_LANGUAGES)
    tz = timezone or config.timezone or ""
    geo = geolocation if geolocation is not None else config.geolocation
    parts = [
        # 1) 抹除自动化标志
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});",
        # 2) plugins / mimeTypes：无头浏览器默认是空数组，这里补齐常见插件
        """
        const fakePlugin = (name, filename, desc) => {
            const plugin = Object.create(Plugin.prototype);
            Object.defineProperties(plugin, {
                name: {value: name}, filename: {value: filename},
                description: {value: desc}, length: {value: 1},
            });
            return plugin;
        };
        Object.defineProperty(navigator, 'plugins', {
            get: () => [
                fakePlugin('PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
                fakePlugin('Chrome PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
                fakePlugin('Chromium PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
            ],
        });
        Object.defineProperty(navigator, 'mimeTypes', {
            get: () => [{type: 'application/pdf', suffixes: 'pdf',
                         description: 'Portable Document Format'}],
        });
        """,
        # 3) languages
        f"Object.defineProperty(navigator, 'languages', "
        f"{{get: () => {lang_list!r}}});",
        f"Object.defineProperty(navigator, 'language', "
        f"{{get: () => {lang_list[0]!r}}});",
        # 4) chrome 运行时对象（真实 Chrome 存在，无头实例常缺失）
        "window.chrome = window.chrome || {};"
        "window.chrome.runtime = window.chrome.runtime || {};"
        "window.chrome.app = window.chrome.app || {isInstalled: false};",
        # 5) permissions.query('notifications') 的一致性
        """
        if (navigator.permissions && navigator.permissions.query) {
            const originalQuery = navigator.permissions.query.bind(navigator.permissions);
            navigator.permissions.query = (parameters) => (
                parameters && parameters.name === 'notifications'
                    ? Promise.resolve({state: Notification.permission})
                    : originalQuery(parameters));
        }
        """,
        # 6) WebGL 厂商/渲染器（软件渲染是典型风控特征）
        f"""
        const patchWebGL = (proto) => {{
            if (!proto) return;
            const original = proto.getParameter;
            proto.getParameter = function (parameter) {{
                if (parameter === 37445) return {vendor!r};       // UNMASKED_VENDOR_WEBGL
                if (parameter === 37446) return {renderer!r};     // UNMASKED_RENDERER_WEBGL
                return original.apply(this, [parameter]);
            }};
        }};
        patchWebGL(window.WebGLRenderingContext && WebGLRenderingContext.prototype);
        patchWebGL(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype);
        """,
    ]
    if tz:
        parts.append(f"""
        try {{
            const targetZone = {tz!r};
            const dtf = Intl.DateTimeFormat;
            const originalResolved = dtf.prototype.resolvedOptions;
            dtf.prototype.resolvedOptions = function () {{
                const options = originalResolved.call(this);
                options.timeZone = targetZone;
                return options;
            }};
            Date.prototype.getTimezoneOffset = function () {{
                const now = new Date();
                const utc = new Date(now.toLocaleString('en-US', {{timeZone: 'UTC'}}));
                const local = new Date(now.toLocaleString('en-US', {{timeZone: targetZone}}));
                return (utc - local) / 60000;
            }};
        }} catch (error) {{ /* 时区伪装失败不影响其它指纹项 */ }}
        """)
    if geo:
        try:
            latitude, longitude = float(geo[0]), float(geo[1])
        except (TypeError, ValueError, IndexError):
            latitude = longitude = None
        if latitude is not None:
            parts.append(f"""
            if (navigator.geolocation) {{
                const coords = {{latitude: {latitude}, longitude: {longitude},
                                 accuracy: 50, altitude: null, altitudeAccuracy: null,
                                 heading: null, speed: null}};
                const report = (success) => success &&
                    success({{coords, timestamp: Date.now()}});
                navigator.geolocation.getCurrentPosition = (success) => report(success);
                navigator.geolocation.watchPosition = (success) => {{
                    report(success); return 0;
                }};
            }}
            """)
    return "\n".join(parts)


def apply_stealth(driver, config: SessionConfig | None = None) -> bool:
    """给已启动的驱动注入反检测脚本（Playwright 请在 init script 阶段注入）。

    :return: 是否注入成功（不支持 JS 执行时返回 False）
    """
    script = stealth_script(config)
    try:
        driver.execute_script(script)
        return True
    except Exception:
        return False


def proxy_for_requests(config: SessionConfig) -> dict:
    """requests 风格的代理配置（``{"http": ..., "https": ...}``）。"""
    server = config.proxy_server()
    if not server:
        return {}
    return {"http": server, "https": server}


def proxy_for_playwright(config: SessionConfig) -> dict:
    """playwright ``launch`` / ``launch_persistent_context`` 的 proxy 结构。"""
    out: dict = {}
    for key in ("server", "username", "password"):
        value = (config.proxy or {}).get(key)
        if value:
            out[key] = str(value)
    if not out.get("server"):
        server = config.proxy_server()
        if server:
            out["server"] = server
    return out


def proxy_for_selenium(config: SessionConfig) -> list[str]:
    """selenium 的 Chrome 启动参数（``--proxy-server=``）。"""
    server = config.proxy_server()
    return [f"--proxy-server={server}"] if server else []


__all__ = [
    "DEFAULT_LANGUAGES", "rand_delay", "random_delay", "keystroke_delays",
    "bezier_path", "human_scroll", "RateLimiter", "stealth_script",
    "apply_stealth", "proxy_for_requests", "proxy_for_playwright",
    "proxy_for_selenium",
]
