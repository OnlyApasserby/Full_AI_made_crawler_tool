"""抓取层公共契约：抓取上下文、网络事件与抓取器基类。

设计目标（把原先散落在 crawler / downloader 里的请求逻辑收敛为一份）：
- 统一 UA 池、代理、校验开关、请求间隔抖动、停止标志语义
- 静态与动态（浏览器渲染）两种实现暴露**同一个** ``fetch()`` 接口，
  上层（提取器 / 编排线程）不需要关心底层是 requests 还是 playwright

本模块只依赖标准库（playwright/requests 由子类各自引入）。
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field

from core.media.detector import MEDIA_TYPES, kind_of
from utils.user_agents import UserAgentPool

#: 渲染 / 抓取模式
MODE_STATIC = "static"
MODE_DYNAMIC = "dynamic"
MODE_AUTO = "auto"

#: 浏览器与静态请求共用的伪装参数
DEFAULT_LOCALE = "zh-CN"
DEFAULT_VIEWPORT = (1440, 900)
#: 请求头中允许透传给下载阶段的字段（防盗链与鉴权相关）
FORWARD_HEADER_KEYS = ("referer", "cookie", "origin", "authorization",
                       "user-agent", "accept-language", "x-requested-with")


class FetchError(RuntimeError):
    """抓取失败（网络错误、超时、HTTP 错误码等）。"""


class FetcherUnavailable(RuntimeError):
    """抓取器不可用（依赖缺失或浏览器未安装），属于**环境问题**而非抓取失败。

    调用方应据此降级（如从动态渲染退回静态请求）而非记为任务失败。
    """


@dataclass
class NetworkEvent:
    """一次网络请求的事件快照（浏览器渲染模式下由 NetworkRecorder 采集）。

    这是"动态 XHR 监听"的数据单元：只保存**元数据**（URL / 类型 / 请求头），
    不读取响应体，避免大文件进内存。
    """

    url: str
    method: str = "GET"
    resource_type: str = ""      # playwright: document/image/media/xhr/fetch/...
    status: int = 0
    content_type: str = ""
    request_headers: dict = field(default_factory=dict)
    from_response: bool = False  # 是否已获得响应（未获得可能是被拦截/中止）

    def forward_headers(self, referer: str = "") -> dict:
        """抽取可复用于下载的请求头（防盗链/鉴权相关字段）。"""
        out: dict = {}
        for key, value in (self.request_headers or {}).items():
            if value and key.lower() in FORWARD_HEADER_KEYS:
                out[key] = value
        if referer and not any(k.lower() == "referer" for k in out):
            out["Referer"] = referer
        return out

    def to_dict(self) -> dict:
        return {
            "url": self.url, "method": self.method,
            "resource_type": self.resource_type, "status": self.status,
            "content_type": self.content_type,
            "request_headers": dict(self.request_headers),
            "from_response": self.from_response,
        }


@dataclass
class FetchContext:
    """单次抓取的结果：页面内容 + 网络事件 + JSON 数据。

    ``network_events`` 在静态模式下恒为空列表，提取器需据此判断来源；
    ``media_direct`` 表示"请求的 URL 本身就是媒体文件"（媒体直链），
    此时不读取响应体，由调用方直接登记为媒体资源，避免大文件进内存。
    """

    url: str
    final_url: str = ""
    status: int = 0
    content_type: str = ""
    html: str = ""
    json_data: object = None
    network_events: list = field(default_factory=list)
    fetcher: str = ""
    error: str = ""
    media_direct: bool = False

    @property
    def ok(self) -> bool:
        """本次抓取是否成功产出内容（直链媒体不算内容产出）。"""
        if self.error:
            return False
        if self.media_direct:
            return False
        return bool(self.html) or self.json_data is not None

    @property
    def base_url(self) -> str:
        """用于相对链接拼接的基准 URL（优先重定向后的地址）。"""
        return self.final_url or self.url

    @property
    def kind_hint(self) -> str:
        """按 Content-Type / URL 推断的资源类型（供"直链媒体"登记使用）。"""
        return kind_of(self.url, self.content_type)

    @property
    def is_media_response(self) -> bool:
        """响应内容是否为媒体（图片/视频/音频/流媒体）。"""
        return self.kind_hint in MEDIA_TYPES

    def events_since(self, mark: int) -> list:
        """取 ``mark`` 之后的增量网络事件（翻页/滚动场景避免重复提取）。"""
        return list(self.network_events[max(0, int(mark)):])

    def mark(self) -> int:
        """记录当前网络事件数量，配合 :meth:`events_since` 做增量提取。"""
        return len(self.network_events)


def decode_bytes(data: bytes, encoding: str = "") -> str:
    """解码响应体：优先给定编码，未知时按 UTF-8 容错替换。"""
    enc = (encoding or "utf-8") or "utf-8"
    try:
        return data.decode(enc, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return data.decode("utf-8", errors="replace")


def guess_encoding(data: bytes, declared: str = "") -> str:
    """推断响应体编码，解决"响应头未声明 charset"导致的中文乱码。

    顺序：声明值 → charset_normalizer 探测 → UTF-8/GB18030/Big5 试解 → latin-1 兜底。
    注意不要依赖 ``requests.Response.encoding``：对 ``text/*`` 它会给 HTTP 默认的
    ISO-8859-1，会把中文页面解成乱码。
    """
    if declared:
        try:
            "".encode(declared)
            return declared
        except LookupError:
            pass
    sample = data[:200000]
    try:  # requests 的依赖，通常已安装；缺失时静默跳过
        from charset_normalizer import from_bytes
        best = from_bytes(sample).best()
        if best is not None and best.encoding:
            return best.encoding
    except Exception:
        pass
    for enc in ("utf-8", "gb18030", "big5"):
        try:
            sample.decode(enc)
            return enc
        except (UnicodeDecodeError, LookupError):
            continue
    return "latin-1"


def interruptible_sleep(seconds: float, stop_event=None, step: float = 0.1) -> bool:
    """分段睡眠，期间可被停止标志打断。

    :return: True 表示睡满；False 表示被停止标志打断
    """
    remaining = float(seconds or 0.0)
    if remaining <= 0:
        return not (stop_event is not None and stop_event.is_set())
    while remaining > 0:
        if stop_event is not None and stop_event.is_set():
            return False
        chunk = min(step, remaining)
        time.sleep(chunk)
        remaining -= chunk
    return True


class BaseFetcher:
    """抓取器基类：提供 UA / 代理 / 抖动 / 停止标志等公共能力。

    子类需实现 :meth:`fetch`、:meth:`fetch_json`、:meth:`close` 与 :meth:`available`。
    """

    name = "base"

    def __init__(self, *, ua_pool: UserAgentPool | None = None, proxy: dict | None = None,
                 verify_ssl: bool = True, request_delay: float = 0.0,
                 jitter: float = 0.3, user_agent: str = "",
                 stop_event: threading.Event | None = None):
        self.ua_pool = ua_pool or UserAgentPool()
        self.proxy = proxy or {}
        self.verify_ssl = verify_ssl
        self.request_delay = max(0.0, float(request_delay or 0.0))
        self.jitter = max(0.0, float(jitter or 0.0))
        self.user_agent = user_agent
        self.stop_event = stop_event
        self._first_request = True

    # ---------- 公共能力 ----------
    @classmethod
    def available(cls) -> tuple[bool, str]:
        """本抓取器是否可用；不可用时返回可读原因（供 UI 降级提示）。"""
        return True, ""

    def ua(self) -> str:
        """当前请求使用的 UA：显式指定优先，否则从池中随机取。"""
        return self.user_agent or self.ua_pool.random()

    @property
    def stopped(self) -> bool:
        """外部是否已请求停止。"""
        return self.stop_event is not None and self.stop_event.is_set()

    def delay_before_request(self) -> bool:
        """按配置间隔 + 随机抖动等待（首个请求不等待）。

        :return: False 表示等待期间收到停止信号
        """
        if self._first_request:
            self._first_request = False
            return not self.stopped
        delay = self.request_delay * (1 + random.uniform(0, self.jitter))
        return interruptible_sleep(delay, self.stop_event)

    def fetch(self, url: str, *, wait_selector: str = "", scroll: bool = False,
              max_scroll: int = 8, timeout: float | None = None,
              referer: str = "") -> FetchContext:
        """抓取单个页面/接口，返回 :class:`FetchContext`。"""
        raise NotImplementedError

    def fetch_json(self, url: str, *, timeout: float | None = None,
                   referer: str = "") -> FetchContext:
        """抓取 JSON 接口（翻页接口场景）。"""
        raise NotImplementedError

    def close(self) -> None:
        """释放资源（连接池 / 浏览器），可重复调用。"""

    # ---------- 上下文管理 ----------
    def __enter__(self) -> "BaseFetcher":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


__all__ = [
    "MODE_STATIC", "MODE_DYNAMIC", "MODE_AUTO", "DEFAULT_LOCALE",
    "DEFAULT_VIEWPORT", "FORWARD_HEADER_KEYS",
    "FetchError", "FetcherUnavailable", "NetworkEvent", "FetchContext",
    "BaseFetcher", "decode_bytes", "guess_encoding", "interruptible_sleep",
]
