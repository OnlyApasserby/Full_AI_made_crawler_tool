"""媒体领域数据模型：单条媒体资源 ``MediaItem`` 与媒体抓取配置 ``MediaConfig``。

与 UI / 数据库解耦的纯数据结构，可被 core / manager / ui 任意引用。

URL 归一化去重（``normalize_url`` / ``url_fingerprint``）也放在这里：
它属于"媒体资源身份"的定义，且被 pipeline 与提取器共用。
追踪参数集合直接复用 ``core.security.STRONG_AD_PARAMS``，
避免"广告识别一份、去重一份"两处维护导致口径不一致。
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from core.media.detector import (
    MEDIA_TYPES, STREAM_TYPES, TYPE_UNKNOWN, ext_of,
)
from core.security import STRONG_AD_PARAMS

# ---------------------------------------------------------------------------
# 渲染模式（决定用静态请求还是浏览器渲染抓取）
# ---------------------------------------------------------------------------
RENDER_STATIC = "static"
RENDER_DYNAMIC = "dynamic"
RENDER_AUTO = "auto"

#: 安全文件名/目录名清洗：去掉路径分隔符与常见非法字符
_UNSAFE_NAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def normalize_url(url: str) -> str:
    """URL 归一化：用于去重的"等价地址"表示。

    处理内容：
    1. scheme / host 转小写，去掉 fragment 与默认端口
    2. 剔除追踪/广告参数（``utm_*``、``gclid``、``spm`` 等，见 STRONG_AD_PARAMS）
    3. 保留参数按键排序，保证 ``?b=1&a=2`` 与 ``?a=2&b=1`` 视为同一地址

    解析失败时原样返回，保证调用方不需要处理异常。
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
    except ValueError:
        return raw
    if not parsed.scheme and not parsed.netloc:
        return raw
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if not host:
        return raw
    netloc = host
    if parsed.port and not ((scheme == "http" and parsed.port == 80)
                            or (scheme == "https" and parsed.port == 443)):
        netloc = f"{host}:{parsed.port}"
    if parsed.username:
        auth = parsed.username
        if parsed.password:
            auth += f":{parsed.password}"
        netloc = f"{auth}@{netloc}"
    try:
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
    except ValueError:
        pairs = []
    kept = [(k, v) for k, v in pairs
            if k.lower() not in STRONG_AD_PARAMS and not k.lower().startswith("utm_")]
    kept.sort()
    return urlunparse((scheme, netloc, parsed.path, parsed.params,
                       urlencode(kept), ""))


def url_fingerprint(url: str) -> str:
    """归一化后的 URL 指纹（SHA-256），用于跨来源/跨页面去重。"""
    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()


def safe_name(name: str, fallback: str = "", max_len: int = 80) -> str:
    """清洗名称以便安全用作文件/目录名（非法字符替换为下划线并截断）。"""
    cleaned = _UNSAFE_NAME_RE.sub("_", str(name or "")).strip(" ._")
    cleaned = re.sub(r"\s+", " ", cleaned)[:max_len].strip()
    return cleaned or fallback


#: 缩略图 → 原图的常见命名规律（左为缩略图特征，右为原图替换结果）
THUMB_RULES = (
    ("/thumb/", "/original/"),
    ("/thumbs/", "/originals/"),
    ("/thumbnail/", "/original/"),
    ("/thumbnails/", "/images/"),
    ("/small/", "/large/"),
    ("/s/", "/l/"),
    ("_thumb.", "."),
    ("_thumbnail.", "."),
    ("_small.", "."),
    ("-thumb.", "."),
    ("_s.", "."),
    ("_150x150.", "."),
    ("_240x240.", "."),
    ("_300x300.", "."),
)
#: 查询串中的缩略图尺寸参数（→ 删除该参数以获得原图）
THUMB_QUERY_KEYS = {"w", "h", "width", "height", "size", "resize", "quality",
                    "thumb", "thumbnail", "x-oss-process", "imageview"}


