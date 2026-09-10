"""通用 HTML 提取器：从静态/渲染后的 HTML 中提取媒体资源。

相比 ``core.parser.extract_media_links``（保留给原有递归爬取流程复用），本提取器
补齐了通用图片站真正需要的几件事：

1. **每个标签只取最高质量的那个地址**：同一 ``<img>`` 上常有 ``src``(缩略图)、
   ``data-original``(原图)、``data-src``(懒加载) 等多个属性，按优先级挑原图，
   避免把缩略图也下载一遍
2. **``srcset`` 取最大候选**：按 ``640w`` / ``2x`` 描述符排序取最清晰的一张
3. **CSS 背景图**：``style="background-image:url(...)"`` 与 ``<style>`` 块中的
   ``url(...)``（瀑布流站点大量使用）
4. **``og:image`` / ``og:video`` / ``twitter:image``**：文章主图往往只在这里出现
5. **JS/JSON 内嵌地址**：脚本变量里的图片与视频直链（含相对路径形式）
6. **缩略图 URL 猜解原图**：命中预设命名规律时额外产出一个原图候选
7. **页面标题作为合集名**：便于按图集/文章分目录归档

本模块只依赖标准库（正则实现），不引入解析器依赖。
"""

from __future__ import annotations

import html as html_lib
import re
from typing import Iterator

from core.extractor.base import BaseExtractor
from core.media.detector import (
    MEDIA_TYPES, TYPE_IMAGE, TYPE_UNKNOWN, ext_of, has_media_ext, kind_of,
)
from core.media.models import MediaItem, upgrade_thumbnail_url

# ---------------------------------------------------------------------------
# 正则与属性优先级
# ---------------------------------------------------------------------------
_ATTR_RE = re.compile(r"""([\w\-:.]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_OG_TITLE_RE = re.compile(
    r"""<meta\b[^>]*\b(?:property|name)\s*=\s*["'](?:og:title|twitter:title)["'][^>]*>""",
    re.IGNORECASE)
_OG_IMAGE_RE = re.compile(
    r"""<meta\b[^>]*\b(?:property|name)\s*=\s*["']"""
    r"""(?:og:image(?::url|:secure_url)?|twitter:image(?::src)?)["'][^>]*>""",
    re.IGNORECASE)
_OG_VIDEO_RE = re.compile(
    r"""<meta\b[^>]*\b(?:property|name)\s*=\s*["']"""
    r"""(?:og:video(?::url|:secure_url)?|twitter:player:stream)["'][^>]*>""",
    re.IGNORECASE)
_CONTENT_RE = re.compile(r"""content\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.IGNORECASE)
_CSS_URL_RE = re.compile(r"""url\(\s*(?:"([^"]*)"|'([^']*)'|([^)'"]+))\s*\)""",
                         re.IGNORECASE)
_STYLE_BLOCK_RE = re.compile(r"<style[^>]*>(.*?)</style>",
                             re.IGNORECASE | re.DOTALL)
_A_HREF_RE = re.compile(r"""<a\b[^>]*\bhref\s*=\s*(?:"([^"]*)"|'([^']*)')""",
                        re.IGNORECASE)

#: 媒体标签的地址属性优先级（越靠前越可能是原图/主资源）
_SRC_ATTR_PRIORITY = (
    "data-original", "data-origin", "data-actualsrc", "data-actual-src",
    "data-large", "data-big", "data-zoom-image", "data-hd", "data-raw",
    "data-full", "data-real-src", "data-original-src",
    "data-src", "data-lazy-src", "data-lazyload", "data-defer-src",
    "data-echo", "data-url", "data-img", "data-image",
    "data-video", "data-video-src", "data-mp4", "data-source", "data-file",
    "src", "_src", "data-thumb", "data-thumbnail",
)
#: 明确指向"原图/大图"的属性：即使同时存在 srcset 也要一并保留
_STRONG_ATTRS = frozenset((
    "data-original", "data-origin", "data-actualsrc", "data-actual-src",
    "data-large", "data-big", "data-zoom-image", "data-hd", "data-raw",
    "data-full", "data-real-src", "data-original-src",
))
_SRCSET_ATTRS = ("srcset", "data-srcset", "data-lazy-srcset")
_MEDIA_TAGS = ("img", "video", "audio", "source", "embed")
_SCRIPT_BLOCK_RE = re.compile(r"<script\b[^>]*>(.*?)</script>",
                              re.IGNORECASE | re.DOTALL)

#: JS / JSON 内嵌的绝对媒体地址
_JS_ABS_RE = re.compile(
    r"""["']((?:https?:)?//[^"'\s<>\\]+?\.(?:jpg|jpeg|png|gif|webp|bmp|avif|heic|"""
    r"""mp4|m4v|mov|webm|mkv|flv|ts|m3u8|mpd|mp3|m4a|aac|ogg|flac|wav)"""
    r"""(?:\?[^"'\s<>\\]*)?)["']""", re.IGNORECASE)
