"""动态抓取器：Playwright 浏览器渲染 + 网络（XHR）监听。

为什么需要它
------------
现代图片站/视频站的首屏 HTML 里往往**没有**真实资源地址：
- 懒加载：真实地址写在 ``data-src`` 等属性里，靠 JS 在滚动时替换
- 瀑布流/无限滚动：滚动时才请求下一页接口
- 播放器：真实播放地址在 XHR 响应里，甚至带签名与时效

浏览器渲染 + ``page.on("request"/"response")`` 监听可以一次性拿到这些地址，
并且请求头（Cookie / Referer）由浏览器原样带下来，直接复用到下载阶段即可
绕过常见防盗链。

注意：网络监听只采集**元数据**，不读取响应体（读 body 会显著放大内存，
且对大文件无意义）。
"""

from __future__ import annotations

import threading

from core.fetcher import browser_pool, chrome_setup
from core.fetcher.base import (
    DEFAULT_LOCALE, DEFAULT_VIEWPORT, BaseFetcher, FetchContext, NetworkEvent,
    interruptible_sleep,
)
from core.media.detector import MEDIA_TYPES, classify_content_type

#: 滚动脚本：返回滚动后的页面高度（用于判断是否已触底）
_SCROLL_JS = "window.scrollTo(0, document.body.scrollHeight); document.body.scrollHeight"
_HEIGHT_JS = "document.body ? document.body.scrollHeight : 0"


class NetworkRecorder:
    """页面网络事件采集器（同页面内按 URL 去重合并）。

    - ``request`` 事件：立刻拿到 URL / 资源类型 / **请求头**（Cookie、Referer），
      被 ``route`` 中止的请求同样会被记录，避免漏抓
    - ``response`` 事件：补充状态码与 Content-Type，用于精确判定资源类型
    """

    def __init__(self, max_events: int = 5000):
        self._lock = threading.Lock()
        self._events: dict[str, NetworkEvent] = {}
        self._max_events = max(1, int(max_events))

    def attach(self, page) -> None:
        """挂载到页面（必须在 ``goto`` 之前调用，否则首屏请求会漏采）。"""
        page.on("request", self._on_request)
        page.on("response", self._on_response)

    def reset(self) -> None:
        """清空采集结果（每次抓取新页面时调用，避免跨页累积占用内存）。"""
        with self._lock:
            self._events.clear()

    def _record(self, url: str, create: bool = True) -> NetworkEvent | None:
        """取（或创建）该 URL 的事件记录；超出上限时返回 None。"""
        event = self._events.get(url)
        if event is None:
            if not create or len(self._events) >= self._max_events:
                return None
            event = NetworkEvent(url=url)
            self._events[url] = event
        return event

    def _on_request(self, request) -> None:
        try:
            url = request.url
            if not url.startswith(("http://", "https://")):
                return
            with self._lock:
                event = self._record(url)
                if event is None:
                    return
                event.method = request.method or event.method
                event.resource_type = request.resource_type or event.resource_type
                headers = getattr(request, "headers", None)
                if headers:
                    event.request_headers.update(headers)
        except Exception:  # 回调异常不能影响页面加载
            return

    def _on_response(self, response) -> None:
        try:
            url = response.url
            if not url.startswith(("http://", "https://")):
                return
            headers = {}
            try:
                headers = response.headers or {}
            except Exception:
                headers = {}
            content_type = (headers.get("content-type") or "")
            content_type = content_type.split(";")[0].strip().lower()
            with self._lock:
                event = self._record(url)
                if event is None:
                    return
                try:
                    event.status = response.status
                except Exception:
                    pass
                if content_type:
                    event.content_type = content_type
                event.from_response = True
        except Exception:
            return

    def snapshot(self) -> list:
        """当前全部事件的快照（保持插入顺序）。"""
        with self._lock:
            return list(self._events.values())


