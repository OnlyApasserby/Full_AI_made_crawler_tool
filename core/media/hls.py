"""HLS（m3u8）播放列表解析。

支持范围
--------
- **主播放列表**（``EXT-X-STREAM-INF``）：多码率变体，按 ``RESOLUTION`` → ``BANDWIDTH`` 选最优
- **媒体播放列表**：``EXTINF`` 分片、``EXT-X-BYTERANGE``（含"续上一片"的省略写法）、
  ``EXT-X-MAP``（fMP4 初始化段）、``EXT-X-KEY``（AES-128）、``EXT-X-MEDIA-SEQUENCE``
- **直播流**（无 ``ENDLIST``）：标记 ``live``，只合并当前可得分片

不支持（会给出明确提示，而不是静默产出坏文件）
- ``SAMPLE-AES`` 等需要取样本级解密的加密方式
- 播放列表内密钥轮换（同一播放列表出现多个不同的 ``EXT-X-KEY`` URI）

解析结果统一为 :class:`core.media.merger.MediaPlan`，交由 merger 下载合并。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

from core.media.merger import MediaPlan, Segment, StreamError

#: HLS 属性列表（形如 BANDWIDTH=1280000,CODECS="avc1,mp4a"）
_ATTR_RE = re.compile(r'([A-Za-z0-9\-]+)=("[^"]*"|[^,]*)')
#: 加密方式中需要样本级解密（本工具不支持）
_UNSUPPORTED_METHODS = {"SAMPLE-AES", "SAMPLE-AES-CTR"}


def _parse_attrs(text: str) -> dict:
    """解析 HLS 属性列表，返回大写键 → 去引号值。"""
    attrs: dict = {}
    for key, value in _ATTR_RE.findall(text or ""):
        attrs[key.upper()] = value.strip().strip('"')
    return attrs


def _lines(text: str) -> list:
    """按行切分并去掉空白行（HLS 规定标签与 URI 逐行排列）。"""
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def _resolution_score(resolution: str) -> int:
    """把 ``1920x1080`` 转成像素数，用于比较清晰度。"""
    if "x" not in (resolution or ""):
        return 0
    try:
        width, height = resolution.lower().split("x", 1)
        return int(width) * int(height)
    except (ValueError, AttributeError):
        return 0


@dataclass
class Variant:
    """主播放列表中的一路码流。"""

    url: str
    bandwidth: int = 0
    resolution: str = ""
    codecs: str = ""

    def describe(self) -> str:
        parts = [part for part in (self.resolution, f"{self.bandwidth}bps") if part]
        return " ".join(parts) or "未知码率"


@dataclass
class Playlist:
    """媒体播放列表（分片 + 加密信息）。"""

    segments: list = field(default_factory=list)      # list[merger.Segment]
    key_uri: str = ""
    key_iv: str = ""
    init_segment: Segment | None = None
    is_master: bool = False
    variants: list = field(default_factory=list)
    live: bool = False

    @property
    def total_duration(self) -> float:
        return sum(seg.duration for seg in self.segments)

    @property
    def is_encrypted(self) -> bool:
        return bool(self.key_uri)


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
def looks_like_master(text: str) -> bool:
    """是否为多码率主播放列表。"""
    return "#EXT-X-STREAM-INF" in (text or "")


def parse_master(text: str, base_url: str) -> list:
    """解析主播放列表，返回按清晰度升序的 :class:`Variant` 列表。"""
    lines = _lines(text)
    variants: list = []
    for index, line in enumerate(lines):
        if not line.upper().startswith("#EXT-X-STREAM-INF:"):
            continue
        attrs = _parse_attrs(line.split(":", 1)[1])
        uri = ""
        for follow in lines[index + 1:]:
            if not follow.startswith("#"):
                uri = follow
                break
            if follow.upper().startswith("#EXT-X-STREAM-INF:"):
                break
        if not uri:
            continue
        try:
            bandwidth = int(attrs.get("BANDWIDTH") or 0)
        except ValueError:
            bandwidth = 0
        variants.append(Variant(url=urljoin(base_url, uri), bandwidth=bandwidth,
                                resolution=attrs.get("RESOLUTION", ""),
                                codecs=attrs.get("CODECS", "")))
    return sorted(variants, key=lambda item: (_resolution_score(item.resolution),
                                              item.bandwidth))


def parse_media(text: str, base_url: str) -> Playlist:
    """解析媒体播放列表。

    :raises StreamError: 使用了本工具不支持的加密方式或出现密钥轮换
    """
    playlist = Playlist()
    duration = 0.0
    byte_range: tuple = ()
    last_range_end = 0
    media_sequence = 0
    key_uris: set = set()
    ended = False

    for line in _lines(text):
        upper = line.upper()
        if upper.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                media_sequence = int(line.split(":", 1)[1].strip())
            except ValueError:
                media_sequence = 0
        elif upper.startswith("#EXT-X-KEY:"):
            attrs = _parse_attrs(line.split(":", 1)[1])
            method = attrs.get("METHOD", "").upper()
            if method == "NONE":
                continue
            if method in _UNSUPPORTED_METHODS:
                raise StreamError(
                    f"该流使用 {method} 加密（样本级加密/DRM），本工具不支持；"
                    "可先用浏览器插件下载后再处理")
            uri = attrs.get("URI", "")
            if uri:
                absolute = urljoin(base_url, uri)
                playlist.key_uri = absolute
                playlist.key_iv = attrs.get("IV", "")
                key_uris.add(absolute)
        elif upper.startswith("#EXT-X-MAP:"):
            attrs = _parse_attrs(line.split(":", 1)[1])
            uri = attrs.get("URI", "")
            if uri:
                playlist.init_segment = Segment(
                    url=urljoin(base_url, uri),
                    byte_range=_parse_range(attrs.get("BYTERANGE", "")))
        elif upper.startswith("#EXT-X-BYTERANGE:"):
            byte_range = _parse_range(line.split(":", 1)[1], last_range_end)
            last_range_end = (byte_range[0] + byte_range[1]) if byte_range else 0
        elif upper.startswith("#EXTINF:"):
            head = line.split(":", 1)[1].split(",")[0].strip()
            try:
                duration = float(head)
            except ValueError:
                duration = 0.0
        elif upper.startswith("#EXT-X-ENDLIST"):
            ended = True
        elif line.startswith("#"):
            continue
        else:
            playlist.segments.append(Segment(
                url=urljoin(base_url, line), duration=duration,
                byte_range=byte_range,
                sequence=media_sequence + len(playlist.segments)))
            duration = 0.0
            byte_range = ()

    if len(key_uris) > 1:
        raise StreamError(f"播放列表内出现 {len(key_uris)} 个不同的密钥（密钥轮换），"
                          "本工具暂不支持，请改用支持轮换的下载器")
    playlist.live = not ended
    return playlist


def _parse_range(spec: str, previous_end: int = 0) -> tuple:
    """解析 ``BYTERANGE`` 属性：``length[@offset]``；省略 offset 时续上一片末尾。"""
    text = (spec or "").strip()
    if not text:
        return ()
    length_part, _, offset_part = text.partition("@")
    try:
        length = int(length_part)
    except ValueError:
        return ()
    if offset_part:
        try:
            offset = int(offset_part)
        except ValueError:
            return ()
    else:
        offset = previous_end
    return (max(0, offset), max(0, length))


def pick_best_variant(variants: list) -> Variant | None:
    """从多码率变体中选择最优一路（优先分辨率，其次带宽）。"""
    if not variants:
        return None
    return max(variants, key=lambda item: (_resolution_score(item.resolution),
                                           item.bandwidth))


# ---------------------------------------------------------------------------
# 对外主入口
# ---------------------------------------------------------------------------
def build_plan(text: str, base_url: str, *, fetch_text=None,
               progress=None, label: str = "") -> MediaPlan:
    """把 m3u8 文本解析为可下载的 :class:`MediaPlan`。

    若是主播放列表且提供了 ``fetch_text``，会自动选中最高清晰度并抓取其媒体播放列表。

    :raises StreamError: 需要抓取变体列表但未提供 ``fetch_text``、或列表为空
    """
    if looks_like_master(text):
        variants = parse_master(text, base_url)
        best = pick_best_variant(variants)
        if best is None:
            raise StreamError("主播放列表中未找到可用的码流")
        if fetch_text is None:
            raise StreamError("该地址是多码率主播放列表，需要先抓取具体码流")
        if progress:
            try:
                progress(f"　多码率主列表：{len(variants)} 路，选择最高清晰度"
                         f"（{best.describe()}）")
            except Exception:
                pass
        child_text = fetch_text(best.url)
        return build_plan(child_text, best.url, fetch_text=fetch_text,
                          progress=progress, label=best.describe())

    playlist = parse_media(text, base_url)
    if not playlist.segments:
        raise StreamError("播放列表中没有分片（可能是空列表或直播尚未开始）")
    if progress:
        try:
            progress(f"　分片列表：{len(playlist.segments)} 个"
                     f"（总时长约 {playlist.total_duration:.0f}s"
                     f"{'，加密流' if playlist.is_encrypted else ''}"
                     f"{'，直播流' if playlist.live else ''}）")
        except Exception:
            pass
    plan = MediaPlan(
        segments=playlist.segments,
        init_segment=playlist.init_segment,
        key_uri=playlist.key_uri,
        key_iv=playlist.key_iv,
        label=label,
        suffix=".mp4" if playlist.init_segment is not None else ".ts",
        needs_remux=playlist.init_segment is not None,
        live=playlist.live,
    )
    return plan


__all__ = [
    "Variant", "Playlist", "Segment", "parse_master", "parse_media",
    "pick_best_variant", "looks_like_master", "build_plan",
]