#: JS / JSON 内嵌的相对媒体地址（以 / 开头）
_JS_REL_RE = re.compile(
    r"""["'](/[^"'\s<>\\]+?\.(?:jpg|jpeg|png|gif|webp|bmp|avif|heic|"""
    r"""mp4|m4v|mov|webm|mkv|flv|ts|m3u8|mpd|mp3|m4a|aac|ogg|flac|wav)"""
    r"""(?:\?[^"'\s<>\\]*)?)["']""", re.IGNORECASE)


def _parse_attrs(tag_text: str) -> dict:
    """解析标签内的属性（键统一小写）。"""
    attrs: dict = {}
    for match in _ATTR_RE.finditer(tag_text):
        key = match.group(1).lower()
        value = match.group(2) or match.group(3) or match.group(4) or ""
        if key not in attrs:  # 同名属性只取第一个
            attrs[key] = value
    return attrs


def _srcset_candidates(value: str) -> list:
    """解析 ``srcset``：返回 [(url, 清晰度分数)]，分数越大越清晰。"""
    candidates: list = []
    for part in (value or "").split(","):
        piece = part.strip()
        if not piece:
            continue
        bits = piece.split()
        url = bits[0]
        score = 0.0
        if len(bits) > 1:
            descriptor = bits[1].lower()
            try:
                if descriptor.endswith("w"):
                    score = float(descriptor[:-1])
                elif descriptor.endswith("x"):
                    score = float(descriptor[:-1]) * 1000
            except ValueError:
                score = 0.0
        candidates.append((url, score))
    return candidates


def _best_srcset(value: str) -> str:
    """从 srcset 中取最清晰的一张（无描述符时取最后一个，约定俗成是最大图）。"""
    candidates = _srcset_candidates(value)
    if not candidates:
        return ""
    if all(score <= 0 for _url, score in candidates):
        return candidates[-1][0]
    return max(candidates, key=lambda pair: pair[1])[0]


def _pick_src(attrs: dict) -> tuple:
    """按优先级从标签属性中挑出最高质量的地址。

    :return: (命中的属性名, 地址)；都没命中返回 ("", "")
    """
    for key in _SRC_ATTR_PRIORITY:
        value = (attrs.get(key) or "").strip()
        if value:
            return key, value
    return "", ""


def _page_album(html_text: str) -> str:
    """取页面标题作为合集名（og:title 优先，其次 <title>）。"""
    for match in _OG_TITLE_RE.finditer(html_text):
        content = _CONTENT_RE.search(match.group(0))
        if content:
            title = (content.group(1) or content.group(2) or "").strip()
            if title:
                return html_lib.unescape(title)[:60]
    match = _TITLE_RE.search(html_text)
    if match:
        title = re.sub(r"\s+", " ", html_lib.unescape(match.group(1))).strip()
        if title:
            return title[:60]
    return ""


