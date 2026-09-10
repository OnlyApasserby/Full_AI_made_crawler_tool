"""core.media —— 媒体领域层（类型识别 / 数据模型 / 探测 / 流媒体）。

模块划分：
- ``detector``：URL 扩展名与 Content-Type → 资源类型（唯一判定口径）
- ``models``：``MediaItem``（提取结果与下载输入的统一载体）、``MediaConfig``
- ``probe``：图片尺寸探测（本地文件 / 远端头部）
- ``hls`` / ``dash`` / ``merger``：流媒体解析与合并（**可选功能，首期仅预留接口**）

本包只依赖标准库与可选依赖（Pillow），不依赖 Qt 与网络层的具体实现。
"""

from __future__ import annotations

from core.media.detector import (
    TYPE_AUDIO, TYPE_DASH, TYPE_DOCUMENT, TYPE_HLS, TYPE_IMAGE, TYPE_OTHER,
    TYPE_UNKNOWN, TYPE_VIDEO, STREAM_TYPES, MEDIA_TYPES, TYPE_PRIORITY,
    classify_content_type, classify_url, ext_of, guess_extension,
    has_media_ext, is_media_kind, is_stream_kind, kind_by_ext, kind_of,
)
from core.media.models import (
    RENDER_AUTO, RENDER_DYNAMIC, RENDER_STATIC,
    THUMB_QUERY_KEYS, THUMB_RULES, MediaConfig, MediaItem, merge_items,
    normalize_url, safe_name, upgrade_thumbnail_url, url_fingerprint,
)
from core.media.probe import (
    DEFAULT_PREFIX_BYTES, HAS_PIL, dhash, hamming, is_duplicate_hash,
    probe_bytes, probe_from_fetcher, probe_local,
)
# 流媒体：hls/dash 解析 → merger 下载合并（merger 不反向依赖，无循环导入）
from core.media import dash, hls, merger
from core.media.merger import (
    MediaPlan, Segment, StreamError, download_and_merge, find_ffmpeg,
    has_ffmpeg, target_suffix,
)

__all__ = [
    # detector
    "TYPE_IMAGE", "TYPE_VIDEO", "TYPE_AUDIO", "TYPE_DOCUMENT", "TYPE_HLS",
    "TYPE_DASH", "TYPE_OTHER", "TYPE_UNKNOWN", "STREAM_TYPES", "MEDIA_TYPES",
    "TYPE_PRIORITY", "classify_url", "classify_content_type", "ext_of",
    "guess_extension", "has_media_ext", "is_media_kind", "is_stream_kind",
    "kind_by_ext", "kind_of",
    # models
    "RENDER_STATIC", "RENDER_DYNAMIC", "RENDER_AUTO",
    "MediaItem", "MediaConfig", "normalize_url", "url_fingerprint", "safe_name",
    "THUMB_RULES", "THUMB_QUERY_KEYS", "upgrade_thumbnail_url", "merge_items",
    # probe
    "HAS_PIL", "DEFAULT_PREFIX_BYTES", "probe_bytes", "probe_local",
    "probe_from_fetcher", "dhash", "hamming", "is_duplicate_hash",
    # 流媒体
    "hls", "dash", "merger", "StreamError", "MediaPlan", "Segment",
    "download_and_merge", "find_ffmpeg", "has_ffmpeg", "target_suffix",
]
