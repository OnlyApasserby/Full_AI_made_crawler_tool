"""静态抓取器：基于 requests 的快速路径。

用途与取舍
----------
- **快**：无浏览器开销，单页通常 100ms 级，适合绝大多数服务端渲染页面
- **无 JS**：拿不到懒加载/滚动/接口渲染出来的内容，由 ``PlaywrightFetcher`` 补齐

实现要点：
- 线程局部 ``Session`` 复用连接池（与现有下载器一致的做法）
- 流式读取 + 单页字节上限 + 停止标志检查（可及时中断）
- 媒体直链（Content-Type 为图片/音视频）**不回读响应体**，只回报类型，
  由调用方登记为媒体资源，避免把大文件读进内存
- 编码推断使用 :func:`guess_encoding`，规避 requests 对 ``text/*`` 的
  ISO-8859-1 默认值导致的中文乱码
"""

from __future__ import annotations

import json as json_lib
import re
import threading

import requests

from core.fetcher.base import (
    BaseFetcher, FetchContext, FetchError, decode_bytes, guess_encoding,
    interruptible_sleep,
)
from core.media.detector import MEDIA_TYPES, classify_content_type
from core.media.probe import DEFAULT_PREFIX_BYTES

_CHARSET_RE = re.compile(r"charset\s*=\s*['\"]?([\w\-]+)", re.IGNORECASE)

#: 静态请求的默认 Accept（尽量贴近浏览器，降低被判定为爬虫的概率）
_HTML_ACCEPT = ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8")
_JSON_ACCEPT = "application/json, text/plain, */*"


