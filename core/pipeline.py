"""媒体流水线：去重、质量过滤、编号与限流。

媒体抓取的"最后一公里"：提取器产出的候选往往包含大量噪声
（缩略图与原图并存、CDN 多域名同一张图、埋点图标、SVG 图标、超小占位图），
本模块负责把它们收敛成一份干净、可直接下载的清单。

过滤顺序（从便宜到昂贵）：
1. 协议 / 类型（图片、视频、音频是否为目标类型）
2. SVG 与类型不匹配
3. 屏蔽正则（复用 ``core.filter.URLFilter`` 的**屏蔽规则**；
   域名白名单不作用于媒体——图片常托管在第三方 CDN 上）
4. URL 归一化去重（``utm_*`` 等追踪参数视为同一资源）+ 同资源信息合并
5. 已知元数据的尺寸/体积过滤（未知时留给下载后 :func:`check_local_file` 兜底）
6. 合集内编号 + 总量限流

下载后的最终质量校验由 :func:`check_local_file` 提供（下载器在落盘后调用），
两处共用同一份 ``MediaConfig``，保证"提前过滤"与"落地校验"口径一致。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from core.media.detector import MEDIA_TYPES, TYPE_DASH, TYPE_HLS, TYPE_VIDEO
from core.media.models import MediaConfig, MediaItem, merge_items
from core.media.probe import HAS_PIL, dhash, is_duplicate_hash, probe_local
from utils.helpers import format_size

#: 目标类型 → 实际可能出现的资源类型（流媒体在配置里算作 video）
_KIND_GROUPS = {
    "image": {"image"},
    "video": {"video", TYPE_HLS, TYPE_DASH},
    "audio": {"audio"},
}
#: 被拒绝样本的保留上限（日志排查用）
_MAX_SAMPLES = 20


@dataclass
class FilterStats:
    """过滤统计（用于日志与界面展示）。"""

    total: int = 0          # 进入流水线的候选总数
    kept: int = 0           # 最终保留
    duplicate: int = 0      # 归一化后重复
    wrong_kind: int = 0     # 类型不符合目标
    svg: int = 0            # SVG 图标（默认排除）
    blocked: int = 0        # 命中屏蔽正则
    too_small: int = 0      # 尺寸/体积过小
    too_large: int = 0      # 体积超限
    truncated: int = 0      # 达到条数上限后丢弃
    samples: list = field(default_factory=list)   # [(url, 原因)]

    def reject(self, url: str, reason: str) -> None:
        """登记一条被拒绝的记录（计数由调用方负责）。"""
        if len(self.samples) < _MAX_SAMPLES:
            self.samples.append((url, reason))

    def summary(self) -> str:
        """单行摘要，便于写入日志。"""
        parts = [f"候选 {self.total}", f"保留 {self.kept}"]
        for label, value in (("重复", self.duplicate), ("类型不符", self.wrong_kind),
                             ("SVG", self.svg), ("被拦截", self.blocked),
                             ("过小", self.too_small), ("超限", self.too_large),
                             ("超出条数", self.truncated)):
            if value:
                parts.append(f"{label} {value}")
        return " | ".join(parts)

    def merge(self, other: "FilterStats") -> None:
        """累加另一份统计（多页汇总）。"""
        for name in ("total", "kept", "duplicate", "wrong_kind", "svg", "blocked",
                     "too_small", "too_large", "truncated"):
            setattr(self, name, getattr(self, name) + getattr(other, name, 0))
        room = _MAX_SAMPLES - len(self.samples)
        if room > 0:
            self.samples.extend(other.samples[:room])


class MediaPipeline:
    """媒体去重与质量过滤流水线（一次媒体抓取任务一个实例）。"""

    def __init__(self, config: MediaConfig | None = None, url_filter=None):
        """
        :param config: 媒体抓取配置（目标类型 / 尺寸 / 条数 / 去重开关）
        :param url_filter: 可选 :class:`core.filter.URLFilter`，只取其**屏蔽正则**
        """
        self.config = config or MediaConfig()
        self.url_filter = url_filter
        self.stats = FilterStats()
        self._items: dict[str, MediaItem] = {}      # 归一化指纹 -> 媒体项
        self._album_counters: dict[str, int] = {}
        self._compiled_blocks: list = []
        self._blocks_dirty = True

    # ---------- 屏蔽规则 ----------
    def refresh_blocks(self) -> int:
        """重新读取屏蔽正则（每页调用一次即可，规则改动实时生效）。"""
        patterns = []
        if self.url_filter is not None:
            try:
                patterns = self.url_filter.block_patterns()
            except Exception:
                patterns = []
        compiled = []
        for pattern in patterns:
            try:
                compiled.append((pattern, re.compile(pattern)))
            except re.error:
                continue
        self._compiled_blocks = compiled
        self._blocks_dirty = False
        return len(compiled)

    def _blocked_reason(self, url: str) -> str:
        if self._blocks_dirty:
            self.refresh_blocks()
        for pattern, regex in self._compiled_blocks:
            try:
                if regex.search(url):
                    return f"屏蔽正则 {pattern}"
            except re.error:
                continue
        return ""

    # ---------- 目标类型 ----------
    def _target_kinds(self) -> set:
        wanted: set = set()
        for kind in self.config.target_kinds():
            wanted |= _KIND_GROUPS.get(kind, set())
        return wanted

    # ---------- 主流程 ----------
    def add(self, items, album_hint: str = "") -> list:
        """把候选媒体加入流水线，返回本次**新增通过**的条目（已编号）。

        :param items: 可迭代的 :class:`MediaItem`
        :param album_hint: 未提供 album 时的兜底合集名（通常是页面标题）
        """
        passed: list = []
        wanted = self._target_kinds()
        max_count = max(0, int(self.config.max_count or 0))
        for item in items or []:
            if item is None or not item.url:
                continue
            self.stats.total += 1
            if not item.url.startswith(("http://", "https://")):
                self.stats.wrong_kind += 1
                self.stats.reject(item.url, "非 http(s) 地址")
                continue
            if item.kind not in MEDIA_TYPES:
                self.stats.wrong_kind += 1
                self.stats.reject(item.url, f"类型 {item.kind} 非媒体")
                continue
            if wanted and item.kind not in wanted:
                self.stats.wrong_kind += 1
                self.stats.reject(item.url, f"类型 {item.kind} 不在目标范围")
                continue
            if item.ensure_ext().lower() == ".svg" and not self.config.allow_svg:
                self.stats.svg += 1
                self.stats.reject(item.url, "SVG 图标（默认排除）")
                continue
            reason = self._blocked_reason(item.url)
            if reason:
                self.stats.blocked += 1
                self.stats.reject(item.url, reason)
                continue

            key = item.key()
            existing = self._items.get(key)
            if existing is not None:
                # 同一资源（含追踪参数差异）：合并元信息，不重复入列
                self.stats.duplicate += 1
                merge_items(existing, item)
                continue

            if max_count and len(self._items) >= max_count:
                self.stats.truncated += 1
                continue

            if not self._passes_metadata(item):
                continue

            self._assign_naming(item, album_hint)
            self._items[key] = item
            self.stats.kept += 1
            passed.append(item)
        return passed

    def _passes_metadata(self, item: MediaItem) -> bool:
        """基于**已知**元数据做尺寸/体积过滤（未知值不拦截，交由下载后校验）。"""
        min_size = max(0, int(self.config.min_size or 0))
        max_size = max(0, int(self.config.max_size or 0))
        if item.size > 0:
            if min_size and item.size < min_size:
                self.stats.too_small += 1
                self.stats.reject(item.url, f"体积 {format_size(item.size)} 小于下限")
                return False
            if max_size and item.size > max_size:
                self.stats.too_large += 1
                self.stats.reject(item.url, f"体积 {format_size(item.size)} 超过上限")
                return False
        min_width = max(0, int(self.config.min_width or 0))
        min_height = max(0, int(self.config.min_height or 0))
        if item.width and min_width and item.width < min_width:
            self.stats.too_small += 1
            self.stats.reject(item.url, f"宽度 {item.width} 小于下限")
            return False
        if item.height and min_height and item.height < min_height:
            self.stats.too_small += 1
            self.stats.reject(item.url, f"高度 {item.height} 小于下限")
            return False
        return True

    def _assign_naming(self, item: MediaItem, album_hint: str) -> None:
        """补齐合集名，并**统一重排**合集内序号。

        序号必须由流水线统一分配而非沿用提取器的文档序号：
        同一合集往往由多页（翻页）与多来源（HTML + 网络事件）共同贡献，
        只有统一编号才能保证落盘文件名稳定、不重号。
        提取器原始序号保留在 ``extra['doc_index']`` 便于溯源。
        """
        if not item.album:
            item.album = (album_hint or "").strip()
        if not item.album:
            try:
                item.album = (urlparse(item.source_page or item.url).netloc
                              .replace(":", "_") or "media")
            except ValueError:
                item.album = "media"
        if item.index:
            item.extra.setdefault("doc_index", item.index)
        counter = self._album_counters.get(item.album, 0) + 1
        self._album_counters[item.album] = counter
        item.index = counter

    # ---------- 结果 ----------
    @property
    def count(self) -> int:
        """当前保留的媒体条数。"""
        return len(self._items)

    def result(self) -> list:
        """全部保留条目（按合集 + 序号排序，作为下载与命名顺序）。"""
        return sorted(self._items.values(), key=lambda it: (it.album, it.index))

    def urls(self) -> list:
        """全部保留条目的 URL 列表（兼容旧下载流程的纯 URL 入参）。"""
        return [item.url for item in self.result()]

    # ---------- 感知哈希去重（下载后） ----------
    def perceptual_duplicates(self, paths) -> dict:
        """对已落盘文件做感知哈希分组（近似同图）。

        :param paths: 文件路径可迭代对象
        :return: ``{重复文件路径: 已保留文件路径}``
        """
        if not (HAS_PIL and self.config.dedup_perceptual):
            return {}
        groups: list = []       # [(哈希, 代表路径)]
        duplicates: dict = {}
        for path in paths or []:
            digest = dhash(path)
            if not digest:
                continue
            for known_hash, keeper in groups:
                if is_duplicate_hash(digest, known_hash):
                    if os.path.abspath(keeper) != os.path.abspath(path):
                        duplicates[path] = keeper
                    break
            else:
                groups.append((digest, path))
        return duplicates


def check_local_file(path: str, config: MediaConfig | None = None) -> tuple:
    """下载后的本地质量校验（尺寸/体积/SVG）。

    与 :meth:`MediaPipeline.add` 共用同一份配置，构成"提前过滤 + 落地校验"闭环：
    提取阶段拿不到尺寸信息（懒加载地址、无 Content-Length 的接口直链）时，
    在这里兜底剔除。

    :return: (是否合格, 不合格原因)；合格时原因为空串
    """
    config = config or MediaConfig()
    if not path or not os.path.isfile(path):
        return False, "文件不存在"
    ext = os.path.splitext(path)[1].lower()
    if ext == ".svg" and not config.allow_svg:
        return False, "SVG 图标（默认排除）"
    try:
        size = os.path.getsize(path)
    except OSError:
        return False, "无法读取文件大小"
    if config.min_size and size < int(config.min_size):
        return False, f"体积 {format_size(size)} 小于下限 {format_size(config.min_size)}"
    if config.max_size and size > int(config.max_size):
        return False, f"体积 {format_size(size)} 超过上限 {format_size(config.max_size)}"
    if (config.min_width or config.min_height) and ext in (".jpg", ".jpeg", ".png",
                                                          ".gif", ".webp", ".bmp",
                                                          ".avif", ".ico"):
        width, height = probe_local(path)
        if width and config.min_width and width < int(config.min_width):
            return False, f"宽度 {width} 小于下限 {config.min_width}"
        if height and config.min_height and height < int(config.min_height):
            return False, f"高度 {height} 小于下限 {config.min_height}"
    return True, ""


__all__ = ["FilterStats", "MediaPipeline", "check_local_file"]
