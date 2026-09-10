"""通用 JSON 提取器：从接口响应（XHR 翻页/列表接口）中提取媒体地址。

典型场景：图片站滚动加载时返回 ``{"data":{"list":[{"thumb":"...","original":"..."}]}}``，
真实原图地址只存在于 JSON 里，静态 HTML 完全没有。

判定策略（宁可少抓也不误抓）
----------------------------
1. 值本身带媒体扩展名（``.jpg`` / ``.m3u8`` …）→ 直接采纳
2. 值没有扩展名时，仅当**键名本身足够具体**（``original`` / ``playUrl`` /
   ``download_url`` / ``m3u8`` …）才采纳；``url`` / ``link`` / ``path`` 这类
   泛化键名一律拒绝，避免把接口地址当媒体下载
3. 同一对象内的 ``title`` / ``name`` / ``alt`` 等字段复用为该对象的标题
"""

from __future__ import annotations

from typing import Iterator

from core.extractor.base import BaseExtractor
from core.media.detector import MEDIA_TYPES, kind_of

#: 无扩展名也允许采纳的键名（足够具体，误判率低）
_EXTLESS_OK_KEYS = {
    "original", "origin", "originalurl", "original_url", "raw", "rawurl",
    "hd", "hdurl", "large", "big", "full", "fullurl",
    "playurl", "play_url", "videourl", "video_url", "audiourl", "audio_url",
    "downloadurl", "download_url", "fileurl", "file_url",
    "m3u8", "hls", "mp4", "mpd", "dash",
}
#: 用作标题的键名
_TITLE_KEYS = ("title", "name", "alt", "caption", "desc", "description", "text")
#: 遍历上限：防止异常大的接口响应拖慢流程
_MAX_DEPTH = 6
_MAX_NODES = 20000


class GenericJSONExtractor(BaseExtractor):
    """通用 JSON 提取器（递归遍历，键名 + 扩展名双判定）。"""

    name = "json"
    priority = 90

    def can_handle(self, url: str, ctx) -> float:
        return 0.7 if getattr(ctx, "json_data", None) is not None else 0.0

    def extract(self, ctx) -> Iterator:
        """遍历 JSON 结构并产出媒体项。"""
        base = (ctx.base_url if ctx is not None else "") or ctx.url
        seen: set[str] = set()
        index = 0
        nodes = [0]
        for raw, title, hinted in self._walk(ctx.json_data, base, "", 0, nodes):
            item = self.make_item(
                raw, ctx, title=title, index=index,
                origin="json:hint" if hinted else "json",
                extra={"key_hint": hinted} if hinted else None)
            if item is None or item.url in seen:
                continue
            seen.add(item.url)
            if item.kind not in MEDIA_TYPES:
                if not hinted:
                    continue
                item.kind = "video" if hinted in ("m3u8", "mp4", "mpd", "dash", "hls") \
                    else "image"
            index += 1
            yield item

    # ---------- 递归遍历 ----------
    def _walk(self, node, base: str, title: str, depth: int,
              nodes: list) -> Iterator:
        """深度优先遍历，产出 (地址, 标题, 键名提示)。"""
        nodes[0] += 1
        if depth > _MAX_DEPTH or nodes[0] > _MAX_NODES:
            return
        if isinstance(node, dict):
            local_title = title
            for key in _TITLE_KEYS:
                value = node.get(key)
                if isinstance(value, str) and value.strip():
                    local_title = value.strip()[:60]
                    break
            for key, value in node.items():
                key_lower = str(key).lower()
                if isinstance(value, str):
                    candidate = self._candidate(value, base, key_lower)
                    if candidate:
                        yield candidate[0], local_title, candidate[1]
                elif isinstance(value, (dict, list)):
                    yield from self._walk(value, base, local_title, depth + 1, nodes)
        elif isinstance(node, list):
            for entry in node:
                if isinstance(entry, str):
                    candidate = self._candidate(entry, base, "")
                    if candidate:
                        yield candidate[0], title, candidate[1]
                elif isinstance(entry, (dict, list)):
                    yield from self._walk(entry, base, title, depth + 1, nodes)

    def _candidate(self, value: str, base: str, key_lower: str) -> tuple | None:
        """判定字符串值是否为媒体地址；返回 (绝对地址, 键名提示) 或 None。"""
        text = (value or "").strip()
        if not text or len(text) > 2000:
            return None
        if not text.startswith(("http://", "https://", "//", "/", "./", "../")):
            return None
        absolute = self.absolute(text, base)
        if not absolute:
            return None
        if absolute == base:  # 指向页面自身，无意义
            return None
        if kind_of(absolute, "") in MEDIA_TYPES:
            return absolute, ""
        if key_lower in _EXTLESS_OK_KEYS:
            return absolute, key_lower
        return None


__all__ = ["GenericJSONExtractor"]
