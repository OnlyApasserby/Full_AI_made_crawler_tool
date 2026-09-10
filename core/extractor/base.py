"""提取器基类：把"抓取结果"翻译为"媒体资源列表"的统一契约。

插件化设计
----------
每个提取器只需回答两个问题：
1. :meth:`BaseExtractor.can_handle` —— 这条抓取结果我能否处理？置信度多少？
2. :meth:`BaseExtractor.extract` —— 从这里能提取出哪些媒体？

``priority`` 决定顺序（越小越先），同一 URL 被多个提取器命中时由
``registry.extract_all`` 合并（先命中者提供请求头，后命中者补齐合集/标题）。
"""

from __future__ import annotations

from typing import Iterator
from urllib.parse import urljoin

from core.media.detector import TYPE_UNKNOWN, kind_of
from core.media.models import MediaConfig, MediaItem

#: 不可下载的伪协议前缀
_SKIPPED_PREFIXES = ("data:", "blob:", "javascript:", "about:", "mailto:",
                     "tel:", "file:", "#")


class BaseExtractor:
    """媒体提取器基类。"""

    #: 提取器标识（出现在 MediaItem.origin 中，便于排查漏抓）
    name = "base"
    #: 优先级：数值越小越先执行（站点适配器 10 < 规则 50 < 网络 60 < HTML 80 < JSON 90）
    priority = 100

    def __init__(self, config: MediaConfig | None = None):
        self.config = config or MediaConfig()

    # ---------- 子类实现 ----------
    def can_handle(self, url: str, ctx) -> float:
        """能否处理本次抓取结果；返回 0~1 置信度，0 表示不处理。"""
        return 0.0

    def extract(self, ctx) -> Iterator[MediaItem]:
        """从抓取结果中产出媒体项（生成器，可增量消费）。"""
        return iter(())

    # ---------- 公共工具 ----------
    @staticmethod
    def should_skip(raw: str) -> bool:
        """URL 是否属于不可下载的伪协议或空值。"""
        text = (raw or "").strip()
        if not text:
            return True
        return text.lower().startswith(_SKIPPED_PREFIXES)

    @staticmethod
    def absolute(raw: str, base_url: str) -> str:
        """相对地址转绝对地址；非 http(s) 返回空串。"""
        text = (raw or "").strip()
        if not text or BaseExtractor.should_skip(text):
            return ""
        try:
            full = urljoin(base_url, text)
        except ValueError:
            return ""
        if full.startswith(("http://", "https://")):
            return full
        # 协议相对地址（//cdn.example.com/a.jpg）
        if text.startswith("//"):
            try:
                joined = urljoin(base_url, "https:" + text)
            except ValueError:
                return ""
            return joined if joined.startswith(("http://", "https://")) else ""
        return ""

    def make_item(self, url: str, ctx, *, kind: str = "", title: str = "",
                  album: str = "", index: int = 0, headers: dict | None = None,
                  origin: str = "", extra: dict | None = None) -> MediaItem | None:
        """构造 :class:`MediaItem`（自动补全来源页、类型与来源标识）。

        :return: 无法归一化为绝对地址时返回 None
        """
        absolute = self.absolute(url, ctx.base_url if ctx is not None else "")
        if not absolute:
            return None
        return MediaItem(
            url=absolute,
            kind=kind or kind_of(absolute, "") or TYPE_UNKNOWN,
            source_page=(ctx.base_url if ctx is not None else ""),
            album=album, title=title, index=index,
            headers=dict(headers or {}),
            origin=origin or self.name,
            extra=dict(extra or {}),
        )


__all__ = ["BaseExtractor"]