class PlaywrightFetcher(BaseFetcher):
    """基于 Chromium 的渲染抓取器（自带网络监听）。"""

    name = "playwright"

    def __init__(self, *, headless: bool = True, locale: str = DEFAULT_LOCALE,
                 viewport: tuple = DEFAULT_VIEWPORT, page_timeout: float = 30.0,
                 wait_timeout: float = 15.0, max_events: int = 5000,
                 scroll_pause: float = 0.8, extra_headers: dict | None = None,
                 executable_path: str = "", auto_extract: bool = True,
                 progress=None, **kwargs):
        super().__init__(**kwargs)
        self.headless = bool(headless)
        self.locale = locale or DEFAULT_LOCALE
        self.viewport = tuple(viewport or DEFAULT_VIEWPORT)
        self.page_timeout = max(1.0, float(page_timeout or 30.0))
        self.wait_timeout = max(1.0, float(wait_timeout or 15.0))
        self.scroll_pause = max(0.1, float(scroll_pause or 0.5))
        self.extra_headers = dict(extra_headers or {})
        # 浏览器内核：留空则自动查找/解压本地离线包（见 chrome_setup）
        self.executable_path = executable_path
        self.auto_extract = bool(auto_extract)
        self.progress = progress
        self._recorder = NetworkRecorder(max_events)

    # ---------- 可用性 ----------
    @classmethod
    def available(cls, auto_extract: bool = True, progress=None) -> tuple:
        """探测 playwright 与浏览器内核是否就绪（供 UI 提示与降级判断）。"""
        return browser_pool.probe(auto_extract=auto_extract, progress=progress)

    @staticmethod
    def prepare(auto_extract: bool = True, progress=None) -> tuple:
        """预先准备浏览器内核（需要时解压离线包），返回 (路径, 失败原因)。"""
        return chrome_setup.ensure_executable(auto_extract=auto_extract,
                                              progress=progress)

    def close(self) -> None:
        """释放当前线程的浏览器（工作线程退出前必须调用）。"""
        browser_pool.release()

    # ---------- 抓取 ----------
    def _acquire(self):
        """获取浏览器句柄（UA 仅在显式配置时覆盖，否则用浏览器自身 UA）。"""
        return browser_pool.acquire(
            user_agent=self.user_agent, locale=self.locale, viewport=self.viewport,
            headless=self.headless, ignore_https_errors=not self.verify_ssl,
            proxy=self.proxy, executable_path=self.executable_path,
            auto_extract=self.auto_extract, progress=self.progress)

    def _goto_kwargs(self, timeout: float | None, referer: str) -> dict:
        kwargs = {"wait_until": "domcontentloaded",
                  "timeout": (timeout or self.page_timeout) * 1000}
        if referer:
            kwargs["referer"] = referer
        return kwargs

    def fetch(self, url: str, *, wait_selector: str = "", scroll: bool = False,
              max_scroll: int = 8, timeout: float | None = None,
              referer: str = "") -> FetchContext:
        """渲染页面并返回渲染后的 DOM + 本次页面的网络事件。"""
        ctx = FetchContext(url=url, fetcher=self.name)
        if self.stopped:
            ctx.error = "任务已停止"
            return ctx
        if not self.delay_before_request():
            ctx.error = "任务已停止"
            return ctx

        handle = self._acquire()
        page = None
        self._recorder.reset()
        try:
            page = handle.new_page()
            # 先挂监听再 goto，保证首屏请求（往往含关键图片/接口）被采集
            self._recorder.attach(page)
            if self.extra_headers:
                page.set_extra_http_headers(self.extra_headers)
            page.set_default_timeout(self.page_timeout * 1000)

            response = page.goto(url, **self._goto_kwargs(timeout, referer))
            ctx.final_url = page.url or url
            if response is not None:
                ctx.status = response.status
                try:
                    headers = response.headers or {}
                except Exception:
                    headers = {}
                ctx.content_type = (headers.get("content-type") or "")
                ctx.content_type = ctx.content_type.split(";")[0].strip().lower()

            # 页面本身即媒体直链（点击图片链接下载的场景）：无需渲染
            if classify_content_type(ctx.content_type) in MEDIA_TYPES:
                ctx.media_direct = True
                ctx.network_events = self._recorder.snapshot()
                return ctx

            if ctx.status >= 400:
                ctx.error = f"HTTP {ctx.status}"
                ctx.network_events = self._recorder.snapshot()
                return ctx

            if wait_selector:
                self._wait_selector(page, wait_selector, timeout)
            else:
                self._wait_idle(page, timeout)
            if scroll:
                self._auto_scroll(page, max_scroll, timeout)

            ctx.html = page.content()
            ctx.network_events = self._recorder.snapshot()
            return ctx
        except Exception as exc:
            ctx.error = "任务已停止" if self.stopped else \
                f"{type(exc).__name__}: {str(exc)[:160]}"
            ctx.network_events = self._recorder.snapshot()
            return ctx
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass

    def fetch_json(self, url: str, *, timeout: float | None = None,
                   referer: str = "") -> FetchContext:
        """通过浏览器上下文请求接口（复用页面 Cookie，应对需要登录态的接口）。"""
        ctx = FetchContext(url=url, fetcher=self.name)
        if self.stopped:
            ctx.error = "任务已停止"
            return ctx
        if not self.delay_before_request():
            ctx.error = "任务已停止"
            return ctx
        try:
            handle = self._acquire()
            headers = {"Accept": "application/json, text/plain, */*"}
            if referer:
                headers["Referer"] = referer
            headers.update(self.extra_headers)
            response = handle.context.request.get(
                url, headers=headers,
                timeout=(timeout or self.page_timeout) * 1000)
            ctx.status = response.status
            headers_out = {}
            try:
                headers_out = response.headers or {}
            except Exception:
                headers_out = {}
            ctx.content_type = (headers_out.get("content-type") or "")
            ctx.content_type = ctx.content_type.split(";")[0].strip().lower()
            ctx.final_url = getattr(response, "url", "") or url
            if ctx.status >= 400:
                ctx.error = f"HTTP {ctx.status}"
                return ctx
            try:
                ctx.json_data = response.json()
            except Exception:
                ctx.error = "响应不是合法 JSON"
            return ctx
        except Exception as exc:
            ctx.error = "任务已停止" if self.stopped else \
                f"{type(exc).__name__}: {str(exc)[:160]}"
            return ctx

    # ---------- 页面等待与滚动 ----------
    def _wait_selector(self, page, selector: str, timeout: float | None) -> bool:
        """等待指定选择器出现（找不到不视为抓取失败，仅由调用方决定是否告警）。"""
        budget = min(timeout or self.wait_timeout, self.wait_timeout)
        try:
            page.wait_for_selector(selector, timeout=budget * 1000)
            return True
        except Exception:
            return False

    def _wait_idle(self, page, timeout: float | None) -> None:
        """尽量等待网络空闲，兜底超时不影响主流程。"""
        budget = min((timeout or self.page_timeout), self.page_timeout)
        try:
            page.wait_for_load_state("networkidle", timeout=budget * 1000)
        except Exception:
            pass

    def _auto_scroll(self, page, max_scroll: int, timeout: float | None) -> int:
        """滚动到底触发懒加载/无限加载，返回实际滚动次数。

        终止条件：达到次数上限、页面高度不再增长（已触底）或收到停止信号。
        """
        limit = max(0, int(max_scroll or 0))
        height = 0
        for index in range(limit):
            if self.stopped:
                break
            try:
                height = int(page.evaluate(_HEIGHT_JS) or 0)
                new_height = int(page.evaluate(_SCROLL_JS) or 0)
            except Exception:
                break
            if index > 0 and new_height <= height:
                break  # 高度未变化，说明已触底
            if not interruptible_sleep(self.scroll_pause, self.stop_event):
                break
        return limit


__all__ = ["PlaywrightFetcher", "NetworkRecorder"]