class GenericHTMLExtractor(BaseExtractor):
    """通用 HTML 提取器（正则实现，遍历多类媒体来源）。"""

    name = "html"
    priority = 80

    def can_handle(self, url: str, ctx) -> float:
        return 0.9 if getattr(ctx, "html", "") else 0.0

    # ---------- 主流程 ----------
    def extract(self, ctx) -> Iterator[MediaItem]:
        html_text = ctx.html or ""
        album = _page_album(html_text)
        seen: set[str] = set()
        index = 0

        for raw, title, origin in self._iter_candidates(html_text):
            item = self.make_item(raw, ctx, title=title, album=album,
                                  index=index + 1, origin=f"html:{origin}" if origin else "html")
            if item is None or item.url in seen:
                continue
            seen.add(item.url)
            # 类型未知但在 HTML 媒体标签里出现：按图片兜底（多数场景成立）
            if item.kind == TYPE_UNKNOWN or item.kind not in MEDIA_TYPES:
                item.kind = kind_of(item.url, "") or TYPE_IMAGE
                if item.kind not in MEDIA_TYPES:
                    continue
            index += 1
            yield item
            yield from self._upgraded(item, ctx, album, seen)

    def _upgraded(self, item: MediaItem, ctx, album: str, seen: set) -> Iterator[MediaItem]:
        """缩略图 URL → 原图候选（命中命名规律时额外产出一条）。"""
        if not self.config.upgrade_urls:
            return
        upgraded = upgrade_thumbnail_url(item.url)
        if not upgraded or upgraded in seen:
            return
        seen.add(upgraded)
        candidate = self.make_item(upgraded, ctx, kind=item.kind, title=item.title,
                                   album=album, index=item.index,
                                   origin=f"{item.origin}+upgrade",
                                   extra={"upgraded_from": item.url})
        if candidate is not None:
            yield candidate

    # ---------- 各类来源 ----------
    def _iter_candidates(self, html_text: str) -> Iterator[tuple]:
        """依次产出 (原始地址, 标题, 来源标签)。"""
        yield from self._from_tags(html_text)
        yield from self._from_meta(html_text)
        yield from self._from_css(html_text)
        yield from self._from_scripts(html_text)
        yield from self._from_anchors(html_text)

    def _from_tags(self, html_text: str) -> Iterator[tuple]:
        """媒体标签：每个标签只取最高质量的地址，避免把缩略图也下载一遍。

        ``srcset`` 与"原图属性"（``data-original`` 等）可能指向不同地址，
        两者都是大图时同时保留；普通 ``src`` 与 ``srcset`` 同时存在时只取
        ``srcset`` 中最大的那张（``src`` 通常是小图兜底）。
        """
        for tag in _MEDIA_TAGS:
            pattern = re.compile(rf"<{tag}\b[^>]*>", re.IGNORECASE)
            for match in pattern.finditer(html_text):
                attrs = _parse_attrs(match.group(0))
                title = html_lib.unescape((attrs.get("alt") or attrs.get("title") or "")
                                          ).strip()[:60]
                attr_key, best = _pick_src(attrs)
                srcset_value = ""
                for key in _SRCSET_ATTRS:
                    if attrs.get(key):
                        srcset_value = attrs[key]
                        break
                from_srcset = _best_srcset(srcset_value) if srcset_value else ""
                if from_srcset and attr_key not in _STRONG_ATTRS:
                    yield from_srcset, title, "srcset"
                else:
                    if best:
                        yield best, title, tag
                    if from_srcset and from_srcset != best:
                        yield from_srcset, title, "srcset"
                # 封面图（视频第一帧）
                poster = (attrs.get("poster") or attrs.get("data-poster") or "").strip()
                if poster:
                    yield poster, title, "poster"

    def _from_meta(self, html_text: str) -> Iterator[tuple]:
        """og:image / og:video 等社交卡片元信息（文章主图常只在此出现）。"""
        for pattern, tag in ((_OG_IMAGE_RE, "og:image"), (_OG_VIDEO_RE, "og:video")):
            for match in pattern.finditer(html_text):
                content = _CONTENT_RE.search(match.group(0))
                if not content:
                    continue
                value = (content.group(1) or content.group(2) or "").strip()
                if value:
                    yield value, "", tag

    def _from_css(self, html_text: str) -> Iterator[tuple]:
        """CSS 背景图：``style`` 属性与 ``<style>`` 块中的 ``url(...)``。

        仅保留带媒体扩展名的地址，避免把字体（woff2）等一并带出。
        """
        blocks = [match.group(0) for match in
                  re.finditer(r"""style\s*=\s*(?:"([^"]*)"|'([^']*)')""",
                              html_text, re.IGNORECASE)]
        blocks.extend(match.group(0) for match in _STYLE_BLOCK_RE.finditer(html_text))
        for block in blocks:
            for match in _CSS_URL_RE.finditer(block):
                value = (match.group(1) or match.group(2) or match.group(3) or "").strip()
                if not value or not has_media_ext(value):
                    continue
                yield value, "", "css"

    def _from_scripts(self, html_text: str) -> Iterator[tuple]:
        """JS/JSON 内嵌直链（播放地址、图集数组等常写在脚本变量里）。

        只在 ``<script>`` 块内匹配：若在全文档上跑，会把 ``src="..."`` 这类
        HTML 属性值也当成脚本字符串，产生重复与错误的猜解结果。
        """
        for block in _SCRIPT_BLOCK_RE.finditer(html_text):
            body = block.group(1)
            for pattern, tag in ((_JS_ABS_RE, "js"), (_JS_REL_RE, "js-rel")):
                for match in pattern.finditer(body):
                    yield match.group(1), "", tag

    def _from_anchors(self, html_text: str) -> Iterator[tuple]:
        """``<a href>`` 指向媒体文件（下载页/种子页常见）。"""
        for match in _A_HREF_RE.finditer(html_text):
            value = (match.group(1) or match.group(2) or "").strip()
            if not value or not has_media_ext(value):
                continue
            if not ext_of(value):
                continue
            yield value, "", "a-href"


#: 供规则提取器复用的公共工具（避免"选最优 srcset / 解析标签属性"两处实现）
parse_attrs = _parse_attrs
best_srcset = _best_srcset
extract_page_album = _page_album

__all__ = ["GenericHTMLExtractor", "parse_attrs", "best_srcset", "extract_page_album"]
