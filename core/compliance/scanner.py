"""合规辅助筛查编排线程（后台执行，信号驱动 UI）。

流程：robots 参考信息 → 发现公开法律页面 → 抓取正文 → 条款切分 →
本地分类 →（可选）LLM 辅助标注 → 结构化报告 → 落库 + 落盘。

线程安全与可中断：与 ``MediaCrawlThread`` 保持同一套约定——
``pause/resume/stop`` 由 GUI 线程调用，``_pause_event`` / ``_stop_event``
在抓取之间生效；抓取器自带间隔抖动与停止语义。

红线执行：本线程**只产出线索报告**，不改变任何抓取许可判断；
报告结果不会被回写到爬取流程中（robots.txt 仍是唯一的硬性闸门）。
"""

from __future__ import annotations

import threading
from urllib.parse import urljoin, urlparse

from PyQt6.QtCore import QThread, pyqtSignal

import config
from core.compliance import classifier, discovery, extract, llm
from core.compliance import report as report_mod
from core.compliance.models import ENGINE_LOCAL, GuardStats, LegalPage
from core.fetcher import FetcherUnavailable
from core.fetcher.playwright_fetcher import PlaywrightFetcher
from core.fetcher.requests_fetcher import RequestsFetcher
from core.robots import RobotsParser
from manager import db_manager

#: 静态提取正文短于该长度时，尝试用浏览器渲染兜底（应对 SPA 条款页）
RENDER_FALLBACK_MIN_CHARS = 200
#: 触发渲染兜底的页面数上限（浏览器成本高，宁愿少扫几页）
RENDER_FALLBACK_MAX_PAGES = 2

#: 页面级状态码 -> 可读原因。
#: 403 / 404 在扫描过程中很常见（条款入口迁移、站点挡爬虫），
#: 只记一句裸的 "HTTP 404" 无法判断该换入口还是该放慢节奏，因此统一翻译成
#: "为什么没拿到"的说明；这类页面**只跳过自身**，不影响已成功页面的结果。
_PAGE_STATUS_REASONS: dict[int, str] = {
    401: "HTTP 401：该页面需要登录才能访问（本功能只扫描公开页面）",
    403: "HTTP 403：站点拒绝访问该页面（可能启用了反爬校验或限制了访问来源）",
    404: "HTTP 404：页面不存在，条款入口可能已迁移",
    429: "HTTP 429：请求过于频繁被限流（可增大抓取间隔后重试）",
    451: "HTTP 451：因法律原因不可访问",
}


def page_failure_reason(status: int, error: str = "") -> str:
    """把页面抓取失败翻译成可读原因。

    未列入对照表的状态码回退到抓取器给出的原始错误文本，
    保证"原因"始终非空，便于在报告里如实交代该页为何未被纳入筛查。
    """
    reason = _PAGE_STATUS_REASONS.get(int(status or 0))
    if reason:
        return reason
    if error:
        return error
    return f"HTTP {status}" if status else "抓取失败"


