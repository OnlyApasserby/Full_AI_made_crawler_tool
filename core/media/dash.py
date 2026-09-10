"""DASH（mpd）清单解析。

支持范围
--------
- **SegmentTemplate**：``$Number$`` / ``$Time$`` / ``$RepresentationID$`` / ``$Bandwidth$``
  占位符与 ``%0Nd`` 补零写法
- **SegmentTimeline**：``S@t`` / ``S@d`` / ``S@r``（含 ``r=-1`` 重复到片段结束）
- **SegmentList**：``Initialization@sourceURL`` + ``SegmentURL@media``
- **SegmentBase**：整文件单分片
- **BaseURL 层级**：MPD → Period → AdaptationSet → Representation 逐级拼接
- **音视频分离**：分别选出最优视频轨与音频轨，由 ffmpeg 合流
  （``core.media.merger.plan`` 在无 ffmpeg 时只保留视频轨并明确提示）

设计取舍
--------
DASH 的媒体分片是 fMP4 片段，必须带上初始化段才能播放，因此解析结果统一
标记 ``needs_remux=True``，交给 merger 用 ffmpeg 转封装为 MP4。
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import urljoin

from core.media.merger import MediaPlan, Segment, StreamError

#: ISO8601 时长（PT1H2M3.5S / P1DT2H）
_DURATION_RE = re.compile(
    r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?)?", re.IGNORECASE)
#: SegmentTemplate 占位符
_NUMBER_RE = re.compile(r"\$Number(?:%0(\d+)d)?\$")
_TIME_RE = re.compile(r"\$Time(?:%0(\d+)d)?\$")


def _local(tag) -> str:
    """取去除命名空间前缀的标签名。"""
    return str(tag).rsplit("}", 1)[-1]


def _children(element, name: str) -> list:
    """按标签名（忽略命名空间）取子元素。"""
    return [child for child in list(element) if _local(child.tag) == name]


def _child(element, name: str):
    """取第一个匹配的子元素，无则返回 None。"""
    found = _children(element, name)
    return found[0] if found else None


def _parse_duration(text: str) -> float:
    """解析 ISO8601 时长为秒（无法解析返回 0）。"""
    match = _DURATION_RE.fullmatch((text or "").strip())
    if not match:
        return 0.0
    days, hours, minutes, seconds = match.groups()
    try:
        return (int(days or 0) * 86400 + int(hours or 0) * 3600
                + int(minutes or 0) * 60 + float(seconds or 0))
    except ValueError:
        return 0.0


def _child_base(element, parent_base: str) -> str:
    """把元素内的 <BaseURL> 拼接到父级基准地址上。"""
    node = _child(element, "BaseURL")
    text = (node.text or "").strip() if node is not None else ""
    if not text:
        return parent_base
    return urljoin(parent_base, text)


def _fill(template: str, rep: "Representation", number=None,
          time_value=None) -> str:
    """填充 SegmentTemplate 占位符。"""
    text = template or ""
    text = text.replace("$RepresentationID$", rep.id or "")
    text = text.replace("$Bandwidth$", str(rep.bandwidth or 0))

    def replace_number(match):
        width = match.group(1)
        value = number if number is not None else 0
        return str(value).zfill(int(width)) if width else str(value)

    def replace_time(match):
        width = match.group(1)
        value = time_value if time_value is not None else 0
        return str(value).zfill(int(width)) if width else str(value)

    text = _NUMBER_RE.sub(replace_number, text)
    text = _TIME_RE.sub(replace_time, text)
    return text


@dataclass
class Representation:
    """一条码流（不同清晰度 / 音视频轨）。"""

    id: str = ""
    bandwidth: int = 0
    width: int = 0
    height: int = 0
    mime_type: str = ""
    codecs: str = ""
    content_type: str = ""          # video / audio
    segments: list = field(default_factory=list)      # list[merger.Segment]
    init_segment: Segment | None = None

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}" if self.width and self.height else ""

    def describe(self) -> str:
        parts = [part for part in (self.resolution, f"{self.bandwidth}bps",
                                   self.codecs) if part]
        return " ".join(parts) or "未知码率"


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
def parse_mpd(text: str, base_url: str) -> list:
    """解析 MPD 清单，返回全部 :class:`Representation`（含音视频轨）。

    :raises StreamError: XML 非法或清单中没有可用码流
    """
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise StreamError(f"MPD 清单解析失败：{exc}") from exc
    if _local(root.tag) != "MPD":
        raise StreamError("不是合法的 MPD 清单（根元素不是 MPD）")

    mpd_duration = _parse_duration(root.attrib.get("mediaPresentationDuration", ""))
    mpd_base = _child_base(root, base_url)
    representations: list = []

    for period in _children(root, "Period"):
        period_duration = _parse_duration(period.attrib.get("duration", "")) or mpd_duration
        period_base = _child_base(period, mpd_base)
        for adaptation in _children(period, "AdaptationSet"):
            adapt_base = _child_base(adaptation, period_base)
            adapt_content = (adaptation.attrib.get("contentType")
                             or adaptation.attrib.get("mimeType", "")).lower()
            adapt_mime = adaptation.attrib.get("mimeType", "")
            adapt_template = _child(adaptation, "SegmentTemplate")
            adapt_list = _child(adaptation, "SegmentList")
            for rep_node in _children(adaptation, "Representation"):
                rep = _build_representation(
                    rep_node, adaptation, adapt_base, period_duration,
                    adapt_template, adapt_list, adapt_content, adapt_mime)
                if rep is not None:
                    representations.append(rep)

    if not representations:
        raise StreamError("MPD 清单中没有可用的码流")
    return representations


def _build_representation(rep_node, adaptation, base_url: str, period_duration: float,
                          adapt_template, adapt_list, adapt_content: str,
                          adapt_mime: str) -> Representation | None:
    """从 Representation 节点（继承 AdaptationSet 的模板）构建码流描述。"""
    attributes = rep_node.attrib
    mime = attributes.get("mimeType") or adapt_mime
    content = (adapt_content
               or ("video" if mime.lower().startswith("video/") else "")
               or ("audio" if mime.lower().startswith("audio/") else ""))
    try:
        rep = Representation(
            id=attributes.get("id", ""),
            bandwidth=int(attributes.get("bandwidth") or 0),
            width=int(attributes.get("width") or 0),
            height=int(attributes.get("height") or 0),
            mime_type=mime, codecs=attributes.get("codecs") or adaptation.attrib.get("codecs", ""),
            content_type=content)
    except ValueError:
        return None

    rep_base = _child_base(rep_node, base_url)
    # 模板可定义在 AdaptationSet（公共属性）与 Representation（覆盖）两处，需要合并
    template_node = _child(rep_node, "SegmentTemplate")
    template = _merged_attributes(adapt_template, template_node)
    list_node = _child(rep_node, "SegmentList") or adapt_list
    base_node = _child(rep_node, "SegmentBase") or _child(adaptation, "SegmentBase")

    if template and template.get("media"):
        rep.init_segment = _init_from_template(template, rep, rep_base)
        rep.segments = _segments_from_template(template, template_node, rep,
                                              rep_base, period_duration)
    elif list_node is not None:
        rep.init_segment, rep.segments = _segments_from_list(list_node, rep_base)
    elif rep_base and (base_node is not None or not _child(rep_node, "SegmentTemplate")):
        # SegmentBase / 单文件：BaseURL 本身就是媒体文件
        if rep_base != base_url or _child(rep_node, "BaseURL") is not None:
            rep.segments = [Segment(url=rep_base, sequence=1)]
    if not rep.segments:
        return None
    return rep


def _merged_attributes(parent_node, child_node) -> dict:
    """合并父子两级 SegmentTemplate 属性（子级覆盖父级）。"""
    merged: dict = {}
    for node in (parent_node, child_node):
        if node is None:
            continue
        for key, value in node.attrib.items():
            merged[key] = value
    return merged


def _init_from_template(template: dict, rep: Representation, base_url: str):
    """按 SegmentTemplate 的 initialization 属性生成初始化段。"""
    init_template = template.get("initialization", "")
    if not init_template:
        return None
    return Segment(url=urljoin(base_url, _fill(init_template, rep)))


def _segments_from_template(template: dict, template_node, rep: Representation,
                            base_url: str, period_duration: float) -> list:
    """按 SegmentTemplate 生成分片列表（优先 SegmentTimeline，其次固定时长）。"""
    media_template = template.get("media", "")
    if not media_template:
        return []
    try:
        start_number = int(template.get("startNumber") or 1)
    except ValueError:
        start_number = 1
    try:
        timescale = int(template.get("timescale") or 1) or 1
    except ValueError:
        timescale = 1
    try:
        segment_duration = float(template.get("duration") or 0)
    except ValueError:
        segment_duration = 0.0

    timeline = _child(template_node, "SegmentTimeline") if template_node is not None else None
    segments: list = []

    if timeline is not None:
        number = start_number
        time_value = 0
        for entry in _children(timeline, "S"):
            if entry.attrib.get("t") is not None:
                try:
                    time_value = int(entry.attrib["t"])
                except ValueError:
                    pass
            try:
                duration = int(entry.attrib.get("d") or 0)
                repeat = int(entry.attrib.get("r") or 0)
            except ValueError:
                continue
            if duration <= 0:
                continue
            repeat = max(0, repeat)
            for _ in range(repeat + 1):
                segments.append(Segment(
                    url=urljoin(base_url, _fill(media_template, rep, number, time_value)),
                    duration=duration / timescale, sequence=number))
                time_value += duration
                number += 1
        return segments

    # 固定时长：按片段总时长推算分片数量
    if segment_duration <= 0 or period_duration <= 0:
        raise StreamError(
            "SegmentTemplate 缺少 SegmentTimeline 与 duration，无法推算分片列表")
    count = max(1, int(math.ceil(period_duration * timescale / segment_duration)))
    for index in range(count):
        number = start_number + index
        segments.append(Segment(
            url=urljoin(base_url, _fill(media_template, rep, number, None)),
            duration=segment_duration / timescale, sequence=number))
    return segments


def _segments_from_list(list_node, base_url: str) -> tuple:
    """按 SegmentList 生成初始化段与分片列表。"""
    init_node = _child(list_node, "Initialization")
    init_segment = None
    if init_node is not None and init_node.attrib.get("sourceURL"):
        init_segment = Segment(url=urljoin(base_url, init_node.attrib["sourceURL"]))
    segments: list = []
    for index, node in enumerate(_children(list_node, "SegmentURL")):
        media = node.attrib.get("media")
        if not media:
            continue
        segments.append(Segment(url=urljoin(base_url, media), sequence=index + 1))
    return init_segment, segments


def pick_best(representations: list) -> Representation | None:
    """选择最优码流（优先分辨率，其次带宽）。"""
    if not representations:
        return None
    return max(representations,
               key=lambda rep: (rep.width * rep.height, rep.bandwidth))


# ---------------------------------------------------------------------------
# 对外主入口
# ---------------------------------------------------------------------------
def build_plan(representations: list, *, max_height: int = 0) -> MediaPlan:
    """把码流列表整理为可下载的 :class:`MediaPlan`（视频轨 + 可选音频轨）。

    :param max_height: 清晰度上限（0=不限，取最高）
    :raises StreamError: 没有可用码流
    """
    if not representations:
        raise StreamError("MPD 清单中没有码流")

    def is_video(rep: Representation) -> bool:
        return rep.content_type == "video" or (not rep.content_type and rep.height > 0)

    def is_audio(rep: Representation) -> bool:
        return rep.content_type == "audio" or "audio/" in (rep.mime_type or "").lower()

    videos = [rep for rep in representations if is_video(rep)]
    audios = [rep for rep in representations if is_audio(rep)]
    if max_height > 0:
        limited = [rep for rep in videos if rep.height and rep.height <= max_height]
        videos = limited or videos

    chosen = pick_best(videos) or pick_best(audios)
    if chosen is None:
        raise StreamError("MPD 清单中没有可识别的音视频码流")

    plan = MediaPlan(
        segments=list(chosen.segments),
        init_segment=chosen.init_segment,
        label=chosen.describe(),
        suffix=".mp4",
        needs_remux=True,       # fMP4 分片需 ffmpeg 转封装/合流后才能顺畅播放
    )
    if audios:
        audio = pick_best(audios)
        if audio is not None and audio is not chosen and audio.segments:
            plan.audio_plan = MediaPlan(
                segments=list(audio.segments), init_segment=audio.init_segment,
                label=audio.describe(), suffix=".mp4", needs_remux=False)
    return plan


__all__ = ["Representation", "parse_mpd", "pick_best", "build_plan"]
