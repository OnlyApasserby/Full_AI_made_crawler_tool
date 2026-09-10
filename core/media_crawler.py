"""媒体抓取编排线程 ``MediaCrawlThread``：把抓取层、提取器、翻页与流水线串起来。

一次媒体抓取任务的完整链路::

    入口 URL
      ├─ 抓取（静态 requests → 产出过少时自动升级为浏览器渲染 + XHR 监听）
      ├─ 提取（core/extractor：网络事件 / HTML / JSON 三路并行，按 URL 合并）
      ├─ 翻页（core/paginator：下一页链接 / 数字页码，逐页推进）
      ├─ 层级（可选：跟进列表页里的详情页链接，形成"列表→图集"两段式抓取）
      └─ 流水线（core/pipeline：去重 / 质量过滤 / 编号 / 限流）
          → MediaItem 列表 → 交给 ContentDownloader 下载

与 ``RecursiveCrawlerThread``（以文字与链接为目标的通用爬虫）的分工：
本线程只关心**媒体资源**，不做站内深度遍历，翻页与详情页跟随都是围绕
"把一套图/一组视频抓全"这一目标设计的。
"""

from __future__ import annotations

import re
import threading
from collections import deque
from urllib.parse import urljoin, urlparse

from PyQt6.QtCore import QThread, pyqtSignal

from config import DEFAULT_MEDIA_RENDER_WORKERS
from core.extractor import rule_extractor
from core.extractor.registry import default_extractors, extract_all
from core.fetcher import (
    MODE_AUTO, MODE_DYNAMIC, MODE_STATIC, BaseFetcher, FetchContext,
    FetcherUnavailable, PlaywrightFetcher, RequestsFetcher, playwright_status,
)
from core.media.detector import STREAM_TYPES, has_media_ext
from core.media.models import MediaConfig, MediaItem
from core.paginator import PageRule, Paginator
from core.pipeline import MediaPipeline

#: 静态抓取至少产出多少条媒体才认为"不必升级为渲染"
_AUTO_ESCALATE_MIN_MEDIA = 3
#: 兜底页数硬上限（防止 max_pages=0 时在无限翻页站点上失控）
_HARD_PAGE_CAP = 200
#: 页面内出现这些特征说明内容靠 JS 渲染/懒加载（触发 auto 升级）
_JS_SIGNAL_RE = re.compile(
    r"(?:data-src|data-original|lazyload|lazy-load|IntersectionObserver|"
    r"__NEXT_DATA__|__NUXT__|__INITIAL_STATE__|window\.__|vue|react|"
    r"infinite-scroll|waterfall)", re.IGNORECASE)
#: 详情页链接排除特征（导航/登录/帮助等非内容页）
_DETAIL_EXCLUDE_RE = re.compile(
    r"(?:login|logout|register|signup|sign-in|cart|account|user|help|about|"
    r"contact|privacy|terms|subscribe|rss|feed|share|comment|download|"
    r"javascript:|mailto:)", re.IGNORECASE)
#: 每个页面最多跟进的详情页链接数（防止列表页把队列撑爆）
_MAX_DETAIL_LINKS = 20

#: 并发浏览器渲染任务上限（每个 Chromium 约占 150–250MB，跨任务全局限流）
_RENDER_SLOTS = threading.BoundedSemaphore(max(1, int(DEFAULT_MEDIA_RENDER_WORKERS)))


