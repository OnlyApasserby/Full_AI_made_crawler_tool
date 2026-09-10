"""页面解析器：从 HTML 中提取链接、媒体资源与纯文字。

这些函数原是 RecursiveCrawlerThread 的成员方法，均不依赖实例状态，
拆分为独立模块后由 core/crawler.py 调用。
"""

from __future__ import annotations

import html
import re
from urllib.parse import urljoin


def extract_links(html_content: str, base_url: str) -> set[str]:
    """从HTML中提取所有合法的绝对链接。

    :param html_content: 页面 HTML 源码
    :param base_url: 页面 URL（用于拼接相对链接）
    :return: 绝对 http(s) 链接集合
    """
    links = re.findall(r'<a[^>]+href\s*=\s*["\']([^"\']+)["\']', html_content, re.IGNORECASE)
    absolute_links = set()
    for link in links:
        full_link = urljoin(base_url, link)
        # 过滤非http协议链接
        if full_link.startswith(('http://', 'https://')):
            absolute_links.add(full_link)
    return absolute_links


# 媒体文件扩展名（用于筛选 a[href] / link / JS 内嵌直链，避免把普通链接当媒体）
MEDIA_EXT_PATTERN = (
    r"\.(?:mp4|m4v|mov|webm|mkv|avi|flv|f4v|wmv|mpg|mpeg|3gp|rmvb|m3u8|"
    r"mp3|m4a|aac|flac|ogg|oga|opus|wav|wma)"
)
MEDIA_EXT_RE = re.compile(MEDIA_EXT_PATTERN + r"(?:[?#]|$)", re.IGNORECASE)
# 可直接取用的媒体地址属性（含常见懒加载/播放器属性）
_MEDIA_SRC_ATTR = (r"(?:src|data-src|data-original|data-lazy-src|data-url|"
                   r"data-video|data-video-src|data-mp4|data-source)")


def extract_media_links(html_content: str, base_url: str) -> set[str]:
    """从HTML中提取图片/视频/音频等媒体资源链接。

    覆盖范围：
    - img/video/audio/source/embed 的 src、data-src、data-original、data-lazy-src、
      data-url、data-video、data-video-src、data-mp4、data-source 等属性
    - img/video 的 poster、srcset/data-srcset（多候选取第一个）
    - a[href]、link[rel=preload as=video/audio]、meta[og:video] 指向的媒体直链
    - JS/JSON 内嵌的视频/音频直链（如 "url":"https://.../v.mp4"、"....m3u8?k=v"）
    """
    media: set[str] = set()

    def _add(raw: str, require_media_ext: bool = False) -> None:
        candidate = html.unescape((raw or "").strip())
        if not candidate or candidate.lower().startswith(
                ("data:", "blob:", "javascript:", "about:", "#")):
            return
        # srcset 可能包含多个候选地址（逗号/空格分隔），只取第一个
        candidate = candidate.split(",")[0].strip().split(" ")[0]
        full_url = urljoin(base_url, candidate)
        if not full_url.startswith(("http://", "https://")):
            return
        if require_media_ext and not MEDIA_EXT_RE.search(full_url):
            return
        media.add(full_url)

    # 1. 媒体标签的地址属性（含懒加载属性）
    for tag in ("img", "video", "audio", "source", "embed"):
        pattern = rf'<{tag}\b[^>]*\b{_MEDIA_SRC_ATTR}\s*=\s*["\']([^"\']+)["\']'
        for m in re.finditer(pattern, html_content, re.IGNORECASE):
            _add(m.group(1))

    # 2. 封面图
    for m in re.finditer(
            r'<(?:img|video)\b[^>]*\b(?:poster|data-poster)\s*=\s*["\']([^"\']+)["\']',
            html_content, re.IGNORECASE):
        _add(m.group(1))

    # 3. 多候选图（srcset）
    for m in re.finditer(
            r'<(?:img|source)\b[^>]*\b(?:srcset|data-srcset)\s*=\s*["\']([^"\']+)["\']',
            html_content, re.IGNORECASE):
        _add(m.group(1))

    # 4. a[href] 指向媒体文件（视频下载页/种子页常见）
    for m in re.finditer(r'<a\b[^>]*\bhref\s*=\s*["\']([^"\']+)["\']',
                         html_content, re.IGNORECASE):
        _add(m.group(1), require_media_ext=True)

    # 5. link rel=preload as=video/audio
    for m in re.finditer(r"<link\b[^>]*>", html_content, re.IGNORECASE):
        tag = m.group(0)
        if re.search(r'as\s*=\s*["\'](?:video|audio)["\']', tag, re.IGNORECASE):
            href = re.search(r'href\s*=\s*["\']([^"\']+)["\']', tag, re.IGNORECASE)
            if href:
                _add(href.group(1))

    # 6. meta og:video / twitter:player:stream
    for m in re.finditer(
            r'<meta\b[^>]*\b(?:property|name)\s*=\s*["\']'
            r'(?:og:video(?::url|:secure_url)?|twitter:player:stream)["\'][^>]*>',
            html_content, re.IGNORECASE):
        content = re.search(r'content\s*=\s*["\']([^"\']+)["\']', m.group(0), re.IGNORECASE)
        if content:
            _add(content.group(1), require_media_ext=True)

    # 7. JS/JSON 内嵌直链（视频站常见：播放地址写在脚本变量中）
    for m in re.finditer(
            rf'["\']((?:https?:)?//[^"\'\s<>]+?{MEDIA_EXT_PATTERN}(?:\?[^"\'\s<>]*)?)["\']',
            html_content, re.IGNORECASE):
        _add(m.group(1))

    return media


def extract_text(html_content: str) -> str:
    """从HTML中提取纯文字内容（去除脚本、样式、标签并还原HTML实体）。"""
    # 去除 script/style 块
    content = re.sub(r'<script[^>]*>.*?</script>', ' ', html_content,
                     flags=re.IGNORECASE | re.DOTALL)
    content = re.sub(r'<style[^>]*>.*?</style>', ' ', content,
                     flags=re.IGNORECASE | re.DOTALL)
    # 标签替换为换行
    text = re.sub(r'<[^>]+>', '\n', content)
    # 还原HTML实体
    text = html.unescape(text)
    # 清理空白与空行
    lines = [ln.strip() for ln in text.splitlines()]
    return '\n'.join(ln for ln in lines if ln)


__all__ = ["extract_links", "extract_media_links", "extract_text"]
