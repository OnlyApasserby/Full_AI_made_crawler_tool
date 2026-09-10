"""网络事件提取器：把浏览器监听到的请求流翻译成媒体资源。

这是"动态 XHR 监听"的落地环节：浏览器自己发出的请求携带正确的 Cookie / Referer /
签名参数，因此这里拿到的地址是**真实可用**的（静态 HTML 里往往根本不存在）。

几个关键取舍
------------
- **只在拿到响应（status < 400）时采纳**，被 404/403 拒绝的地址不下载
- **转发请求头**（Referer / Cookie / UA），直接复用浏览器上下文，绕过常见防盗链
- **过滤埋点与图标噪声**：广告联盟域名（复用 ``core.security.AD_DOMAINS``）、
  统计打点（beacon / 1x1 / sprite）不进入队列
- **``resource_type`` 仅作辅助**：真正的类型判定仍以 Content-Type / 扩展名为准，
  避免把 ``media`` 类型里混入的封面图当视频
"""

from __future__ import annotations

import re
from typing import Iterator
from urllib.parse import urlparse

from core.extractor.base import BaseExtractor
from core.media.detector import (
    MEDIA_TYPES, TYPE_IMAGE, TYPE_VIDEO, classify_content_type, has_media_ext,
    kind_of,
)
from core.media.models import MediaItem
from core.security import AD_DOMAINS

#: 明显不是资源文件的请求类型，直接跳过
_SKIP_RESOURCE_TYPES = {"script", "stylesheet", "font", "document", "manifest",
                        "eventsource", "websocket", "texttrack", "preflight"}

#: 噪声地址特征（打点、图标、占位图）——真正的质量过滤在下游 pipeline 按尺寸判定
_NOISE_RE = re.compile(
    r"(?:favicon|sprite|1x1|spacer|blank\.(?:gif|png)|pixel\.(?:gif|png)|"
    r"beacon|/track(?:ing)?/|hm\.gif|stat\.gif|analytics)",
    re.IGNORECASE)


def _domain_hit(host: str) -> bool:
    """域名（含子域）是否属于已知广告/追踪网络。"""
    host = (host or "").lower()
    if not host:
        return False
    return any(host == domain or host.endswith("." + domain)
               for domain in AD_DOMAINS)


class NetworkExtractor(BaseExtractor):
    """网络事件提取器（XHR / 图片 / 媒体请求）。"""

    name = "network"
    priority = 60

    def can_handle(self, url: str, ctx) -> float:
        return 0.8 if getattr(ctx, "network_events", None) else 0.0

    def extract(self, ctx) -> Iterator[MediaItem]:
        seen: set[str] = set()
        index = 0
        for event in ctx.network_events or []:
            item = self._to_item(event, ctx)
            if item is None or item.url in seen:
                continue
            seen.add(item.url)
            index += 1
            item.index = index
            yield item

    # ---------- 单个事件 → 媒体项 ----------
    def _to_item(self, event, ctx) -> MediaItem | None:
        url = (event.url or "").strip()
        if not url.startswith(("http://", "https://")):
            return None
        if event.status and event.status >= 400:
            return None
        if _NOISE_RE.search(url):
            return None
        try:
            host = (urlparse(url).hostname or "")
        except ValueError:
            return None
        if _domain_hit(host):
            return None

        kind = self._kind_of_event(event, url)
        if kind not in MEDIA_TYPES:
            return None
        return MediaItem(
            url=url, kind=kind, source_page=ctx.base_url,
            headers=event.forward_headers(ctx.base_url),
            origin="network",
            extra={"resource_type": event.resource_type, "status": event.status},
        )

    @staticmethod
    def _kind_of_event(event, url: str) -> str:
        """综合 Content-Type、URL 扩展名与浏览器资源类型判定资源类型。"""
        kind = kind_of(url, event.content_type)
        if kind in MEDIA_TYPES:
            return kind
        resource_type = (event.resource_type or "").lower()
        if resource_type == "image":
            return TYPE_IMAGE
        if resource_type == "media":
            by_ct = classify_content_type(event.content_type)
            return by_ct if by_ct in MEDIA_TYPES else TYPE_VIDEO
        if resource_type in _SKIP_RESOURCE_TYPES:
            return ""
        # xhr / fetch / other：无扩展名的接口式直链，必须带媒体特征才采纳
        if not has_media_ext(url):
            return ""
        by_ct = classify_content_type(event.content_type)
        return by_ct if by_ct in MEDIA_TYPES else kind_of(url, "")


__all__ = ["NetworkExtractor"]