def upgrade_thumbnail_url(url: str) -> str:
    """把缩略图 URL 启发式猜解为原图 URL；无法猜解时返回空串。

    只做**高置信度**替换（命中预设命名规律），避免误猜导致大量 404。
    调用方应把猜解结果作为**额外候选**，而不是替换掉原始地址。
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    lowered = raw.lower()
    for thumb, original in THUMB_RULES:
        if thumb in lowered:
            return raw.replace(thumb, original).replace(thumb.upper(), original)
    # 查询串尺寸参数：去掉后通常是原图（仅在同时含图片扩展名时尝试）
    try:
        parsed = urlparse(raw)
    except ValueError:
        return ""
    if not parsed.query or not ext_of(raw):
        return ""
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    if not any(k.lower() in THUMB_QUERY_KEYS for k, _ in pairs):
        return ""
    kept = [(k, v) for k, v in pairs if k.lower() not in THUMB_QUERY_KEYS]
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params,
                       urlencode(kept), parsed.fragment))


def merge_items(target: "MediaItem", source: "MediaItem") -> "MediaItem":
    """把同一资源的多个提取结果合并到 ``target``（就地修改并返回）。

    合并策略：``target`` 中为空的字段用 ``source`` 补齐，字典字段按键合并
    （``target`` 已有键优先）。这样"网络事件带的请求头"与"HTML 提取带的
    合集/标题"能互补，而不是互相覆盖。
    """
    for field_name in ("kind", "source_page", "album", "title", "ext", "origin"):
        if not getattr(target, field_name, "") and getattr(source, field_name, ""):
            setattr(target, field_name, getattr(source, field_name))
    if not target.index and source.index:
        target.index = source.index
    for field_name in ("width", "height", "size", "duration"):
        if not getattr(target, field_name, 0) and getattr(source, field_name, 0):
            setattr(target, field_name, getattr(source, field_name))
    for key, value in (source.headers or {}).items():
        target.headers.setdefault(key, value)
    for key, value in (source.extra or {}).items():
        target.extra.setdefault(key, value)
    return target


@dataclass
class MediaItem:
    """单条媒体资源（提取结果 / 下载输入的统一载体）。

    相比裸 URL，额外携带**来源页**与**请求头**，使下载阶段能够：
    - 正确处理防盗链（Referer / Cookie 由抓取阶段原样带下来）
    - 按 合集 / 序号 / 标题 组织落盘目录
    - 展示与导出去重、分辨率、时长等元信息
    """

    url: str
    kind: str = TYPE_UNKNOWN
    source_page: str = ""            # 来源页（同时也是默认 Referer）
    album: str = ""                  # 图集/合集名（用于分目录）
    title: str = ""
    index: int = 0                   # 合集内序号
    ext: str = ""                    # 含点号，空则按 Content-Type 推断
    headers: dict = field(default_factory=dict)
    width: int = 0
    height: int = 0
    size: int = 0
    duration: float = 0.0
    origin: str = ""                 # html / json / network —— 便于排查漏抓
    extra: dict = field(default_factory=dict)

    # ---------- 派生属性 ----------
    @property
    def is_media(self) -> bool:
        """是否属于媒体类型（图片/视频/音频/流媒体）。"""
        return self.kind in MEDIA_TYPES

    @property
    def is_stream(self) -> bool:
        """是否属于流媒体（hls / dash），需走解析 + 合并流程。"""
        return self.kind in STREAM_TYPES

    @property
    def referer(self) -> str:
        """下载时应使用的 Referer：显式请求头优先，否则回退来源页。"""
        for key, value in self.headers.items():
            if key.lower() == "referer":
                return value
        return self.source_page or ""

    @property
    def resolution(self) -> str:
        """可读分辨率表示（未知返回空串）。"""
        return f"{self.width}x{self.height}" if self.width and self.height else ""

    def key(self) -> str:
        """去重键（URL 归一化指纹）。"""
        return url_fingerprint(self.url)

    def ensure_ext(self) -> str:
        """返回扩展名：优先显式值，其次从 URL 推断。"""
        return self.ext or ext_of(self.url)

    def display(self) -> str:
        """单行展示文本（日志/表格用）。"""
        parts = [self.kind or TYPE_UNKNOWN]
        if self.resolution:
            parts.append(self.resolution)
        if self.album:
            parts.append(f"合集={self.album}")
        return f"[{'/'.join(parts)}] {self.url}"

    def filename(self, template: str = "{album}/{index:03d}_{title}{ext}",
                 fallback: str = "media") -> str:
        """按模板生成相对保存路径（不含下载根目录）。

        模板可用字段：{album} {title} {index} {kind} {ext} {url}
        非法字符会被清洗；模板结果为空时回退为 URL 文件名或哈希占位。
        """
        values = {
            "album": safe_name(self.album),
            "title": safe_name(self.title),
            "index": self.index,
            "kind": self.kind,
            "ext": self.ensure_ext(),
            "url": self.url,
        }
        try:
            rendered = template.format(**values)
        except (KeyError, IndexError, ValueError):
            rendered = ""
        rendered = "/".join(seg for seg in
                            (safe_name(seg) for seg in rendered.split("/")) if seg)
        if not rendered or rendered in (".", ".."):
            name = safe_name(os.path.basename(urlparse(self.url).path), fallback)
            if "." not in name:
                name = f"{name}{self.ensure_ext() or ''}"
            rendered = safe_name(name, fallback)
        return rendered

    # ---------- 序列化 ----------
    def to_dict(self) -> dict:
        return {
            "url": self.url, "kind": self.kind, "source_page": self.source_page,
            "album": self.album, "title": self.title, "index": self.index,
            "ext": self.ext, "headers": dict(self.headers),
            "width": self.width, "height": self.height, "size": self.size,
            "duration": self.duration, "origin": self.origin, "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MediaItem":
        data = data or {}
        return cls(
            url=str(data.get("url", "")), kind=str(data.get("kind", TYPE_UNKNOWN)),
            source_page=str(data.get("source_page", "")),
            album=str(data.get("album", "")), title=str(data.get("title", "")),
            index=int(data.get("index", 0) or 0), ext=str(data.get("ext", "")),
            headers=dict(data.get("headers") or {}),
            width=int(data.get("width", 0) or 0),
            height=int(data.get("height", 0) or 0),
            size=int(data.get("size", 0) or 0),
            duration=float(data.get("duration", 0.0) or 0.0),
            origin=str(data.get("origin", "")), extra=dict(data.get("extra") or {}),
        )


@dataclass
class MediaConfig:
    """媒体抓取配置（UI 控件 ↔ 抓取线程的中间载体）。

    与 ``models.TaskConfig`` 的 ``to_dict / from_dict`` 保持同构风格，
    便于随任务队列一起持久化。
    """

    enabled: bool = False
    #: 目标类型：image / video / audio 的子集（hls/dash 由 video 派生识别）
    kinds: list = field(default_factory=lambda: ["image", "video"])
    render_mode: str = RENDER_AUTO
    # 动态渲染
    scroll: bool = True              # 滚动触发懒加载
    max_scroll: int = 8              # 滚动次数上限
    wait_selector: str = ""          # 等待该选择器出现再提取
    wait_timeout: float = 15.0
    # 翻页
    follow_pagination: bool = True
    max_pages: int = 5               # 翻页上限（0=不限制）
    # 层级：0=只抓入口页(+翻页)；1=再跟进页面内的详情页链接（列表页→详情页 图集）
    max_depth: int = 0
    #: 跟进详情页时的链接关键词过滤（留空表示不限制）
    detail_link_pattern: str = ""
    # 质量过滤
    min_width: int = 0
    min_height: int = 0
    min_size: int = 0                # 字节
    max_size: int = 0                # 字节，0=不限
    allow_svg: bool = False
    max_count: int = 0               # 0=不限
    # 去重
    dedup: bool = True               # URL 归一化 + 内容 MD5
    dedup_perceptual: bool = True    # 感知哈希（需 Pillow，缺失自动跳过）
    # 提取增强：缩略图 URL → 原图 URL 的启发式猜解（/thumb/ → /original/ 等）
    upgrade_urls: bool = True
    # 流媒体（HLS/DASH 分片合并）
    merge_stream: bool = False
    #: 合并后的转封装策略：auto（有 ffmpeg 才转）/ ffmpeg（强制，失败保底原始容器）/ never
    stream_remux: str = "auto"
    # 落盘命名
    filename_template: str = "{album}/{index:03d}_{title}{ext}"
    extra: dict = field(default_factory=dict)

    def target_kinds(self) -> list[str]:
        """规范化后的目标类型列表（去重、小写、过滤非法值）。"""
        allowed = {"image", "video", "audio"}
        out: list[str] = []
        for kind in self.kinds or []:
            kind = str(kind).strip().lower()
            if kind in allowed and kind not in out:
                out.append(kind)
        return out or ["image", "video"]

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled, "kinds": list(self.kinds),
            "render_mode": self.render_mode, "scroll": self.scroll,
            "max_scroll": self.max_scroll, "wait_selector": self.wait_selector,
            "wait_timeout": self.wait_timeout,
            "follow_pagination": self.follow_pagination,
            "max_pages": self.max_pages, "max_depth": self.max_depth,
            "detail_link_pattern": self.detail_link_pattern,
            "min_width": self.min_width, "min_height": self.min_height,
            "min_size": self.min_size, "max_size": self.max_size,
            "allow_svg": self.allow_svg, "max_count": self.max_count,
            "dedup": self.dedup, "dedup_perceptual": self.dedup_perceptual,
            "upgrade_urls": self.upgrade_urls,
            "merge_stream": self.merge_stream,
            "stream_remux": self.stream_remux,
            "filename_template": self.filename_template,
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MediaConfig":
        data = data or {}
        kinds = data.get("kinds")
        if not isinstance(kinds, list):
            kinds = ["image", "video"]
        return cls(
            enabled=bool(data.get("enabled", False)),
            kinds=[str(k) for k in kinds],
            render_mode=str(data.get("render_mode", RENDER_AUTO)),
            scroll=bool(data.get("scroll", True)),
            max_scroll=int(data.get("max_scroll", 8) or 0),
            wait_selector=str(data.get("wait_selector", "")),
            wait_timeout=float(data.get("wait_timeout", 15.0) or 0.0),
            follow_pagination=bool(data.get("follow_pagination", True)),
            max_pages=int(data.get("max_pages", 5) or 0),
            max_depth=int(data.get("max_depth", 0) or 0),
            detail_link_pattern=str(data.get("detail_link_pattern", "")),
            min_width=int(data.get("min_width", 0) or 0),
            min_height=int(data.get("min_height", 0) or 0),
            min_size=int(data.get("min_size", 0) or 0),
            max_size=int(data.get("max_size", 0) or 0),
            allow_svg=bool(data.get("allow_svg", False)),
            max_count=int(data.get("max_count", 0) or 0),
            dedup=bool(data.get("dedup", True)),
            dedup_perceptual=bool(data.get("dedup_perceptual", True)),
            upgrade_urls=bool(data.get("upgrade_urls", True)),
            merge_stream=bool(data.get("merge_stream", False)),
            stream_remux=str(data.get("stream_remux", "auto") or "auto"),
            filename_template=str(data.get(
                "filename_template", "{album}/{index:03d}_{title}{ext}")),
            extra=dict(data.get("extra") or {}),
        )


__all__ = [
    "RENDER_STATIC", "RENDER_DYNAMIC", "RENDER_AUTO",
    "normalize_url", "url_fingerprint", "safe_name",
    "THUMB_RULES", "THUMB_QUERY_KEYS", "upgrade_thumbnail_url", "merge_items",
    "MediaItem", "MediaConfig",
]
