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


def extract_media_links(html_content: str, base_url: str) -> set[str]:
    """从HTML中提取图片/视频/音频等媒体资源链接。

    覆盖 img/video/audio/source 的 src、data-src、srcset 属性。
    """
    media = set()
    patterns = [
        r'<img[^>]+src\s*=\s*["\']([^"\']+)["\']',
        r'<img[^>]+data-src\s*=\s*["\']([^"\']+)["\']',
        r'<img[^>]+srcset\s*=\s*["\']([^"\']+)["\']',
        r'<video[^>]+src\s*=\s*["\']([^"\']+)["\']',
        r'<audio[^>]+src\s*=\s*["\']([^"\']+)["\']',
        r'<source[^>]+src\s*=\s*["\']([^"\']+)["\']',
        r'<source[^>]+data-src\s*=\s*["\']([^"\']+)["\']',
    ]
    for pat in patterns:
        for m in re.finditer(pat, html_content, re.IGNORECASE):
            raw = m.group(1).strip()
            # srcset可能包含多个候选地址（用逗号分隔），只取第一个
            first = raw.split(",")[0].strip().split(" ")[0]
            full_url = urljoin(base_url, first)
            if full_url.startswith(('http://', 'https://')):
                media.add(full_url)
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
