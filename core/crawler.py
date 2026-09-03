"""递归爬取后台线程（原 RecursiveCrawlerThread）。

广度优先（BFS）递归爬取 + 任务级 SQLite 持久化（断点续爬）：
- 会话内 / 数据库双去重（重启后自动跳过已爬页面）
- 暂停 / 继续 / 手动停止（可中断进行中的 HTTP 请求）
- 随机 UA + 代理 + 请求间隔抖动（反爬策略）
- URL 过滤规则实时生效（共享 core.filter.URLFilter 实例）
- 页面解析（链接 / 媒体 / 纯文字）委托 core.parser
- 数据库读写集中在 manager/db_manager
"""

from __future__ import annotations

import hashlib
import os
import random
import socket
import threading
import time
from urllib.parse import urlparse

import requests
from PyQt6.QtCore import QThread, pyqtSignal

from core.parser import extract_links, extract_media_links, extract_text
from core.filter import URLFilter
from utils.user_agents import UserAgentPool
from manager.db_manager import (
    ensure_schema, load_visited_hashes, create_crawl_task,
    finish_crawl_task, save_crawled_url,
)


class RecursiveCrawlerThread(QThread):
    """递归爬取后台线程。"""

    signal_log = pyqtSignal(str)
    signal_single_page_done = pyqtSignal(str, str)  # 完成的url + 页面内容
    signal_finish = pyqtSignal(int, list, dict)  # 总爬取页数 + 所有收集到的链接列表 + 页面文字字典{url: text}
    signal_media_found = pyqtSignal(list)  # 本次爬取发现的媒体资源URL列表
    signal_progress = pyqtSignal(int, int, int)  # 已爬页面数, 当前层, 总层数
    signal_error = pyqtSignal(str)
    signal_paused = pyqtSignal()   # 进入暂停状态
    signal_resumed = pyqtSignal()  # 恢复运行状态
    signal_stopped = pyqtSignal(int, list, dict)  # 手动停止：已爬页数 + 链接列表 + 页面文字字典

    def __init__(self, start_url, max_depth, crawl_external=False, request_delay=0.0,
                 max_chars_per_page=0, link_filter="", db_path="crawler_cache.db",
                 ua_pool=None, jitter=0.3, proxy=None, url_filter=None, max_pages=0,
                 resume_task_id=None):
        super().__init__()
        self.start_url = start_url
        self.max_depth = max_depth
        self.crawl_external = crawl_external
        self.request_delay = request_delay
        self.max_chars_per_page = max_chars_per_page  # 0 表示不限制
        self.link_filter = link_filter  # 定向爬取关键词，空表示不限制
        self.db_path = db_path  # sqlite3 持久化文件
        self.ua_pool = ua_pool or UserAgentPool()  # 随机UA池
        self.jitter = jitter  # 请求间隔随机抖动幅度（0~1，实际间隔=设定间隔*(1+抖动)）
        self.proxy = proxy or {}  # 代理配置 {"http":..., "https":...}
        # URL过滤规则（与GUI共享同一实例，规则变化实时生效，内部线程安全）
        self.url_filter = url_filter or URLFilter()
        self.max_pages = max_pages  # 自动停止：最大爬取页数（0表示不限制）
        self.visited_urls = set()
        self.all_extracted_links = set()
        self.page_contents = {}  # url -> 页面提取的纯文字内容
        self.media_urls = set()  # 页面中提取到的图片/视频等媒体资源
        # 暂停/继续控制：Event set=运行，clear=暂停，wait()会阻塞
        self._pause_event = threading.Event()
        self._pause_event.set()
        # 停止控制：GUI线程调用stop()设置，爬取线程在循环检查点与流式读取时检测
        self._stop_event = threading.Event()
        self._response_lock = threading.Lock()  # 保护当前响应引用（跨线程关闭用）
        self._current_response = None  # 当前正在进行的HTTP响应，停止时用于中断请求
        # 提取站点根域名做外部链接判断
        self.base_domain = urlparse(start_url).netloc
        # 任务记录ID（断点续爬用）
        self.task_id = None
        # 续爬时复用原任务ID（None表示全新任务，全量爬取不受历史记录影响）
        self.resume_task_id = resume_task_id
        # 已爬URL指纹集合：仅加载当前任务的记录（任务级隔离，新任务不跳过历史URL）
        self.visited_hashes = set()
        self._init_db()

    # ---------- 数据库：建表与旧库迁移 ----------
    def _init_db(self):
        """初始化数据库表结构与旧库迁移（等效原内联逻辑）。"""
        ensure_schema(self.db_path)

    # ---------- 数据库：加载已爬指纹（任务级隔离，重启续爬跳过依据） ----------
    def _load_visited_from_db(self, task_id=None):
        """加载指定任务的已爬指纹。

        task_id=None（全新任务）时清空指纹集合，保证重新爬取同一站点
        不被历史记录跳过；传入task_id（续爬）时仅跳过该任务已爬的URL。
        """
        self.visited_hashes = load_visited_hashes(self.db_path, task_id)

    # ---------- 数据库：记录任务开始/结束（断点续爬） ----------
    def _create_task(self):
        """登记任务记录（续爬复用原任务ID），随后按任务ID加载已爬指纹。"""
        self.task_id = create_crawl_task(self.db_path, self.start_url,
                                         self.max_depth, self.resume_task_id)
        # 按任务ID加载已爬指纹：续爬跳过已爬页面，新任务全量爬取
        self._load_visited_from_db(self.task_id)

    def _finish_task(self):
        """任务正常结束时标记 completed。"""
        finish_crawl_task(self.db_path, self.task_id)

    # ---------- URL 指纹 ----------
    @staticmethod
    def _url_fingerprint(url):
        """URL 指纹：SHA-256，避免直接存储明文且去重稳定"""
        return hashlib.sha256(url.encode("utf-8")).hexdigest()

    # ---------- 数据库：记录已爬URL ----------
    def _save_crawled(self, url):
        """将已爬 URL 写入数据库（内部容错）并加入会话内指纹集合。"""
        url_hash = save_crawled_url(self.db_path, url, self.task_id or 0)
        self.visited_hashes.add(url_hash)

    # ---------- 暂停 / 继续（GUI线程直接调用，线程安全） ----------
    def pause(self):
        self._pause_event.clear()
        self.signal_paused.emit()

    def resume(self):
        self._pause_event.set()
        self.signal_resumed.emit()

    # ---------- 反爬：随机UA + 代理 + 请求间隔抖动 ----------
    def _build_headers(self):
        """每次请求随机换UA，降低被识别为同一爬虫的风险"""
        return {"User-Agent": self.ua_pool.random()}

    def _sleep_with_jitter(self):
        """按设定间隔+随机抖动睡眠，实际间隔=设定值*(1+uniform(0, jitter))"""
        if self.request_delay <= 0 or not self.visited_urls:
            return
        delay = self.request_delay * (1 + random.uniform(0, self.jitter))
        # 分段睡眠：每隔0.1秒检查一次停止标志，保证停止指令能及时响应
        elapsed = 0.0
        while elapsed < delay:
            if self._stop_event.is_set():
                return
            time.sleep(min(0.1, delay - elapsed))
            elapsed += 0.1

    # ---------- 停止控制（GUI线程调用，线程安全） ----------
    def stop(self):
        """请求停止爬取：设置停止标志并中断当前正在进行的HTTP请求。

        线程安全，可在GUI线程直接调用。停止后：
        - run()在下一个检查点退出，已爬数据已写入数据库（断点续爬依据）
        - 任务在数据库中保持running状态，重启后可选择"继续上次任务"
        """
        self._stop_event.set()
        with self._response_lock:
            resp = self._current_response
        if resp is not None:
            # 注意：不能在此调用 resp.close() —— Windows上阻塞读取中的close()会等待
            # 读取线程释放锁，导致停止操作长时间卡死。改为直接shutdown底层socket，
            # 可靠唤醒阻塞中的recv，读取线程随后自行close并退出。
            try:
                sock = self._find_socket(getattr(resp, "raw", None))
                if sock is not None:
                    sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass

    @staticmethod
    def _find_socket(obj, depth=0):
        """递归查找对象属性链中的socket实例。

        urllib3/http.client的内部结构（_fp.fp.raw._sock）随版本可能变化，
        因此递归遍历常见属性名，最多下探6层，找不到返回None。
        """
        if depth > 6 or obj is None:
            return None
        if isinstance(obj, socket.socket):
            return obj
        if isinstance(obj, dict):
            for value in obj.values():
                found = RecursiveCrawlerThread._find_socket(value, depth + 1)
                if found is not None:
                    return found
            return None
        for attr in ("_sock", "raw", "_fp", "fp", "_connection", "sock"):
            try:
                found = RecursiveCrawlerThread._find_socket(getattr(obj, attr, None), depth + 1)
            except Exception:
                continue
            if found is not None:
                return found
        return None

    def _is_stopped(self):
        """返回是否已请求停止（供爬取循环检查点使用）"""
        return self._stop_event.is_set()

    # ---------- 主流程 ----------
    def run(self):
        try:
            # 登记任务（断点续爬依据）
            self._create_task()
            # BFS广度优先递归爬取
            current_level_urls = {self.start_url}
            stopped = False   # 手动停止标志
            reached_limit = False  # 达到最大页数自动停止标志
            for depth in range(self.max_depth + 1):
                # 每层开始时检查停止/暂停
                if self._is_stopped():
                    stopped = True
                    break
                self._pause_event.wait()
                self.signal_log.emit(f"\n===== 开始爬取第 {depth} 层页面 =====")
                next_level_urls = set()

                for url in current_level_urls:
                    # 每个URL请求前检查停止/暂停
                    if self._is_stopped():
                        stopped = True
                        break
                    self._pause_event.wait()

                    # 会话内去重 + 数据库指纹去重（重启自动跳过）
                    if url in self.visited_urls or self._url_fingerprint(url) in self.visited_hashes:
                        self.signal_log.emit(f"跳过已爬取过的URL: {url}")
                        continue

                    # URL过滤规则（共享URLFilter实例，白名单/正则规则实时生效）
                    if not self.url_filter.should_fetch(url):
                        self.signal_log.emit(f"被URL过滤规则拦截，跳过: {url}")
                        continue

                    try:
                        # 控制请求间隔（含随机抖动），避免请求过于频繁对目标站点造成压力
                        self._sleep_with_jitter()
                        if self._is_stopped():
                            stopped = True
                            break

                        self.visited_urls.add(url)
                        self.signal_log.emit(f"正在爬取: {url}")

                        # 流式获取页面（统一走流式读取，支持停止时中断请求），支持单页最大字符数限制
                        resp = requests.get(url, headers=self._build_headers(),
                                            proxies=self.proxy, timeout=10, stream=True)
                        with self._response_lock:
                            self._current_response = resp
                        truncated = False
                        limit_bytes = self.max_chars_per_page * 4 if self.max_chars_per_page > 0 else 0  # UTF-8最多4字节/字符
                        chunks = []
                        total = 0
                        interrupted = False
                        for chunk in resp.iter_content(chunk_size=8192):
                            if self._is_stopped():
                                interrupted = True  # 停止指令到达，中断本次请求
                                break
                            chunks.append(chunk)
                            total += len(chunk)
                            if limit_bytes and total >= limit_bytes:
                                truncated = True
                                break
                        if interrupted:
                            # 停止中断：不再访问响应体，直接清理连接并退出
                            resp.close()
                            with self._response_lock:
                                self._current_response = None
                            self.signal_log.emit(f"请求 {url} 已被停止指令中断")
                            stopped = True
                            break
                        # 解码编码：优先响应头charset；仅在响应体未被完整消费时尝试检测实际编码
                        # （apparent_encoding在body已被iter_content完整消费后访问会抛RuntimeError）
                        encoding = resp.encoding or "utf-8"
                        if not resp.encoding:
                            try:
                                encoding = resp.apparent_encoding or "utf-8"
                            except RuntimeError:
                                encoding = "utf-8"  # body已消费完，无法检测，回退utf-8
                        resp.close()
                        with self._response_lock:
                            self._current_response = None
                        page_html = b"".join(chunks).decode(encoding, errors="replace")
                        if self.max_chars_per_page > 0:
                            page_html = page_html[:self.max_chars_per_page]

                        # 提取当前页链接、媒体资源及页面纯文字（委托 core.parser）
                        page_links = extract_links(page_html, url)
                        page_media = extract_media_links(page_html, url)
                        page_text = extract_text(page_html)
                        self.all_extracted_links.update(page_links)
                        self.media_urls.update(page_media)
                        self.page_contents[url] = page_text
                        done_info = f"页面字符数：{len(page_html)}，提取到链接数：{len(page_links)}，媒体资源数：{len(page_media)}"
                        if truncated:
                            done_info += "（已超过单页上限，内容被截断）"
                        self.signal_single_page_done.emit(url, done_info)
                        self.signal_progress.emit(len(self.visited_urls), depth, self.max_depth)

                        # 爬取成功后写入数据库，供下次重启跳过
                        self._save_crawled(url)

                        # 筛选下一层待爬取链接（定向过滤：仅沿包含关键词的链接继续递归）
                        for link in page_links:
                            link_domain = urlparse(link).netloc
                            in_filter = (not self.link_filter) or (self.link_filter in link)
                            # 非外部链接且匹配定向关键词才加入待爬队列
                            if in_filter and (self.crawl_external or (link_domain == self.base_domain)):
                                if link not in self.visited_urls:
                                    next_level_urls.add(link)

                        # 自动停止：达到最大爬取页数
                        if self.max_pages > 0 and len(self.visited_urls) >= self.max_pages:
                            reached_limit = True
                            break

                    except Exception as e:
                        if self._is_stopped():
                            # 停止指令中断了请求（如socket被shutdown），非真实失败
                            self.signal_log.emit(f"请求 {url} 因停止指令中断")
                            stopped = True
                            break
                        self.signal_log.emit(f"爬取 {url} 失败: {str(e)}")
                        continue

                if self._is_stopped():
                    stopped = True
                if stopped or reached_limit:
                    break
                current_level_urls = next_level_urls
                if not current_level_urls:
                    self.signal_log.emit("没有更多可爬取的新链接，爬取提前结束")
                    break

            # 结束分支处理：
            if stopped:
                # 手动停止：不标记任务完成（保留running状态供断点续爬），回传已爬取的部分结果
                self.signal_log.emit("⏹ 爬取任务已手动停止，已爬数据与进度状态已保存，重启后可继续上次任务")
                self.signal_media_found.emit(list(self.media_urls))
                self.signal_stopped.emit(len(self.visited_urls), list(self.all_extracted_links), self.page_contents)
            else:
                if reached_limit:
                    self.signal_log.emit(f"⏹ 已到达最大爬取页数({self.max_pages})，自动停止")
                # 正常/自动停止：标记completed，并回传媒体资源列表
                self._finish_task()
                self.signal_media_found.emit(list(self.media_urls))
                self.signal_finish.emit(len(self.visited_urls), list(self.all_extracted_links), self.page_contents)

        except Exception as e:
            # 异常时不标记任务完成，保留running状态以便下次续爬
            self.signal_error.emit(f"递归爬取异常：{str(e)}")


__all__ = ["RecursiveCrawlerThread"]