class RequestsFetcher(BaseFetcher):
    """基于 requests 的静态抓取器。"""

    name = "requests"

    def __init__(self, *, connect_timeout: float = 10.0, read_timeout: float = 30.0,
                 max_bytes: int = 0, max_retries: int = 1, retry_backoff: float = 1.0,
                 extra_headers: dict | None = None, skip_media_body: bool = True,
                 **kwargs):
        super().__init__(**kwargs)
        # connect / read 分离超时：慢站点不至于卡住整体流程
        self.timeout = (max(1.0, float(connect_timeout)), max(1.0, float(read_timeout)))
        self.max_bytes = max(0, int(max_bytes or 0))      # 单页字节上限（0=不限）
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff = max(0.1, float(retry_backoff or 1.0))
        self.extra_headers = dict(extra_headers or {})
        self.skip_media_body = bool(skip_media_body)
        self._local = threading.local()

    # ---------- 连接池 ----------
    def _session(self) -> requests.Session:
        """返回当前线程的 Session（复用连接，避免每次请求重新握手）。"""
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            adapter = requests.adapters.HTTPAdapter(pool_connections=8,
                                                   pool_maxsize=8, max_retries=0)
            session.mount("http://", adapter)
            session.mount("https://", adapter)
            self._local.session = session
        return session

    def _headers(self, referer: str = "", json_mode: bool = False) -> dict:
        headers = {
            "User-Agent": self.ua(),
            "Accept": _JSON_ACCEPT if json_mode else _HTML_ACCEPT,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }
        if referer:
            headers["Referer"] = referer
        if self.extra_headers:
            headers.update({k: v for k, v in self.extra_headers.items() if v})
        return headers

    def close(self) -> None:
        session = getattr(self._local, "session", None)
        if session is not None:
            try:
                session.close()
            finally:
                self._local.session = None

    # ---------- 抓取主流程 ----------
    def fetch(self, url: str, *, wait_selector: str = "", scroll: bool = False,
              max_scroll: int = 8, timeout: float | None = None,
              referer: str = "") -> FetchContext:
        """抓取页面 HTML（静态请求；``wait_selector`` / ``scroll`` 参数在此忽略）。"""
        return self._fetch_with_retry(url, json_mode=False, timeout=timeout,
                                      referer=referer)

    def fetch_json(self, url: str, *, timeout: float | None = None,
                   referer: str = "") -> FetchContext:
        """抓取 JSON 接口（翻页接口场景）。"""
        return self._fetch_with_retry(url, json_mode=True, timeout=timeout,
                                      referer=referer)

    def _fetch_with_retry(self, url: str, *, json_mode: bool,
                          timeout: float | None, referer: str) -> FetchContext:
        last_error = ""
        for attempt in range(self.max_retries + 1):
            if self.stopped:
                return FetchContext(url=url, fetcher=self.name, error="任务已停止")
            if not self.delay_before_request():
                return FetchContext(url=url, fetcher=self.name, error="任务已停止")
            try:
                ctx = self._request(url, json_mode=json_mode, timeout=timeout,
                                    referer=referer)
            except FetchError as exc:
                last_error = str(exc)
                ctx = None
            if ctx is not None and not self._retryable(ctx):
                return ctx
            if ctx is not None:
                last_error = ctx.error or f"HTTP {ctx.status}"
            if attempt < self.max_retries and not self.stopped:
                interruptible_sleep(self.retry_backoff * (2 ** attempt), self.stop_event)
                continue
            break
        return FetchContext(url=url, fetcher=self.name, error=last_error or "抓取失败")

    def _retryable(self, ctx: FetchContext) -> bool:
        """是否值得重试：服务端错误或传输层错误；4xx 视为确定性失败不重试。"""
        if not ctx.error:
            return False
        return ctx.status == 0 or ctx.status >= 500

    def _request(self, url: str, *, json_mode: bool, timeout: float | None,
                 referer: str) -> FetchContext:
        """执行一次请求并读取响应体。

        :raises FetchError: 连接/传输层失败（由调用方决定是否重试）
        """
        session = self._session()
        try:
            resp = session.get(url, headers=self._headers(referer, json_mode),
                               proxies=self.proxy or None,
                               timeout=timeout or self.timeout,
                               stream=True, allow_redirects=True,
                               verify=self.verify_ssl)
        except requests.RequestException as exc:
            raise FetchError(f"{type(exc).__name__}: {str(exc)[:120]}") from exc

        ctx = FetchContext(url=url, final_url=resp.url or url,
                           status=resp.status_code, fetcher=self.name)
        try:
            raw_ct = resp.headers.get("Content-Type") or ""
            ctx.content_type = raw_ct.split(";")[0].strip().lower()
            if ctx.status >= 400:
                ctx.error = f"HTTP {ctx.status}"
                return ctx
            # 直链媒体：不读响应体，交由调用方登记为媒体资源
            if (self.skip_media_body and not json_mode
                    and classify_content_type(ctx.content_type) in MEDIA_TYPES):
                ctx.media_direct = True
                return ctx
            return self._read_body(resp, ctx, json_mode=json_mode, raw_ct=raw_ct)
        finally:
            try:
                resp.close()
            except Exception:
                pass

    def _read_body(self, resp, ctx: FetchContext, *, json_mode: bool,
                   raw_ct: str) -> FetchContext:
        """流式读取响应体（受单页上限与停止标志约束）并解码。"""
        chunks: list[bytes] = []
        total = 0
        truncated = False
        try:
            for chunk in resp.iter_content(chunk_size=8192):
                if self.stopped:
                    ctx.error = "任务已停止"
                    return ctx
                if not chunk:
                    continue
                chunks.append(chunk)
                total += len(chunk)
                if self.max_bytes and total >= self.max_bytes:
                    truncated = True
                    break
        except requests.RequestException as exc:
            raise FetchError(f"传输中断 {type(exc).__name__}: {str(exc)[:100]}") from exc

        data = b"".join(chunks)
        if json_mode:
            text = decode_bytes(data, self._declared_encoding(raw_ct) or "utf-8")
            try:
                ctx.json_data = json_lib.loads(text)
            except (ValueError, TypeError):
                ctx.error = "响应不是合法 JSON"
            return ctx

        encoding = guess_encoding(data, self._declared_encoding(raw_ct))
        ctx.html = decode_bytes(data, encoding)
        if truncated:
            ctx.html += "<!-- 已达单页字节上限，内容被截断 -->"
        return ctx

    @staticmethod
    def _declared_encoding(content_type: str) -> str:
        """从 Content-Type 提取 charset 声明（未声明返回空串）。"""
        match = _CHARSET_RE.search(content_type or "")
        return match.group(1) if match else ""

    # ---------- 头部探测（图片尺寸/体积过滤用） ----------
    def fetch_prefix(self, url: str, *, size: int = DEFAULT_PREFIX_BYTES,
                     headers: dict | None = None, referer: str = "") -> tuple:
        """只读取响应前 N 字节。

        用于"下载前判定图片尺寸"，避免为了过滤一张缩略图而下载整张原图。

        :return: (字节内容, 错误说明)；成功时错误说明为空串
        """
        if self.stopped:
            return b"", "任务已停止"
        budget = max(1, int(size or DEFAULT_PREFIX_BYTES))
        request_headers = self._headers(referer)
        # 二进制语义：Range 与实际字节数必须一致
        request_headers["Accept-Encoding"] = "identity"
        request_headers["Range"] = f"bytes=0-{budget - 1}"
        if headers:
            request_headers.update({k: v for k, v in headers.items() if v})
        session = self._session()
        try:
            resp = session.get(url, headers=request_headers,
                               proxies=self.proxy or None, timeout=self.timeout,
                               stream=True, allow_redirects=True,
                               verify=self.verify_ssl)
        except requests.RequestException as exc:
            return b"", f"{type(exc).__name__}: {str(exc)[:100]}"
        try:
            if resp.status_code >= 400:
                return b"", f"HTTP {resp.status_code}"
            data = bytearray()
            for chunk in resp.iter_content(chunk_size=16384):
                if not chunk:
                    continue
                data.extend(chunk)
                if len(data) >= budget:
                    break
            return bytes(data), ""
        except requests.RequestException as exc:
            return b"", f"传输中断 {type(exc).__name__}"
        finally:
            try:
                resp.close()
            except Exception:
                pass


__all__ = ["RequestsFetcher"]