class ComplianceScanThread(QThread):
    """合规辅助筛查后台线程。"""

    signal_log = pyqtSignal(str)
    signal_page = pyqtSignal(str, str)            # 页面URL + 结果说明
    signal_progress = pyqtSignal(int, int, int)   # 已扫页面, 候选页数, 已切条款数
    signal_finish = pyqtSignal(object)            # ComplianceReport
    signal_stopped = pyqtSignal(object)           # ComplianceReport（手动停止时的局部报告）
    signal_error = pyqtSignal(str)
    signal_paused = pyqtSignal()
    signal_resumed = pyqtSignal()

    def __init__(self, start_url: str = "", *, max_pages: int | None = None,
                 same_host_only: bool = True, respect_robots: bool = True,
                 min_confidence: float | None = None,
                 request_delay: float | None = None, jitter: float = 0.3,
                 proxy: dict | None = None, ua_pool=None, verify_ssl: bool = True,
                 page_timeout: float | None = None, max_page_bytes: int = 0,
                 render_fallback: bool = True, headless: bool = True,
                 executable_path: str = "", auto_extract_browser: bool = True,
                 llm_enabled: bool | None = None, llm_api_key: str = "",
                 save_report: bool = True, db_path: str = "", task_id: int = 0):
        super().__init__()
        self.start_url = str(start_url or "").strip()
        self.max_pages = int(config.COMPLIANCE_MAX_PAGES if max_pages is None
                             else max_pages)
        self.same_host_only = bool(same_host_only)
        self.respect_robots = bool(respect_robots)
        self.min_confidence = (config.COMPLIANCE_MIN_CONFIDENCE
                               if min_confidence is None else float(min_confidence))
        self.request_delay = max(0.0, float(
            config.COMPLIANCE_PAGE_DELAY if request_delay is None else request_delay))
        self.jitter = max(0.0, float(jitter or 0.0))
        self.proxy = proxy or {}
        self.ua_pool = ua_pool
        self.verify_ssl = bool(verify_ssl)
        self.page_timeout = max(5.0, float(
            config.COMPLIANCE_PAGE_TIMEOUT if page_timeout is None else page_timeout))
        self.max_page_bytes = max(0, int(max_page_bytes or 0))
        self.render_fallback = bool(render_fallback)
        self.headless = bool(headless)
        self.executable_path = executable_path
        self.auto_extract_browser = auto_extract_browser
        self.llm_enabled = config.LLM_ENABLED if llm_enabled is None else bool(llm_enabled)
        self.llm_api_key = llm_api_key or ""
        self.save_report = bool(save_report)
        self.db_path = db_path or config.default_db_path()
        self.task_id = int(task_id or 0)

        self._pause_event = threading.Event()
        self._pause_event.set()
        self._stop_event = threading.Event()
        self._fetchers: list = []
        self._render_fallbacks = 0
        self.report = None

    # ---------- 外部控制（GUI 线程调用） ----------
    def pause(self) -> None:
        """暂停扫描（在页面之间生效）。"""
        self._pause_event.clear()
        self.signal_paused.emit()

    def resume(self) -> None:
        """恢复扫描。"""
        self._pause_event.set()
        self.signal_resumed.emit()

    def is_paused(self) -> bool:
        return not self._pause_event.is_set()

    def stop(self) -> None:
        """请求停止（下一次分块读取或页面切换处退出）。"""
        self._stop_event.set()
        self._pause_event.set()   # 避免卡在暂停状态无法退出

    # ---------- 内部工具 ----------
    def _log(self, text: str) -> None:
        self.signal_log.emit(text)

    def _stopped(self) -> bool:
        return self._stop_event.is_set()

    def _wait_if_paused(self) -> None:
        self._pause_event.wait()

    def _make_fetcher(self, dynamic: bool):
        """创建并缓存抓取器（静态与动态各一个，复用连接池/浏览器）。"""
        for fetcher in self._fetchers:
            if isinstance(fetcher, PlaywrightFetcher) == dynamic:
                return fetcher
        if dynamic:
            fetcher = PlaywrightFetcher(
                ua_pool=self.ua_pool, proxy=self.proxy, verify_ssl=self.verify_ssl,
                request_delay=self.request_delay, jitter=self.jitter,
                headless=self.headless, page_timeout=self.page_timeout,
                executable_path=self.executable_path,
                auto_extract=self.auto_extract_browser,
                progress=self.signal_log.emit, stop_event=self._stop_event)
        else:
            fetcher = RequestsFetcher(
                ua_pool=self.ua_pool, proxy=self.proxy, verify_ssl=self.verify_ssl,
                request_delay=self.request_delay, jitter=self.jitter,
                max_bytes=self.max_page_bytes, stop_event=self._stop_event,
                connect_timeout=min(10.0, self.page_timeout),
                read_timeout=self.page_timeout)
        self._fetchers.append(fetcher)
        return fetcher

    def _close_fetchers(self) -> None:
        for fetcher in self._fetchers:
            try:
                fetcher.close()
            except Exception:
                pass
        self._fetchers = []

    # ---------- robots 参考信息 ----------
    def _load_robots(self, fetcher) -> tuple:
        """获取 robots.txt 并解析（仅作为报告中的参考信息项）。"""
        parsed = urlparse(self.start_url)
        robots_url = urljoin(f"{parsed.scheme}://{parsed.netloc}", "/robots.txt")
        try:
            ctx = fetcher.fetch(robots_url)
        except Exception as exc:
            return robots_url, None, f"robots.txt 获取异常：{type(exc).__name__}"
        if ctx.html:
            return robots_url, RobotsParser(ctx.html), \
                f"已获取 robots.txt（{len(ctx.html)} 字符）"
        # robots.txt 的 403/404 很常见且不影响本次筛查，单独给出更准确的说法
        if ctx.status == 404:
            return robots_url, None, "该站点未提供 robots.txt（HTTP 404），扫描继续"
        if ctx.status == 403:
            return robots_url, None, "站点拒绝访问 robots.txt（HTTP 403），扫描继续"
        return robots_url, None, (
            "未获取到 robots.txt（"
            f"{page_failure_reason(ctx.status, ctx.error)}），扫描继续")

    # ---------- 单页抓取 + 提取 ----------
    def _extract_page(self, url: str, page_seq: int) -> tuple:
        """抓取并提取一个法律页面。

        :return: (LegalPage, clauses, title)
        """
        fetcher = self._make_fetcher(dynamic=False)
        ctx = fetcher.fetch(url)
        page = LegalPage(url=url, final_url=ctx.final_url, status=ctx.status)
        if ctx.error or not ctx.html:
            page.error = page_failure_reason(ctx.status, ctx.error or "无内容")
            return page, [], ""
        title, text, clauses = extract.prepare_page(
            ctx.html, page_index=page_seq, page_url=url)
        # 渲染兜底：SPA 条款页在静态请求下正文极短，改用浏览器渲染再试一次
        if (len(text) < RENDER_FALLBACK_MIN_CHARS and self.render_fallback
                and self._render_fallbacks < RENDER_FALLBACK_MAX_PAGES
                and not self._stopped()):
            rendered = self._render_page(url, page_seq)
            if rendered is not None and len(rendered[1]) > len(text):
                title, text, clauses = rendered
                self._log(f"　已改用浏览器渲染并取到更长正文（{len(text)} 字符）")
        page.title = title
        page.chars = len(text)
        return page, clauses, text

    def _render_page(self, url: str, page_seq: int):
        """浏览器渲染兜底；不可用时返回 None（不视为失败）。"""
        try:
            fetcher = self._make_fetcher(dynamic=True)
        except FetcherUnavailable as exc:
            self._log(f"　渲染兜底不可用：{exc}")
            self.render_fallback = False
            return None
        self._render_fallbacks += 1
        if self._render_fallbacks == 1:
            self._log("　正在准备浏览器内核（首次可能需要解压，请稍候）…")
        try:
            ctx = fetcher.fetch(url, scroll=True, timeout=self.page_timeout)
        except Exception as exc:
            self._log(f"　渲染兜底失败：{type(exc).__name__}")
            return None
        if ctx.error or not ctx.html:
            self._log(f"　渲染兜底未取到内容：{ctx.error or '无内容'}")
            return None
        return extract.prepare_page(ctx.html, page_index=page_seq, page_url=url)

    # ---------- 主流程 ----------
    def run(self) -> None:
        try:
            report = self._run()
        except Exception as exc:
            self.signal_error.emit(f"合规筛查异常：{type(exc).__name__}: {str(exc)[:160]}")
            return
        finally:
            self._close_fetchers()
        if report is None:
            return
        self.report = report
        if self._stopped():
            self.signal_stopped.emit(report)
        else:
            self.signal_finish.emit(report)

    def _run(self):
        if not self.start_url.startswith(("http://", "https://")):
            self.signal_error.emit("合规筛查未提供有效的起始 URL")
            return None

        self._log("=== 合规辅助筛查启动 ===")
        self._log("　本功能只整理目标站点公开页面中的相关条款线索，用于辅助人工复核；"
                  "不构成法律意见，也不判断能否抓取。")
        self._log(f"　起始地址：{self.start_url}")
        self._log(f"　页面上限：{self.max_pages} | 抓取间隔：{self.request_delay}s"
                  f"（抖动 {self.jitter}）| robots 校验："
                  f"{'开' if self.respect_robots else '关'}")
        self._log(f"　LLM 辅助标注：{'开' if self.llm_enabled else '关'}"
                  f"（只发送候选条款片段）")

        if self._stopped():
            self._log("⏹ 任务在开始前已被停止，未发起任何请求")
            return report_mod.build_report(start_url=self.start_url)

        fetcher = self._make_fetcher(dynamic=False)
        robots_url, robots_parser, robots_note = self._load_robots(fetcher)
        self._log(f"　{robots_note}")

        # 1) 发现候选页面
        finder = discovery.LegalPageDiscovery(
            fetcher, max_pages=self.max_pages, same_host_only=self.same_host_only,
            respect_robots=self.respect_robots, log=self._log)
        seeds, skipped = finder.discover(
            self.start_url, robots_parser=robots_parser,
            user_agent=config.CRAWLER_USER_AGENT)
        skipped = list(skipped)
        if finder.skipped_total > len(skipped):
            skipped.append({"url": "（其余略）", "reason": (
                f"另有 {finder.skipped_total - len(skipped)} 项跳过未逐条列出"
                "（多为 robots 禁止或重复的站外链接）")})
        if not seeds:
            self._log("⚠ 未发现任何候选法律页面（可能是单页站点或条款位于外域/PDF）")
        else:
            self._log(f"　共发现 {len(seeds)} 个候选页面，开始逐页读取正文")

        # 2) 抓取 → 提取 → 切分
        pages: list = []
        clauses: list = []
        titles: dict = {}
        page_seq = 0
        total_seeds = len(seeds)
        for order, seed in enumerate(seeds, 1):
            if self._stopped():
                break
            self._wait_if_paused()
            if self._stopped():
                break
            self._log(f"[{order}/{total_seeds}] 读取：{seed.url}（{seed.source}）")
            page, page_clauses, _text = self._extract_page(seed.url, page_seq)
            page_seq += 1        # 无论成功与否都占用序号，保证条款 uid 唯一
            page.matched_by = seed.source
            pages.append(page)
            if page.error:
                # 单页失败不中断扫描：该页不纳入筛查，已获得的本地结果照常保留
                self._log(f"　✗ {page.error}（该页未纳入筛查，已获得的结果保留）")
                self.signal_page.emit(seed.url, f"已跳过：{page.error[:80]}")
            else:
                clauses.extend(page_clauses)
                titles[seed.url] = page.title
                self._log(f"　✓ {page.title or '（无标题）'} | 正文 {page.chars} 字符"
                          f" | 条款 {len(page_clauses)} 条")
                self.signal_page.emit(seed.url, f"已提取 {page.chars} 字符")
            self.signal_progress.emit(len(pages), total_seeds, len(clauses))

        if self._stopped():
            self._log("⏹ 已手动停止，以下结果基于已读取页面")

        # 3) 本地分类
        self._log(f"　开始条款分类（共 {len(clauses)} 条候选条款）")
        hits = classifier.classify_clauses(
            clauses, min_confidence=self.min_confidence, page_titles=titles)
        self._log(f"　本地关键词匹配完成：命中 {len(hits)} 条线索")

        # 4) LLM 辅助标注（可选，失败自动降级）
        guard_stats = GuardStats()
        llm_config = llm.LLMConfig.resolve(api_key=self.llm_api_key,
                                           enabled=self.llm_enabled)
        applied, engine, llm_status, guard_stats = llm.enrich(
            hits, llm_config=llm_config, guard_stats=guard_stats, log=self._log)
        if engine == ENGINE_LOCAL:
            self._log(f"　LLM 未参与：{llm_status}")
        else:
            self._log(f"　{llm_status}")
            self._log(f"　LLM 补充标注 {applied} 条（仅标签与说明，引文仍取本地原文）")

        # 5) 组装报告
        robots_summary = {
            "url": robots_url,
            "available": robots_parser is not None,
            "note": robots_note,
            "disallowed_for_our_ua": bool(
                robots_parser is not None and robots_parser.is_disallowed(
                    self.start_url, config.CRAWLER_USER_AGENT)),
            "disclaimer": "该项仅为 robots.txt 的客观记录，不构成可否抓取的结论。",
        }
        report = report_mod.build_report(
            start_url=self.start_url, pages=pages, skipped=skipped, findings=hits,
            robots_summary=robots_summary, engine=engine, llm_status=llm_status,
            guard_stats=guard_stats)
        self._log(f"　报告已生成：命中 {report.total_hits} 条线索，"
                  f"成功扫描 {report.pages_scanned} 页，跳过 {len(skipped)} 项")
        if guard_stats.intercepted:
            self._log(f"　红线机制：已拦截结论式表述 {guard_stats.intercepted} 处")
        if guard_stats.dropped_quotes:
            self._log(f"　红线机制：已丢弃无法回溯的引文 {guard_stats.dropped_quotes} 条")

        # 6) 落盘 + 落库
        if self.save_report:
            self._save(report)
        return report

    def _save(self, report) -> None:
        """报告落盘为 JSON 并登记入库（失败只提示，不影响本次结果）。"""
        file_path = ""
        try:
            file_path = report_mod.default_report_path(report.site)
            report.report_path = file_path     # 让 JSON 自身记录留档位置
            report_mod.write_report(file_path, "json", report)
            self._log(f"　报告已保存：{file_path}")
        except OSError as exc:
            self._log(f"　⚠ 报告落盘失败：{exc}")
        try:
            saved = db_manager.save_compliance_report(
                self.db_path, report, file_path=file_path, task_id=self.task_id)
            if not saved:
                self._log("　⚠ 报告入库失败（不影响本次结果）")
        except Exception as exc:
            self._log(f"　⚠ 报告入库失败：{type(exc).__name__}")


def default_report_file(report) -> str:
    """返回该报告已落盘的路径（未落盘时返回建议路径，供"另存为"使用）。"""
    existing = getattr(report, "report_path", "")
    return existing or report_mod.default_report_path(getattr(report, "site", ""))


def report_base_name(report) -> str:
    """报告默认文件名（供界面另存为对话框使用）。"""
    return report_mod.report_filename(getattr(report, "site", ""))


__all__ = [
    "ComplianceScanThread", "RENDER_FALLBACK_MIN_CHARS",
    "RENDER_FALLBACK_MAX_PAGES", "page_failure_reason",
    "default_report_file", "report_base_name",
]
