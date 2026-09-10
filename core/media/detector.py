"""媒体类型识别：URL 扩展名 / Content-Type → 资源类型。

原先内联在 ``core/downloader.ContentDownloader`` 中的分类常量与方法集中到此，
供下载器、媒体爬取线程（``core/media_crawler.py``）与各提取器共用，
避免"同一份规则多处维护"导致行为不一致。

资源类型取值：
- ``image`` / ``video`` / ``audio`` / ``document``：可直接下载的单文件资源
- ``hls`` / ``dash``：流媒体播放列表（需解析 + 分片合并，见 core/media/hls.py）
- ``other``：无法识别但仍是 http(s) 资源
- ``unknown``：尚未判定（提取阶段占位）

本模块只依赖标准库。
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# 资源类型常量
# ---------------------------------------------------------------------------
TYPE_IMAGE = "image"
TYPE_VIDEO = "video"
TYPE_AUDIO = "audio"
TYPE_DOCUMENT = "document"
TYPE_HLS = "hls"
TYPE_DASH = "dash"
TYPE_OTHER = "other"
TYPE_UNKNOWN = "unknown"

#: 流媒体类型：需解析播放列表并合并分片，不能按普通单文件下载
STREAM_TYPES = frozenset({TYPE_HLS, TYPE_DASH})

#: 媒体类型（有别于 document / other）
MEDIA_TYPES = frozenset({TYPE_IMAGE, TYPE_VIDEO, TYPE_AUDIO, TYPE_HLS, TYPE_DASH})

# ---------------------------------------------------------------------------
# 扩展名集合
# ---------------------------------------------------------------------------
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".ico",
              ".avif", ".heic", ".jfif", ".tif", ".tiff"}
VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".avi", ".mov", ".flv", ".wmv", ".m4v",
              ".ts", ".mpg", ".mpeg", ".3gp", ".f4v", ".rmvb"}
AUDIO_EXTS = {".mp3", ".wav", ".aac", ".ogg", ".flac", ".m4a", ".wma", ".opus",
              ".oga", ".aiff"}
DOCUMENT_EXTS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
                 ".txt", ".zip", ".rar", ".7z", ".epub"}
#: HLS 播放列表
HLS_EXTS = {".m3u8", ".m3u"}
#: DASH 清单
DASH_EXTS = {".mpd"}

# ---------------------------------------------------------------------------
# Content-Type → 扩展名映射（无法识别时用原样 subtype）
# ---------------------------------------------------------------------------
EXT_MAP = {
    "jpeg": "jpg", "svg+xml": "svg", "x-icon": "ico", "vnd.microsoft.icon": "ico",
    "quicktime": "mov", "x-msvideo": "avi", "mpeg": "mpg", "mp2t": "ts",
    "octet-stream": "bin", "plain": "txt", "html": "html", "json": "json",
    "vnd.apple.mpegurl": "m3u8", "x-mpegurl": "m3u8", "mpegurl": "m3u8",
    "dash+xml": "mpd",
}

#: 资源类型优先级（数值越小越先下载；页面图片优先，视频大文件靠后）
TYPE_PRIORITY = {
    TYPE_IMAGE: 0, TYPE_AUDIO: 1, TYPE_DOCUMENT: 2,
    TYPE_VIDEO: 3, TYPE_HLS: 3, TYPE_DASH: 3,
    TYPE_OTHER: 4, TYPE_UNKNOWN: 4,
}

# ---------------------------------------------------------------------------
# 扩展名 → 类型索引（构建一次，避免重复集合查找）
# ---------------------------------------------------------------------------
EXT_KIND: dict[str, str] = {}
for _ext in IMAGE_EXTS:
    EXT_KIND[_ext] = TYPE_IMAGE
for _ext in VIDEO_EXTS:
    EXT_KIND[_ext] = TYPE_VIDEO
for _ext in AUDIO_EXTS:
    EXT_KIND[_ext] = TYPE_AUDIO
for _ext in DOCUMENT_EXTS:
    EXT_KIND[_ext] = TYPE_DOCUMENT
for _ext in HLS_EXTS:
    EXT_KIND[_ext] = TYPE_HLS
for _ext in DASH_EXTS:
    EXT_KIND[_ext] = TYPE_DASH

#: 扩展名粗筛正则（含 image/video/audio/流媒体），网络监听快速过滤用
_MEDIA_EXT_ALT = "|".join(sorted(
    re.escape(e.lstrip("."))
    for e in (IMAGE_EXTS | VIDEO_EXTS | AUDIO_EXTS | HLS_EXTS | DASH_EXTS)))
MEDIA_EXT_RE = re.compile(r"\.(?:" + _MEDIA_EXT_ALT + r")(?:[?#]|$)", re.IGNORECASE)

# Content-Type 归一化：把各类 HLS / DASH 写法统一到具体类型
_HLS_CONTENT_TYPES = (
    "application/vnd.apple.mpegurl", "application/x-mpegurl", "application/mpegurl",
    "audio/mpegurl", "audio/x-mpegurl", "vnd.apple.mpegurl", "x-mpegurl",
)
_DASH_CONTENT_TYPES = ("application/dash+xml", "dash+xml")
# 归入 document 的 Content-Type 关键词
_DOCUMENT_CT_KEYWORDS = ("pdf", "word", "sheet", "excel", "powerpoint",
                         "presentation", "zip", "compressed", "epub", "opendocument")


# ---------------------------------------------------------------------------
# URL 侧识别
# ---------------------------------------------------------------------------
def ext_of(url: str) -> str:
    """取 URL 路径部分的扩展名（小写，含点号）；无扩展名返回空串。

    只取路径部分，避免 ``/pic?id=1.jpg`` 这类查询串干扰判定。
    """
    try:
        path = urlparse(url).path
    except (ValueError, AttributeError):
        path = url or ""
    return os.path.splitext(path)[1].lower()


def kind_by_ext(ext: str) -> str:
    """扩展名 → 资源类型（含点号小写），未收录返回 :data:`TYPE_OTHER`。"""
    return EXT_KIND.get((ext or "").lower(), TYPE_OTHER)


def classify_url(url: str) -> str:
    """按 URL 扩展名推断资源类型。

    - ``.m3u8`` → ``hls``，``.mpd`` → ``dash``（流媒体单独归类，供合并流程识别）
    - 未收录扩展名（含无扩展名的接口式直链）→ ``other``，后续可按 Content-Type 复判
    """
    return kind_by_ext(ext_of(url))


# ---------------------------------------------------------------------------
# Content-Type 侧识别
# ---------------------------------------------------------------------------
def classify_content_type(content_type: str) -> str | None:
    """按 Content-Type 推断资源类型；无法识别返回 None（交由调用方回退）。

    ``text/*`` 与办公文档类统一归入 ``document``，与原下载器行为保持一致。
    """
    ct = (content_type or "").split(";")[0].strip().lower()
    if not ct:
        return None
    if ct in _HLS_CONTENT_TYPES:
        return TYPE_HLS
    if ct in _DASH_CONTENT_TYPES:
        return TYPE_DASH
    for prefix, rtype in (("image/", TYPE_IMAGE), ("video/", TYPE_VIDEO),
                          ("audio/", TYPE_AUDIO)):
        if ct.startswith(prefix):
            # audio/mpegurl 之类已被上面的精确匹配拦下，这里不会误判 HLS
            return rtype
    if ct.startswith("text/") or any(k in ct for k in _DOCUMENT_CT_KEYWORDS):
        return TYPE_DOCUMENT
    return None


def kind_of(url: str, content_type: str = "") -> str:
    """综合 URL 扩展名与 Content-Type 判定资源类型（Content-Type 优先）。

    接口式直链（无扩展名）依赖 Content-Type 才能识别；
    而 Content-Type 为通用类型（如 ``application/octet-stream``）时回退到扩展名。
    """
    by_ct = classify_content_type(content_type)
    by_url = classify_url(url)
    if by_ct is not None:
        # Content-Type 只给出 document 而 URL 明确是媒体时，信任 URL（下载器原行为）
        if by_ct == TYPE_DOCUMENT and by_url in MEDIA_TYPES:
            return by_url
        return by_ct
    return by_url


def guess_extension(content_type: str) -> str:
    """按 Content-Type 推断文件扩展名（含点号），无法识别返回 ``.bin``。"""
    ct = (content_type or "").split(";")[0].strip().lower()
    if "/" not in ct:
        return ".bin"
    main, sub = ct.split("/", 1)
    if main not in ("image", "video", "audio", "application", "text"):
        return ".bin"
    return "." + EXT_MAP.get(sub, sub)


def is_media_kind(kind: str) -> bool:
    """是否属于媒体类型（图片/视频/音频/流媒体）。"""
    return kind in MEDIA_TYPES


def is_stream_kind(kind: str) -> bool:
    """是否属于流媒体类型（hls / dash）。"""
    return kind in STREAM_TYPES


def has_media_ext(url: str) -> bool:
    """URL 中是否含媒体扩展名（网络事件粗筛，判定宽松不影响最终类型判定）。"""
    try:
        return bool(MEDIA_EXT_RE.search(url))
    except TypeError:
        return False


__all__ = [
    "TYPE_IMAGE", "TYPE_VIDEO", "TYPE_AUDIO", "TYPE_DOCUMENT", "TYPE_HLS",
    "TYPE_DASH", "TYPE_OTHER", "TYPE_UNKNOWN", "STREAM_TYPES", "MEDIA_TYPES",
    "IMAGE_EXTS", "VIDEO_EXTS", "AUDIO_EXTS", "DOCUMENT_EXTS", "HLS_EXTS",
    "DASH_EXTS", "EXT_MAP", "EXT_KIND", "TYPE_PRIORITY", "MEDIA_EXT_RE",
    "ext_of", "kind_by_ext", "classify_url", "classify_content_type",
    "kind_of", "guess_extension", "is_media_kind", "is_stream_kind",
    "has_media_ext",
]
