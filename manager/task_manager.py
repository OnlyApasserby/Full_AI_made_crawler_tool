"""多任务管理（多目标网站并发爬取）—— 核心：任务调度器 + 合并导出。

- 任务模型 / 优先级 / 依赖 / 队列持久化集中在 models 与本模块；
- 并发控制：每个运行任务 = 独立后台线程（递归爬取或媒体抓取），运行时只保留
  ``max_concurrent`` 个存活线程，其余按（优先级, ID）排队，容量释放后
  自动补位 —— 等价于可动态调整的线程池；另用 manager.thread_manager 的
  QThreadPool 承载队列/结果文件等异步落盘。
- 任务类型由 ``TaskConfig.media['enabled']`` 决定：
  ``False`` → ``RecursiveCrawlerThread``（页面文字与链接）；
  ``True``  → ``MediaCrawlThread``（图像/视频增强流程，支持渲染与 XHR 监听）。
- 所有线程信号经队列连接回 GUI 线程处理，状态变化通过信号通知 UI。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
from datetime import datetime
from functools import partial

from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal

from config import (
    DEFAULT_DOWNLOAD_DIR, DEFAULT_MEDIA_PAGE_TIMEOUT, DEFAULT_TASK_DATA_DIR,
    ROOT_DIR,
)
from models import (
    PRIORITY_TEXT, STATUS_TEXT, TaskConfig, TaskPriority, TaskStatus,
)
from utils.helpers import now_str, sanitize_name, url_domain
from utils.validators import valid_http_url
from utils.user_agents import UserAgentPool
from core.crawler import RecursiveCrawlerThread
from core.media.models import MediaConfig
from core.media_crawler import MediaCrawlThread
from core.robots import RobotsCheckThread, RobotsParser
from core.filter import URLFilter
from manager.db_manager import configure_wal, ensure_schema, save_media_items
from manager.thread_manager import ThreadPoolManager

# 终态 / 需要持久化的状态集合（模块私有）
_TERMINAL = {TaskStatus.COMPLETED, TaskStatus.FAILED}
_PERSIST_STATES = {TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.PAUSED}


class TaskScheduler(QObject):
    """任务队列 + 优先级 + 依赖 + 动态并发线程池调度器。"""

    task_added = pyqtSignal(int)
    task_changed = pyqtSignal(int)
    task_log_line = pyqtSignal(int, str)
    task_removed = pyqtSignal(int)
    summary_changed = pyqtSignal()

    def __init__(self, max_concurrent: int = 3, data_root: str | None = None,
                 base_download_dir: str | None = None, ua_pool=None,
                 queue_file: str | None = None):
        super().__init__()
        self._lock = threading.RLock()
        # 目录基准用应用目录（打包后为 exe 所在目录），避免依赖进程的当前工作目录
        self.data_root = data_root or os.path.join(ROOT_DIR, DEFAULT_TASK_DATA_DIR)
        os.makedirs(self.data_root, exist_ok=True)
        self.base_download_dir = base_download_dir or os.path.join(ROOT_DIR,
                                                                  DEFAULT_DOWNLOAD_DIR)
        self.queue_file = queue_file or os.path.join(self.data_root, "task_queue.json")

        self.max_concurrent = max(1, int(max_concurrent))
        self.ua_pool = ua_pool or UserAgentPool()
        self._io_pool = ThreadPoolManager(max_threads=4)

        self.tasks: dict[int, dict] = {}
        self._pending_ids: list[int] = []
        self._running: dict[int, QThread] = {}
        self._robots_threads: dict[int, QThread] = {}
        self._next_id = 1
        self._shutdown_flag = False

        # 进度刷新节流
        self._refresh_ids: set = set()
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(250)
        self._refresh_timer.timeout.connect(self._flush_refresh)

        # 队列防抖落盘
        self._persist_timer = QTimer(self)
        self._persist_timer.setSingleShot(True)
        self._persist_timer.setInterval(400)
        self._persist_timer.timeout.connect(self.save_queue)

        self._retry_timers: dict[int, QTimer] = {}

    # ---------------- 只读查询 ----------------
    def task(self, task_id: int) -> dict | None:
        with self._lock:
            rec = self.tasks.get(task_id)
            return dict(rec) if rec else None

    def has_running_tasks(self) -> bool:
        with self._lock:
            return bool(self._running) or bool(self._robots_threads)

    def set_max_concurrent(self, n: int):
        n = max(1, min(32, int(n)))
        with self._lock:
            changed = n != self.max_concurrent
            self.max_concurrent = n
        if changed:
            self._dispatch_next()

    def get_max_concurrent(self) -> int:
        return self.max_concurrent

    def summary(self) -> dict:
        with self._lock:
            s = {"total": len(self.tasks), TaskStatus.PENDING: 0, TaskStatus.RUNNING: 0,
                 TaskStatus.COMPLETED: 0, TaskStatus.PAUSED: 0, TaskStatus.FAILED: 0,
                 "pages": 0, "links": 0, "media": 0, "elapsed": 0.0}
            for rec in self.tasks.values():
                st = rec["status"]
                s[st] = s.get(st, 0) + 1
                s["pages"] += int(rec.get("pages", 0))
                s["links"] += int(rec.get("link_count", 0))
                s["media"] += int(rec.get("media_count", 0))
                s["elapsed"] += rec.get("duration", 0.0) or 0.0
            return s

    def task_metas(self) -> list[dict]:
        """轻量任务信息（不含页面文本等大对象），供对比/元信息展示"""
        with self._lock:
            metas = []
            for tid in sorted(self.tasks):
                rec = self.tasks[tid]
                cfg = rec["config"]
                metas.append({
                    "id": tid, "name": rec["name"], "start_url": cfg.start_url,
                    "status": rec["status"], "status_text": STATUS_TEXT.get(rec["status"], rec["status"]),
                    "priority_text": PRIORITY_TEXT.get(cfg.priority, "-"),
                    "pages": rec.get("pages", 0), "link_count": rec.get("link_count", 0),
                    "media_count": rec.get("media_count", 0),
                    "max_depth": cfg.max_depth, "current_depth": rec.get("current_depth", 0),
                    "progress": rec.get("progress", 0),
                    "retry_count": rec.get("retry_count", 0), "max_retries": cfg.max_retries,
                    "error": rec.get("error", ""), "depends_on": list(cfg.depends_on),
                    "created_at": rec.get("created_at", ""), "started_at": rec.get("started_at", ""),
                    "finished_at": rec.get("finished_at", ""), "duration": rec.get("duration", 0.0),
                    "db_path": rec.get("db_path", ""), "download_dir": rec.get("download_dir", ""),
                    "log_path": rec.get("log_path", ""), "media_urls": list(rec.get("media_urls", [])),
                })
            return metas

    def merged_results(self, task_ids: list[int] | None = None) -> list[dict]:
        """合并（全部/指定）任务结果：url + 文本 + 来源任务"""
        with self._lock:
            ids = task_ids or sorted(self.tasks)
            out = []
            for tid in ids:
                rec = self.tasks.get(tid)
                if not rec:
                    continue
                texts = rec.get("texts") or {}
                for url, text in texts.items():
                    out.append({"task_id": tid, "task_name": rec["name"],
                                "url": url, "text": text})
            return out

    # ---------------- 任务增删 ----------------
    def add_task(self, cfg: TaskConfig) -> int:
        if not valid_http_url(cfg.start_url):
            raise ValueError(f"无效的起始网址：{cfg.start_url}（需以 http:// 或 https:// 开头）")
        with self._lock:
            if self._shutdown_flag:
                raise RuntimeError("调度器已关闭，无法添加任务")
            tid = self._next_id
            self._next_id += 1
            name = cfg.task_name or url_domain(cfg.start_url) or f"任务{tid}"
            task_dir = os.path.join(self.data_root, f"task_{tid:03d}")
            os.makedirs(task_dir, exist_ok=True)
            download_dir = os.path.join(self.base_download_dir, sanitize_name(name))
            rec = {
                "id": tid, "config": cfg, "name": name, "status": TaskStatus.PENDING,
                "error": "", "retry_count": 0, "pages": 0, "current_depth": 0,
                "progress": 0, "link_count": 0, "media_count": 0,
                "texts": {}, "media_urls": [], "media_items": [],
                "created_at": now_str(), "started_at": "", "finished_at": "", "duration": 0.0,
                "crawler_task_id": None, "stopped_partial": False,
                "task_dir": task_dir, "db_path": os.path.join(task_dir, "crawl_cache.db"),
                "download_dir": download_dir, "log_path": os.path.join(task_dir, "task.log"),
                "results_path": os.path.join(task_dir, "results.json"), "log_lines": [],
            }
            # 任务级独立数据库：WAL 模式（多线程并发读写 + 与主库隔离）
            configure_wal(rec["db_path"])
            self.tasks[tid] = rec
            self._enqueue_pending(tid)
            self.task_added.emit(tid)
            self.summary_changed.emit()
            self._schedule_persist()
        self._dispatch_next()
        return tid

    def add_tasks(self, configs: list[TaskConfig]) -> list[int]:
        ids = []
        for cfg in configs:
            try:
                ids.append(self.add_task(cfg))
            except ValueError:
                continue
        return ids

    def remove_task(self, task_id: int, delete_files: bool = False) -> bool:
        with self._lock:
            rec = self.tasks.get(task_id)
            if not rec:
                return False
            thread = self._running.pop(task_id, None)
            rthread = self._robots_threads.pop(task_id, None)
        if thread is not None and thread.isRunning():
            try:
                thread.stop()
                thread.wait(4000)
            except Exception:
                pass
        if rthread is not None and rthread.isRunning():
            try:
                rthread.requestInterruption()
                rthread.wait(2000)
            except Exception:
                pass
        with self._lock:
            self.tasks.pop(task_id, None)
            if task_id in self._pending_ids:
                self._pending_ids.remove(task_id)
            task_dir = rec["task_dir"]
        self._log_task(task_id, f"任务已删除（{rec['name']}）")
        self.task_removed.emit(task_id)
        self.summary_changed.emit()
        if delete_files and os.path.isdir(task_dir):
            shutil.rmtree(task_dir, ignore_errors=True)
        self._schedule_persist()
        self._dispatch_next()
        return True

    # ---------------- 队列 ----------------
    def _enqueue_pending(self, task_id: int):
        if task_id in self._pending_ids:
            return
        rec = self.tasks.get(task_id)
        if not rec or rec["status"] != TaskStatus.PENDING:
            return
        self._pending_ids.append(task_id)
        self._pending_ids.sort(key=lambda i: (self.tasks[i]["config"].priority, i))

    def _deps_ready(self, rec: dict) -> bool:
        for dep in rec["config"].depends_on:
            dep_rec = self.tasks.get(dep)
            if dep_rec is None:
                continue  # 依赖任务已删除 -> 视为满足
            if dep_rec["status"] != TaskStatus.COMPLETED:
                return False
        return True

    def _dispatch_next(self):
        """并发容量空出/队列变化时，启动可执行的待执行任务"""
        started = []
        with self._lock:
            if self._shutdown_flag:
                return
            for tid in list(self._pending_ids):
                if len(self._running) >= self.max_concurrent:
                    break
                rec = self.tasks.get(tid)
                if not rec or not self._deps_ready(rec):
                    continue
                self._pending_ids.remove(tid)
                started.append(tid)
        for tid in started:
            self._begin_task(tid)

    # ---------------- 任务控制 ----------------
    def start_task(self, task_id: int):
        """启动（置队首优先派发）；PAUSED->恢复；FAILED->重试"""
        with self._lock:
            rec = self.tasks.get(task_id)
        if rec is None:
            return
        st = rec["status"]
        if st == TaskStatus.PAUSED:
            self.resume_task(task_id)
            return
        if st == TaskStatus.FAILED:
            self.retry_task(task_id)
            return
        if st != TaskStatus.PENDING:
            self._log_task(task_id, "当前状态不可直接启动")
            return
        with self._lock:
            if task_id in self._pending_ids:
                self._pending_ids.remove(task_id)
            self._pending_ids.insert(0, task_id)
        self._dispatch_next()

    def start_all(self):
        with self._lock:
            ids = [t for t, r in self.tasks.items() if r["status"] == TaskStatus.PENDING]
        for tid in ids:
            self.start_task(tid)

    def pause_task(self, task_id: int):
        with self._lock:
            thread = self._running.get(task_id)
            rec = self.tasks.get(task_id)
        if rec is None:
            return
        if thread is None or not thread.isRunning() or rec["status"] != TaskStatus.RUNNING:
            self._log_task(task_id, "只有执行中的任务可以暂停")
            return
        thread.pause()
        rec["status"] = TaskStatus.PAUSED
        self._log_task(task_id, "任务已暂停")
        self.task_changed.emit(task_id)
        self.summary_changed.emit()

    def pause_all(self):
        with self._lock:
            ids = [t for t, r in self.tasks.items() if r["status"] == TaskStatus.RUNNING]
        for tid in ids:
            self.pause_task(tid)

    def resume_task(self, task_id: int):
        """恢复暂停任务：线程存活则直接继续；已停止则以断点续爬重新启动"""
        with self._lock:
            thread = self._running.get(task_id)
            rec = self.tasks.get(task_id)
        if rec is None or rec["status"] != TaskStatus.PAUSED:
            if rec is not None:
                self._log_task(task_id, "只有暂停的任务可以恢复")
            return
        if thread is not None and thread.isRunning():
            thread.resume()
            rec["status"] = TaskStatus.RUNNING
            rec["started_at"] = now_str()
            self._log_task(task_id, "任务已恢复执行")
            self.task_changed.emit(task_id)
            self.summary_changed.emit()
        else:
            rec["status"] = TaskStatus.PENDING
            self._enqueue_pending(task_id)
            self._dispatch_next()

    def resume_all(self):
        with self._lock:
            ids = [t for t, r in self.tasks.items() if r["status"] == TaskStatus.PAUSED]
        for tid in ids:
            self.resume_task(tid)

    def stop_task(self, task_id: int):
        """手动停止：保留已爬数据，线程退出后状态置为暂停（可续爬）"""
        with self._lock:
            thread = self._running.get(task_id)
            rec = self.tasks.get(task_id)
        if rec is None or thread is None or not thread.isRunning():
            self._log_task(task_id, "没有正在执行的线程可停止")
            return
        thread.stop()
        self._log_task(task_id, "正在停止任务...")

    def stop_all(self):
        with self._lock:
            ids = list(self._running)
        for tid in ids:
            self.stop_task(tid)

    def retry_task(self, task_id: int):
        """手动重试失败任务"""
        with self._lock:
            rec = self.tasks.get(task_id)
        if rec is None or rec["status"] != TaskStatus.FAILED:
            if rec is not None:
                self._log_task(task_id, "只有失败的任务可以重试")
            return
        with self._lock:
            rec["retry_count"] = 0
            rec["error"] = ""
            rec["status"] = TaskStatus.PENDING
            self._enqueue_pending(task_id)
        self.task_changed.emit(task_id)
        self.summary_changed.emit()
        self._dispatch_next()

    def update_config(self, task_id: int, cfg: TaskConfig):
        """更新任务配置（仅对未执行任务生效；执行中的任务下次启动时生效）"""
        with self._lock:
            rec = self.tasks.get(task_id)
            if rec is None:
                return False
            if rec["status"] in (TaskStatus.RUNNING, TaskStatus.PAUSED):
                return False
            rec["config"] = cfg
        self._log_task(task_id, "任务配置已更新")
        self.task_changed.emit(task_id)
        return True

    def restart_task(self, task_id: int):
        """已完成/失败任务清空结果后重新全量爬取"""
        with self._lock:
            rec = self.tasks.get(task_id)
        if rec is None:
            return
        with self._lock:
            rec["status"] = TaskStatus.PENDING
            rec["error"] = ""
            rec["retry_count"] = 0
            rec["crawler_task_id"] = None
            rec["pages"] = 0
            rec["link_count"] = 0
            rec["media_count"] = 0
            rec["current_depth"] = 0
            rec["progress"] = 0
            rec["texts"] = {}
            rec["media_urls"] = []
            rec["media_items"] = []
            rec["started_at"] = ""
            rec["finished_at"] = ""
            rec["duration"] = 0.0
            self._enqueue_pending(task_id)
        self._log_task(task_id, "任务已重置，准备重新爬取")
        self.task_changed.emit(task_id)
        self.summary_changed.emit()
        self._dispatch_next()

    # ---------------- 执行流程 ----------------
    def _begin_task(self, task_id: int):
        """开始任务：任务级 robots.txt 预检通过后启动独立爬虫线程"""
        with self._lock:
            rec = self.tasks.get(task_id)
        if rec is None or self._shutdown_flag:
            return
        cfg = rec["config"]
        rec["status"] = TaskStatus.RUNNING
        rec["started_at"] = now_str()
        rec["error"] = ""
        rec["duration"] = 0.0
        self.task_changed.emit(task_id)
        self.summary_changed.emit()
        if cfg.robots_check:
            check = RobotsCheckThread(cfg.start_url)
            self._robots_threads[task_id] = check
            check.signal_robots_content.connect(partial(self._on_robots_result, task_id))
            check.signal_error.connect(partial(self._on_robots_error, task_id))
            check.finished.connect(partial(self._on_robots_finished, task_id))
            self._log_task(task_id, "正在检查该站点 robots.txt ...")
            check.start()
        else:
            self._start_crawler(task_id)

    def _on_robots_result(self, task_id: int, content: str, robots_url: str):
        with self._lock:
            rec = self.tasks.get(task_id)
        if rec is None:
            return
        cfg = rec["config"]
        parser = RobotsParser("" if (not content or "未提供" in content) else content)
        ua = self.ua_pool.random()
        if parser.is_disallowed(cfg.start_url, ua):
            self._log_task(task_id, f"robots.txt 禁止抓取 {cfg.start_url}，任务失败")
            self._fail_task(task_id, "robots.txt 禁止抓取该网址", retryable=False)
        else:
            if not content or "未提供" in content:
                self._log_task(task_id, "站点未提供 robots.txt，允许爬取")
            else:
                self._log_task(task_id, f"robots.txt 检查通过：{robots_url}")
            self._start_crawler(task_id)

    def _on_robots_error(self, task_id: int, err: str):
        """robots.txt 获取失败：记录警告并放行（避免网络波动阻塞任务）"""
        with self._lock:
            rec = self.tasks.get(task_id)
        if rec is None:
            return
        self._log_task(task_id, f"警告：{err}，跳过robots检查继续爬取")
        self._start_crawler(task_id)

    def _on_robots_finished(self, task_id: int):
        with self._lock:
            self._robots_threads.pop(task_id, None)

    def _start_crawler(self, task_id: int):
        """为任务启动独立 RecursiveCrawlerThread（并发容量校验 + 任务级过滤实例）"""
        with self._lock:
            if self._shutdown_flag:
                return
            if len(self._running) >= self.max_concurrent:
                rec = self.tasks.get(task_id)
                if rec:
                    rec["status"] = TaskStatus.PENDING
                    self._enqueue_pending(task_id)
                    self._log_task(task_id, "并发已达上限，任务回到待执行队列")
                    self.task_changed.emit(task_id)
                return
            rec = self.tasks.get(task_id)
            if rec is None:
                return
            cfg = rec["config"]
            task_filter = URLFilter(block_patterns=cfg.block_patterns,
                                    allow_domains=cfg.allow_domains)
            if cfg.is_media_task:
                thread, start_log = self._build_media_thread(rec, cfg, task_filter)
            else:
                thread, start_log = self._build_crawl_thread(rec, cfg, task_filter)
            self._running[task_id] = thread
            rec["status"] = TaskStatus.RUNNING
            rec["started_at"] = now_str()
            self._log_task(task_id, start_log)
            self.task_changed.emit(task_id)
            self.summary_changed.emit()
            thread.start()

    def _build_crawl_thread(self, rec: dict, cfg: TaskConfig, task_filter) -> tuple:
        """构建递归爬取线程并连接信号，返回 (线程, 启动日志)。"""
        thread = RecursiveCrawlerThread(
            start_url=cfg.start_url, max_depth=cfg.max_depth,
            crawl_external=cfg.crawl_external, request_delay=cfg.request_delay,
            max_chars_per_page=cfg.max_chars_per_page, link_filter=cfg.link_filter,
            db_path=rec["db_path"], ua_pool=self.ua_pool, jitter=cfg.jitter,
            proxy=cfg.proxy or {}, url_filter=task_filter, max_pages=cfg.max_pages,
            resume_task_id=rec["crawler_task_id"],
        )
        task_id = rec["id"]
        thread.signal_log.connect(partial(self._on_crawler_log, task_id))
        thread.signal_finish.connect(partial(self._on_task_finish, task_id))
        thread.signal_media_found.connect(partial(self._on_task_media, task_id))
        thread.signal_progress.connect(partial(self._on_task_progress, task_id))
        thread.signal_error.connect(partial(self._on_task_error, task_id))
        thread.signal_stopped.connect(partial(self._on_task_stopped, task_id))
        thread.finished.connect(partial(self._on_thread_finished, task_id))
        log_text = (f"启动递归爬取：{cfg.start_url}（深度 {cfg.max_depth}，"
                    f"间隔 {cfg.request_delay}s，"
                    f"站外链接：{'允许' if cfg.crawl_external else '禁止'}）")
        return thread, log_text

    def _build_media_thread(self, rec: dict, cfg: TaskConfig,
                            task_filter) -> tuple:
        """构建媒体抓取线程并连接信号，返回 (线程, 启动日志)。

        与爬虫线程的差异：``signal_progress`` / ``signal_finish`` /
        ``signal_stopped`` 的参数含义不同（页面数 / 媒体数 / 统计），
        因此使用独立的槽函数，避免语义串位。

        注意：媒体线程自身不读写数据库（爬虫线程会在 ``_init_db`` 中建表），
        因此这里需要显式准备任务级表结构，否则媒体元信息无法入库。
        """
        task_id = rec["id"]
        try:
            ensure_schema(rec["db_path"])
        except Exception:
            pass
        config = MediaConfig.from_dict(cfg.media)
        max_chars = max(0, int(cfg.max_chars_per_page or 0))
        thread = MediaCrawlThread(
            start_url=cfg.start_url, config=config,
            request_delay=cfg.request_delay, jitter=cfg.jitter,
            proxy=cfg.proxy or {}, ua_pool=self.ua_pool,
            url_filter=task_filter, page_timeout=DEFAULT_MEDIA_PAGE_TIMEOUT,
            max_page_bytes=max_chars * 4 if max_chars > 0 else 0,
        )
        thread.signal_log.connect(partial(self._on_crawler_log, task_id))
        thread.signal_page_done.connect(partial(self._on_media_page_done, task_id))
        thread.signal_media_found.connect(partial(self._on_task_media, task_id))
        thread.signal_media_items.connect(partial(self._on_media_task_items, task_id))
        thread.signal_progress.connect(partial(self._on_media_task_progress, task_id))
        thread.signal_finish.connect(partial(self._on_media_task_finish, task_id))
        thread.signal_stopped.connect(partial(self._on_media_task_stopped, task_id))
        thread.signal_error.connect(partial(self._on_task_error, task_id))
        thread.finished.connect(partial(self._on_thread_finished, task_id))
        log_text = (f"启动媒体抓取：{cfg.start_url}"
                    f"（目标 {'/'.join(config.target_kinds())}，"
                    f"模式 {config.render_mode}，页数上限 {config.max_pages or '不限'}）")
        return thread, log_text

    # ---------------- 爬虫信号 ----------------
    def _on_crawler_log(self, task_id: int, msg: str):
        self._log_task(task_id, msg)

    def _on_task_progress(self, task_id: int, pages: int, depth: int, max_depth: int):
        with self._lock:
            rec = self.tasks.get(task_id)
            if rec is None:
                return
            rec["pages"] = int(pages)
            rec["current_depth"] = int(depth)
            if max_depth >= 0:
                rec["progress"] = int(round((depth + 1) / (max_depth + 1) * 100))
            self._refresh_ids.add(task_id)
            if not self._refresh_timer.isActive():
                self._refresh_timer.start()

    def _flush_refresh(self):
        with self._lock:
            ids = list(self._refresh_ids)
            self._refresh_ids.clear()
        for tid in ids:
            self.task_changed.emit(tid)

    def _on_task_media(self, task_id: int, media: list):
        """任务媒体资源更新（爬取中逐页推送）：写入任务记录并刷新表格与汇总。"""
        with self._lock:
            rec = self.tasks.get(task_id)
            if rec is None:
                return
            rec["media_urls"] = list(media)
            rec["media_count"] = len(media)
        # 与进度刷新共用节流定时器，避免逐页重绘；媒体总数变化同步更新汇总统计
        self._refresh_ids.add(task_id)
        if not self._refresh_timer.isActive():
            self._refresh_timer.start()
        self.summary_changed.emit()

    def _on_task_finish(self, task_id: int, pages: int, links: list, texts: dict):
        with self._lock:
            rec = self.tasks.get(task_id)
            if rec is None:
                return
            rec["status"] = TaskStatus.COMPLETED
            rec["pages"] = int(pages)
            rec["link_count"] = len(links)
            rec["texts"] = dict(texts)
            rec["progress"] = 100
            rec["finished_at"] = now_str()
            rec["duration"] = self._elapsed_since(rec)
            running = self._running.get(task_id)
            rec["crawler_task_id"] = getattr(running, "task_id", rec["crawler_task_id"])
            self._write_results_async(rec, links, texts)
        self._log_task(task_id, f"任务完成：共爬取 {pages} 页，发现 {len(links)} 条链接，"
                                f"{rec.get('media_count', 0)} 个媒体资源")
        self._mark_terminal(task_id)

    def _on_task_stopped(self, task_id: int, pages: int, links: list, texts: dict):
        """手动停止：保留 crawler_task_id 供续爬，状态置为暂停"""
        with self._lock:
            rec = self.tasks.get(task_id)
            if rec is None:
                return
            rec["status"] = TaskStatus.PAUSED
            rec["pages"] = int(pages)
            rec["link_count"] = len(links)
            rec["texts"] = dict(texts)
            rec["stopped_partial"] = True
            rec["finished_at"] = now_str()
            rec["duration"] = self._elapsed_since(rec)
            running = self._running.get(task_id)
            rec["crawler_task_id"] = getattr(running, "task_id", rec["crawler_task_id"])
        self._log_task(task_id, f"任务已停止（已爬 {pages} 页），可从断点继续")

    # ---------------- 媒体抓取信号（参数语义与爬虫线程不同） ----------------
    def _on_media_page_done(self, task_id: int, page_url: str, info: str):
        """媒体抓取的单页结果：写入任务日志。"""
        self._log_task(task_id, f"[{page_url}] {info}")

    def _on_media_task_items(self, task_id: int, items: list):
        """媒体项列表更新：写入任务记录（落盘统一在结束/停止时进行）。"""
        with self._lock:
            rec = self.tasks.get(task_id)
            if rec is None:
                return
            rec["media_items"] = list(items)
            rec["media_count"] = len(items)
            self._refresh_ids.add(task_id)
            if not self._refresh_timer.isActive():
                self._refresh_timer.start()
        self.summary_changed.emit()

    def _on_media_task_progress(self, task_id: int, pages: int, media_count: int,
                                pending: int):
        """媒体抓取进度：页面数 / 媒体数 / 待抓队列。

        媒体抓取没有"层"的概念，进度按"已抓页面数 ÷ 页数上限"近似；
        未设置页数上限时保持 0（不虚构进度）。
        """
        with self._lock:
            rec = self.tasks.get(task_id)
            if rec is None:
                return
            rec["pages"] = int(pages)
            rec["media_count"] = int(media_count)
            rec["current_depth"] = int(pending)     # 复用字段展示"待抓页面"
            limit = int((rec["config"].media or {}).get("max_pages") or 0)
            if limit > 0:
                rec["progress"] = min(99, int(round(pages / limit * 100)))
            self._refresh_ids.add(task_id)
            if not self._refresh_timer.isActive():
                self._refresh_timer.start()

    def _on_media_task_finish(self, task_id: int, pages: int, media_count: int,
                              stats: str):
        """媒体抓取完成：入库媒体元信息 + 结果快照并置为已完成。"""
        self._finalize_media_task(task_id, pages, media_count, stats,
                                  completed=True)

    def _on_media_task_stopped(self, task_id: int, pages: int, media_count: int,
                               stats: str):
        """媒体抓取被手动停止：同样保留已发现结果。"""
        self._finalize_media_task(task_id, pages, media_count, stats,
                                  completed=False)

    def _finalize_media_task(self, task_id: int, pages: int, media_count: int,
                             stats: str, completed: bool):
        """媒体任务收尾：状态迁移 + 媒体元信息落盘 + 结果快照。"""
        with self._lock:
            rec = self.tasks.get(task_id)
            if rec is None:
                return
            rec["status"] = TaskStatus.COMPLETED if completed else TaskStatus.PAUSED
            rec["pages"] = int(pages)
            rec["media_count"] = int(media_count)
            rec["media_stats"] = str(stats)
            rec["progress"] = 100 if completed else rec.get("progress", 0)
            rec["finished_at"] = now_str()
            rec["duration"] = self._elapsed_since(rec)
            if not completed:
                rec["stopped_partial"] = True
            items = list(rec.get("media_items", []))
            db_path = rec["db_path"]
        if items:
            try:
                # 任务级数据库，与主库隔离；带 task_id 便于按任务查询
                save_media_items(db_path, items, task_id=task_id)
            except Exception:
                pass
        self._write_media_results_async(rec, items, stats)
        if completed:
            self._log_task(task_id, f"任务完成：抓取 {pages} 个页面，"
                                    f"{media_count} 个媒体资源 | {stats}")
            self._mark_terminal(task_id)
        else:
            self._log_task(task_id, f"任务已停止（已抓 {pages} 个页面，"
                                    f"{media_count} 个媒体资源），结果已保留")
            self.task_changed.emit(task_id)
            self.summary_changed.emit()

    def _write_media_results_async(self, rec: dict, items: list, stats: str):
        """异步写入媒体任务结果快照（结构区别于爬虫任务的 links/texts）。"""
        snapshot = {
            "task_id": rec["id"], "task_name": rec["name"], "type": "media",
            "start_url": rec["config"].start_url, "finished_at": now_str(),
            "stats": stats,
            "media_count": len(items),
            "media_urls": [item.url for item in items if hasattr(item, "url")],
            "media_items": [item.to_dict() if hasattr(item, "to_dict") else dict(item)
                            for item in items],
        }
        self._io_pool.submit(partial(self._dump_json, rec["results_path"], snapshot))

    def _on_task_error(self, task_id: int, err: str):
        self._log_task(task_id, f"任务出错：{err}")
        self._fail_task(task_id, str(err), retryable=True)

    def _on_thread_finished(self, task_id: int):
        """线程结束：释放并发槽位；异常结束（无完成/停止回调）视为失败"""
        with self._lock:
            thread = self._running.pop(task_id, None)
            rec = self.tasks.get(task_id)
        if rec is not None and rec["status"] == TaskStatus.RUNNING:
            rec["status"] = TaskStatus.FAILED
            rec["error"] = "线程异常结束（未收到完成回调）"
            rec["finished_at"] = now_str()
            rec["duration"] = self._elapsed_since(rec)
            self._log_task(task_id, "线程异常结束，任务失败")
            self._mark_terminal(task_id)
        elif rec is not None:
            self.task_changed.emit(task_id)
            self.summary_changed.emit()
        self._dispatch_next()

    # ---------------- 失败 / 重试 / 依赖级联 ----------------
    def _fail_task(self, task_id: int, reason: str, retryable: bool = True):
        with self._lock:
            rec = self.tasks.get(task_id)
        if rec is None or rec["status"] in _TERMINAL:
            return
        rec["status"] = TaskStatus.FAILED
        rec["error"] = reason
        rec["finished_at"] = now_str()
        rec["duration"] = self._elapsed_since(rec)
        retryable_ok = False
        if retryable:
            rec["retry_count"] += 1
            cfg_retries = rec["config"].max_retries
            retryable_ok = rec["retry_count"] <= cfg_retries
        self._log_task(task_id, f"失败原因：{reason}")
        if retryable_ok:
            rec["status"] = TaskStatus.PENDING
            self._enqueue_pending(task_id)
            backoff = 1.0 * (rec["retry_count"] + 1)
            self._log_task(task_id, f"将在 {backoff:.0f} 秒后自动重试（第 {rec['retry_count']}/"
                                    f"{rec['config'].max_retries} 次）")
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda: self._retry_trigger(task_id))
            self._retry_timers[task_id] = timer
            timer.start(int(backoff * 1000))
        else:
            self._log_task(task_id, "任务最终失败（超过重试次数或不可重试）")
            self._mark_terminal(task_id)
        self.task_changed.emit(task_id)
        self.summary_changed.emit()

    def _retry_trigger(self, task_id: int):
        with self._lock:
            self._retry_timers.pop(task_id, None)
        with self._lock:
            rec = self.tasks.get(task_id)
        if rec is not None and rec["status"] == TaskStatus.PENDING:
            self._dispatch_next()

    def _mark_terminal(self, task_id: int):
        """进入终态：汇总刷新 + 落盘 + 依赖级联（失败 -> 下游待执行任务失败）"""
        with self._lock:
            rec = self.tasks.get(task_id)
        self.task_changed.emit(task_id)
        self.summary_changed.emit()
        self._schedule_persist()
        if rec is not None and rec["status"] == TaskStatus.FAILED:
            dependents = []
            with self._lock:
                for other_id, other in self.tasks.items():
                    if (other["status"] == TaskStatus.PENDING
                            and task_id in other["config"].depends_on):
                        dependents.append(other_id)
            for other_id in dependents:
                self._fail_task(other_id, f"依赖任务（任务{task_id}）已失败", retryable=False)
        self._dispatch_next()

    # ---------------- 任务日志 ----------------
    def _log_task(self, task_id: int, msg: str):
        rec = self.tasks.get(task_id)
        if rec is None:
            return
        lines = rec["log_lines"]
        lines.append(f"[{now_str()}] {msg}")
        if len(lines) > 800:
            del lines[:-600]
        self.task_log_line.emit(task_id, msg)
        try:
            logger = logging.getLogger(f"mt_task_{task_id}")
            if not logger.handlers:
                logger.setLevel(logging.INFO)
                logger.propagate = False
                fh = logging.FileHandler(rec["log_path"], encoding="utf-8")
                fh.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", "%Y-%m-%d %H:%M:%S"))
                logger.addHandler(fh)
            logger.info("%s", msg)
        except Exception:
            pass

    # ---------------- 结果落盘 ----------------
    def _write_results_async(self, rec: dict, links: list, texts: dict):
        snapshot = {
            "task_id": rec["id"], "task_name": rec["name"],
            "start_url": rec["config"].start_url, "finished_at": now_str(),
            "pages": len(texts), "links": list(links), "texts": dict(texts),
            "media_urls": list(rec.get("media_urls", [])),
        }
        path = rec["results_path"]
        self._io_pool.submit(partial(self._dump_json, path, snapshot))

    @staticmethod
    def _dump_json(path: str, data: dict):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)

    # ---------------- 队列持久化 ----------------
    def _schedule_persist(self):
        if self._shutdown_flag:
            return
        self._persist_timer.start()

    def save_queue(self):
        """保存未完成任务（待执行/执行中/暂停）——异步落盘"""
        with self._lock:
            data = []
            for tid in sorted(self.tasks):
                rec = self.tasks[tid]
                if rec["status"] not in _PERSIST_STATES:
                    continue
                saved_status = (TaskStatus.PENDING
                                if rec["status"] in (TaskStatus.RUNNING, TaskStatus.PAUSED)
                                else rec["status"])
                data.append({
                    "config": rec["config"].to_dict(),
                    "status": saved_status,
                    "retry_count": rec.get("retry_count", 0),
                    "crawler_task_id": rec.get("crawler_task_id"),
                    "created_at": rec.get("created_at", ""),
                    "error": rec.get("error", ""),
                })
            payload = {"version": 1, "saved_at": now_str(), "tasks": data}
        try:
            self._io_pool.submit(partial(self._dump_json, self.queue_file, payload))
        except Exception:
            pass

    def load_queue(self, queue_file: str | None = None) -> int:
        """从队列文件恢复未完成任务（恢复为待执行状态，由用户启动）"""
        path = queue_file or self.queue_file
        if not os.path.exists(path):
            return 0
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception:
            return 0
        restored = 0
        for item in payload.get("tasks", []):
            try:
                cfg = TaskConfig.from_dict(item.get("config", {}))
                if not valid_http_url(cfg.start_url):
                    continue
                with self._lock:
                    if self._shutdown_flag:
                        break
                    tid = self._next_id
                    self._next_id += 1
                    name = cfg.task_name or url_domain(cfg.start_url) or f"任务{tid}"
                    task_dir = os.path.join(self.data_root, f"task_{tid:03d}")
                    os.makedirs(task_dir, exist_ok=True)
                    rec = {
                        "id": tid, "config": cfg, "name": name,
                        "status": TaskStatus.PENDING,
                        "error": item.get("error", ""),
                        "retry_count": int(item.get("retry_count", 0)),
                        "pages": 0, "current_depth": 0, "progress": 0,
                        "link_count": 0, "media_count": 0,
                        "texts": {}, "media_urls": [], "media_items": [],
                        "created_at": item.get("created_at", "") or now_str(),
                        "started_at": "", "finished_at": "", "duration": 0.0,
                        "crawler_task_id": item.get("crawler_task_id"),
                        "stopped_partial": bool(item.get("crawler_task_id")),
                        "task_dir": task_dir,
                        "db_path": os.path.join(task_dir, "crawl_cache.db"),
                        "download_dir": os.path.join(self.base_download_dir, sanitize_name(name)),
                        "log_path": os.path.join(task_dir, "task.log"),
                        "results_path": os.path.join(task_dir, "results.json"),
                        "log_lines": [],
                    }
                    self.tasks[tid] = rec
                    self._enqueue_pending(tid)
                self.task_added.emit(tid)
                self._log_task(tid, "任务已从队列文件恢复（点击\"开始\"可继续执行）")
                restored += 1
            except Exception:
                continue
        if restored:
            self.summary_changed.emit()
        return restored

    # ---------------- 关闭 ----------------
    def shutdown(self, save_queue: bool = True, wait_ms: int = 5000):
        """停止所有运行线程并保存队列（窗口关闭时调用）"""
        with self._lock:
            self._shutdown_flag = True
            running = dict(self._running)
            robots = dict(self._robots_threads)
        for tid, th in running.items():
            try:
                if th.isRunning():
                    th.stop()
            except Exception:
                pass
        for tid, th in robots.items():
            try:
                th.requestInterruption()
            except Exception:
                pass
        for tid, th in running.items():
            try:
                th.wait(wait_ms)
            except Exception:
                pass
        for tid, th in robots.items():
            try:
                th.wait(2000)
            except Exception:
                pass
        with self._lock:
            self._running.clear()
            self._robots_threads.clear()
            for rec in self.tasks.values():
                if rec["status"] == TaskStatus.RUNNING:
                    rec["status"] = TaskStatus.PENDING
        if save_queue:
            try:
                self.save_queue()
            except Exception:
                pass
        self._io_pool.wait_for_done(2000)

    @staticmethod
    def _elapsed_since(rec: dict) -> float:
        start = rec.get("started_at") or ""
        try:
            if not start:
                return 0.0
            return max(0.0, (datetime.now() - datetime.strptime(start, "%Y-%m-%d %H:%M:%S")).total_seconds())
        except Exception:
            return 0.0


# ---------------------------------------------------------------------------
# 导出（单任务 / 合并全部任务）
# ---------------------------------------------------------------------------
def export_tasks_to_file(rows: list[dict], filepath: str) -> int:
    """rows: [{"task_id","task_name","url","text"}, ...]；按扩展名选格式(csv/json/md)"""
    ext = os.path.splitext(filepath)[1].lower()
    if ext == ".json":
        payload = {"exported_at": now_str(), "count": len(rows), "tasks": rows}
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        return len(rows)
    if ext == ".md":
        lines = [f"# 爬虫任务汇总导出（{now_str()}）", "", f"共 {len(rows)} 条页面记录。", "",
                 "| 任务ID | 任务名 | 网址 | 页面字符数 |", "| --- | --- | --- | --- |"]
        for row in rows:
            text = row.get("text") or ""
            name = str(row.get("task_name", "")).replace("|", "/")
            lines.append(f"| {row.get('task_id')} | {name} | {row.get('url', '')} | {len(text)} |")
        with open(filepath, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return len(rows)
    import csv as _csv
    with open(filepath, "w", encoding="utf-8-sig", newline="") as f:
        writer = _csv.writer(f)
        writer.writerow(["任务ID", "任务名", "网址", "页面内容"])
        for row in rows:
            writer.writerow([row.get("task_id"), row.get("task_name"),
                             row.get("url"), row.get("text") or ""])
    return len(rows)


__all__ = ["TaskScheduler", "export_tasks_to_file"]