class MediaCrawlThread(QThread):
    """媒体抓取后台线程。"""

    signal_log = pyqtSignal(str)
    signal_page_done = pyqtSignal(str, str)       # 页面URL + 结果说明
    signal_media_found = pyqtSignal(list)         # list[str]（兼容下载管理列表）
    signal_media_items = pyqtSignal(list)         # list[MediaItem]（元信息展示）
    signal_progress = pyqtSignal(int, int, int)   # 已抓页面, 已发现媒体, 队列剩余
    signal_finish = pyqtSignal(int, int, str)     # 页面数, 媒体数, 统计摘要
    signal_stopped = pyqtSignal(int, int, str)    # 同上（手动停止）
    signal_error = pyqtSignal(str)
    signal_paused = pyqtSignal()
    signal_resumed = pyqtSignal()

    def __init__(self, start_url: str = "", *, seed_urls=None,
                 config: MediaConfig | None = None, request_delay: float = 0.5,
                 jitter: float = 0.3, proxy: dict | None = None, ua_pool=None,
                 url_filter=None, page_rule: PageRule | None = None,
                 headless: bool = True, verify_ssl: bool = True,
                 page_timeout: float = 30.0, max_page_bytes: int = 0,
                 executable_path: str = "", auto_extract_browser: bool = True):
        super().__init__()
        self.config = config or MediaConfig()
        seeds = [url for url in (seed_urls or []) if url]
        if start_url:
            seeds.insert(0, start_url)
        self.seed_urls = [url for url in dict.fromkeys(seeds)
                          if str(url).startswith(("http://", "https://"))]
        self.request_delay = max(0.0, float(request_delay or 0.0))
        self.jitter = max(0.0, float(jitter or 0.0))
        self.proxy = proxy or {}
        self.ua_pool = ua_pool
        self.url_filter = url_filter
        self.page_rule = page_rule
        self.headless = headless
        self.verify_ssl = verify_ssl
        self.page_timeout = max(5.0, float(page_timeout or 30.0))
        self.max_page_bytes = max(0, int(max_page_bytes or 0))
        self.executable_path = executable_path
        self.auto_extract_browser = auto_extract_browser

        self.paginator = Paginator(page_rule, max_pages=self.config.max_pages)
        self.pipeline = MediaPipeline(self.config, url_filter)
        # 提取器实例复用：翻页多页共享，避免每页重复构建
        self.extractors = default_extractors(self.config)

        self._pause_event = threading.Event()
        self._pause_event.set()
        self._stop_event = threading.Event()
        self._visited: set = set()
        self._pages_fetched = 0
        self._fetchers: list = []
        self._render_slot = False
        self._stream_notice = False
        self._last_preview: tuple | None = None   # (ctx, 提取结果) 复用，避免重复提取

    # ---------- 外部控制（GUI 线程调用，线程安全） ----------
    def pause(self) -> None:
        """暂停抓取（在页面之间生效）。"""
        self._pause_event.clear()
        self.signal_paused.emit()

    def resume(self) -> None:
        """恢复抓取。"""
        self._pause_event.set()
        self.signal_resumed.emit()

    def is_paused(self) -> bool:
        return not self._pause_event.is_set()

    def stop(self) -> None:
        """请求停止。

        静态模式会在下一次分块读取处立即退出；动态模式下正在渲染的页面
        需等其超时（最长 ``page_timeout``）后退出——浏览器对象只能在创建它的
        线程里操作，跨线程强制关闭会破坏 playwright 的线程约束。
        """
        self._stop_event.set()

    # ---------- 内部工具 ----------
    def _log(self, text: str) -> None:
        self.signal_log.emit(text)

    def _make_fetcher(self, dynamic: bool) -> BaseFetcher:
        """创建抓取器（结果缓存，同一线程内复用浏览器与连接池）。"""
        for fetcher in self._fetchers:
            if isinstance(fetcher, PlaywrightFetcher) == dynamic:
                return fetcher
        if dynamic:
            fetcher = PlaywrightFetcher(
                ua_pool=self.ua_pool, proxy=self.proxy,
                verify_ssl=self.verify_ssl, request_delay=self.request_delay,
                jitter=self.jitter, headless=self.headless,
                page_timeout=self.page_timeout, wait_timeout=self.config.wait_timeout,
                executable_path=self.executable_path,
                auto_extract=self.auto_extract_browser,
                progress=self.signal_log.emit, stop_event=self._stop_event)
        else:
            fetcher = RequestsFetcher(
                ua_pool=self.ua_pool, proxy=self.proxy,
                verify_ssl=self.verify_ssl, request_delay=self.request_delay,
                jitter=self.jitter, max_bytes=self.max_page_bytes,
                stop_event=self._stop_event)
        self._fetchers.append(fetcher)
        return fetcher

    def _close_fetchers(self) -> None:
        for fetcher in self._fetchers:
            try:
                fetcher.close()
            except Exception:
                continue
        self._fetchers = []
        if self._render_slot:
            try:
                _RENDER_SLOTS.release()
            except ValueError:
                pass
            self._render_slot = False

    def _acquire_render_slot(self) -> bool:
        """占用一个全局渲染名额（跨任务限流，避免并发开多个浏览器）。"""
        if self._render_slot:
            return True
        if _RENDER_SLOTS.acquire(blocking=False):
            self._render_slot = True
            return True
        return False

    # ---------- 抓取（含 auto 升级） ----------
    def _render_mode(self) -> str:
        mode = (self.config.render_mode or MODE_AUTO).lower()
        if mode not in (MODE_STATIC, MODE_DYNAMIC, MODE_AUTO):
            return MODE_AUTO
        return mode

    def _fetch_page(self, url: str, depth: int) -> tuple:
        """抓取单个页面，返回 (FetchContext, 是否使用了渲染)。"""
        mode = self._render_mode()
        if mode == MODE_DYNAMIC:
            if not self._acquire_render_slot():
                self._log("⚠ 并发浏览器渲染任务已达上限，本次改用静态模式抓取")
                return self._make_fetcher(False).fetch(url), False
            try:
                return self._render(url, depth), True
            except FetcherUnavailable as exc:
                self._log(f"⚠ 动态渲染不可用，已退回静态模式：{str(exc).splitlines()[0]}")
                return self._make_fetcher(False).fetch(url), False

        ctx = self._make_fetcher(False).fetch(url)
        if mode != MODE_AUTO:
            return ctx, False
        if not self._should_escalate(ctx):
            return ctx, False
        if not self._acquire_render_slot():
            self._log("　→ 页面疑似 JS 渲染，但渲染名额已满，保留静态结果")
            return ctx, False
        available, reason = playwright_status()
        if not available:
            self._log(f"　→ 页面疑似 JS 渲染，但浏览器不可用：{str(reason).splitlines()[0]}")
            return ctx, False
        self._log("　→ 静态结果偏少且页面含懒加载/JS 特征，升级为浏览器渲染重抓")
        try:
            rendered = self._render(url, depth)
        except FetcherUnavailable as exc:
            self._log(f"　→ 渲染失败，保留静态结果：{str(exc).splitlines()[0]}")
            return ctx, False
        if rendered.ok or rendered.network_events:
            return rendered, True
        return ctx, False

    def _render(self, url: str, depth: int) -> FetchContext:
        """用浏览器渲染抓取（滚动触发懒加载）。"""
        referer = self.seed_urls[0] if depth > 0 and self.seed_urls else ""
        return self._make_fetcher(True).fetch(
            url, wait_selector=self.config.wait_selector,
            scroll=self.config.scroll, max_scroll=self.config.max_scroll,
            timeout=self.config.wait_timeout if self.config.wait_selector else None,
            referer=referer)

    def _should_escalate(self, ctx: FetchContext) -> bool:
        """判断是否值得从静态升级为渲染（少做无谓的浏览器开销）。"""
        if ctx.media_direct:
            return False
        if not ctx.ok:
            # 静态抓取失败（超时/被拦截）时尝试渲染一次
            return bool(ctx.error) and "已停止" not in ctx.error
        html = ctx.html or ""
        if len(html) < 800:
            return False
        produced = len(self._preview_extract(ctx))
        if produced >= _AUTO_ESCALATE_MIN_MEDIA:
            return False
        return bool(_JS_SIGNAL_RE.search(html))

    def _preview_extract(self, ctx: FetchContext) -> list:
        """用现有提取器试提取一次（结果缓存，供升级判断与正式提取共用）。"""
        cached = self._last_preview
        if cached is not None and cached[0] is ctx:
            return cached[1]
        try:
            items = extract_all(ctx, self.config, self.extractors)
        except Exception:
            items = []
        self._last_preview = (ctx, items)
        return items

    # ---------- 提取 ----------
    def _extract(self, ctx: FetchContext) -> list:
        """提取 + 投入流水线，返回本次新增通过的媒体项。"""
        items = self._preview_extract(ctx)
        if not items and ctx.media_direct:
            return []
        album_hint = ""
        for item in items:
            if item.album:
                album_hint = item.album
                break
        self.pipeline.refresh_blocks()
        passed = self.pipeline.add(items, album_hint=album_hint)
        self._notice_streams(passed)
        return passed

    def _notice_streams(self, items) -> None:
        """识别到流媒体时给出明确提示（分片合并为可选功能，默认不启用）。"""
        if self._stream_notice:
            return
        streams = [item for item in items if item.kind in STREAM_TYPES]
        if not streams:
            return
        self._stream_notice = True
        self._log(f"🎞 识别到 {len(streams)} 个流媒体（m3u8/mpd）。"
                  f"分片合并为可选功能，当前{'已启用' if self.config.merge_stream else '未启用'}；"
                  f"未启用时仅登记地址（下载得到的是播放列表文件）")

    def _handle_media_direct(self, ctx: FetchContext, url: str) -> bool:
        """页面本身即媒体直链：直接登记为媒体项，不做 HTML 解析。"""
        if not ctx.media_direct:
            return False
        source = self.seed_urls[0] if self.seed_urls else ""
        item = MediaItem(url=url, kind=ctx.kind_hint,
                         source_page=source if source != url else "", origin="direct",
                         extra={"content_type": ctx.content_type})
        passed = self.pipeline.add([item])
        self._notice_streams(passed)
        self._log(f"　→ 该地址本身是媒体文件（{ctx.content_type or ctx.kind_hint}），已登记")
        return True

    # ---------- 详情页链接 ----------
    def _detail_links(self, ctx: FetchContext) -> list:
        """从页面中挑选"详情页"候选链接（列表页 → 图集详情页）。

        优先级：站点规则里的选择器（精准）→ 通用启发式（同站 + 排除导航路径）。
        """
        if self.config.max_depth <= 0:
            return []
        html = ctx.html or ""
        if not html:
            return []
        base = ctx.base_url
        # 1. 站点规则：规则里配置了 detail_links 时优先使用
        rule_links = [url for url in rule_extractor.detail_links_for(base, html)
                      if url not in self._visited]
        if rule_links:
            self._log(f"　→ 站点规则提取到 {len(rule_links)} 个详情页链接")
            return rule_links[:_MAX_DETAIL_LINKS]

        # 2. 通用启发式
        base_host = (urlparse(base).hostname or "").lower()
        pattern = (self.config.detail_link_pattern or "").strip()
        found: list = []
        for match in re.finditer(r"""<a\b[^>]*\bhref\s*=\s*["']([^"']+)["']""",
                                 html, re.IGNORECASE):
            raw = match.group(1).strip()
            if not raw or raw.startswith("#"):
                continue
            try:
                candidate = urljoin(base, raw).split("#", 1)[0]
            except ValueError:
                continue
            if not candidate.startswith(("http://", "https://")):
                continue
            if candidate in self._visited or candidate in found:
                continue
            host = (urlparse(candidate).hostname or "").lower()
            if not host or not (host == base_host or host.endswith("." + base_host)
                                or base_host.endswith("." + host)):
                continue
            if has_media_ext(candidate):        # 已经是媒体直链，不走详情页
                continue
            if _DETAIL_EXCLUDE_RE.search(candidate):
                continue
            if pattern and pattern not in candidate:
                continue
            found.append(candidate)
            if len(found) >= _MAX_DETAIL_LINKS:
                break
        return found

    # ---------- 主循环 ----------
    def run(self) -> None:
        try:
            self._run()
        except Exception as exc:  # 兜底：异常也要把已发现的媒体交出去
            self._emit_media()
            self.signal_error.emit(f"媒体抓取异常：{type(exc).__name__}: {str(exc)[:160]}")
        finally:
            self._close_fetchers()

    def _run(self) -> None:
        if not self.seed_urls:
            self.signal_error.emit("媒体抓取未提供有效的起始 URL")
            return
        config = self.config
        mode = self._render_mode()
        mode_text = {MODE_STATIC: "静态请求", MODE_DYNAMIC: "浏览器渲染",
                     MODE_AUTO: "自动（静态优先，必要时渲染）"}.get(mode, mode)
        self._log("=== 媒体抓取任务启动 ===")
        self._log(f"　目标类型：{'/'.join(config.target_kinds())} | 抓取模式：{mode_text}")
        self._log(f"　起始地址：{self.seed_urls[0]}"
                  f"（共 {len(self.seed_urls)} 个）| 最大页数："
                  f"{config.max_pages or '不限'} | 翻页：{'开' if config.follow_pagination else '关'}")
        self._apply_site_rules()

        queue = deque((url, 0) for url in self.seed_urls)
        max_pages = max(0, int(config.max_pages or 0))
        # 页数硬上限：未配置上限时用 _HARD_PAGE_CAP 兜底，避免无限翻页站点失控
        page_cap = max_pages or _HARD_PAGE_CAP
        stopped = False

        while queue:
            if self._stop_event.is_set():
                stopped = True
                break
            self._pause_event.wait()
            if self._pages_fetched >= page_cap:
                self._log(f"⏹ 已达页数上限（{page_cap}），停止抓取"
                          + ("" if max_pages else "（未配置上限，使用兜底值）"))
                break
            url, depth = queue.popleft()
            if url in self._visited:
                continue
            self._visited.add(url)
            self._pages_fetched += 1

            self._log(f"[{self._pages_fetched}] 抓取页面（层级 {depth}）：{url}")
            ctx, rendered = self._fetch_page(url, depth)
            if self._stop_event.is_set():
                stopped = True
                break
            if ctx.error and not ctx.media_direct:
                self._log(f"❌ 页面抓取失败：{ctx.error[:120]}")
                self.signal_page_done.emit(url, f"失败：{ctx.error[:120]}")
                self._emit_progress(queue)
                continue

            # 直链媒体：登记后无需解析页面
            if self._handle_media_direct(ctx, url):
                self.signal_page_done.emit(url, f"媒体直链（{ctx.content_type or '未知类型'}）")
                self._emit_progress(queue)
                continue

            new_items = self._extract(ctx)
            info = (f"媒体新增 {len(new_items)}（累计 {self.pipeline.count}）"
                    f"{'｜渲染' if rendered else '｜静态'}"
                    f"｜网络事件 {len(ctx.network_events)}")
            self._log(f"　→ {info}")
            self.signal_page_done.emit(url, info)

            # 翻页：同一个入口的后续页面
            if config.follow_pagination and self._pages_fetched < page_cap:
                for page_url in self.paginator.next_pages(
                        ctx, self._visited, page_index=self._pages_fetched):
                    if page_url not in self._visited:
                        queue.append((page_url, depth))
            # 详情页：列表页 → 图集详情页
            if config.max_depth > 0 and depth < config.max_depth:
                details = self._detail_links(ctx)
                for detail_url in details:
                    if detail_url not in self._visited:
                        queue.append((detail_url, depth + 1))
                if details:
                    self._log(f"　→ 跟进 {len(details)} 个详情页（层级 {depth + 1}）")

            self._emit_progress(queue)

        stats = self.pipeline.stats.summary()
        self._log(f"📊 媒体抓取汇总：页面 {self._pages_fetched} 个 | {stats}")
        if self.pipeline.stats.samples:
            self._log("　过滤样本：" + "；".join(
                f"{url[:60]}（{reason}）" for url, reason in
                self.pipeline.stats.samples[:5]))
        self._emit_media()
        if stopped:
            self._log("⏹ 媒体抓取已手动停止，已发现资源已保留")
            self.signal_stopped.emit(self._pages_fetched, self.pipeline.count, stats)
        else:
            self.signal_finish.emit(self._pages_fetched, self.pipeline.count, stats)

    def _apply_site_rules(self) -> None:
        """应用命中的站点规则：翻页配置优先于通用推断（未显式指定时）。"""
        try:
            matched = rule_extractor.matching_rules(self.seed_urls[0])
        except Exception:
            return
        if not matched:
            return
        self._log("　命中站点规则：" + "、".join(
            (rule.name or rule.id) for rule in matched))
        if self.page_rule is not None:
            return
        rule_page = rule_extractor.page_rule_for(self.seed_urls[0])
        if rule_page is not None:
            self.paginator = Paginator(rule_page, max_pages=self.config.max_pages)
            self._log(f"　应用规则翻页配置：strategy={rule_page.strategy}"
                      + (f" | template={rule_page.template}" if rule_page.template else ""))

    # ---------- 上报 ----------
    def _emit_media(self) -> None:
        """推送全量媒体快照（URL 列表 + MediaItem 列表）。"""
        items = self.pipeline.result()
        self.signal_media_found.emit([item.url for item in items])
        self.signal_media_items.emit(items)

    def _emit_progress(self, queue) -> None:
        self.signal_progress.emit(self._pages_fetched, self.pipeline.count, len(queue))

    # ---------- 结果访问 ----------
    def items(self) -> list:
        """当前已收集的媒体项（任务结束后取最终结果）。"""
        return self.pipeline.result()


__all__ = ["MediaCrawlThread"]
