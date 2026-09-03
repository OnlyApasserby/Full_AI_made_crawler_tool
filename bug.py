from __future__ import annotations

import sys
import re
import time
import csv
import html
import json
import os
import base64
import queue
import random
import shutil
import socket
import threading
import hashlib
import sqlite3
import requests
from urllib.parse import urlparse, urljoin, unquote
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QPushButton, QLineEdit, QTextEdit,
                             QLabel, QMessageBox, QSpinBox, QDoubleSpinBox, QCheckBox,
                             QFileDialog, QDialog, QComboBox, QListWidget,
                             QListWidgetItem, QAbstractItemView, QToolBar,
                             QTabWidget, QGroupBox)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtGui import QShortcut, QKeySequence

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    Image = None
    HAS_PIL = False

# 用于 robots.txt 匹配的爬虫标识
CRAWLER_USER_AGENT = "crawler-tool"

# 常用浏览器UA池（UserAgentPool默认使用）
DEFAULT_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edge/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
]


class UserAgentPool:
    """UA池：线程安全地提供随机User-Agent，支持添加/删除"""

    def __init__(self, user_agents=None):
        self._lock = threading.Lock()
        self._user_agents = list(user_agents) if user_agents else list(DEFAULT_USER_AGENTS)

    def random(self):
        """随机返回一个UA（线程安全）"""
        with self._lock:
            return random.choice(self._user_agents)

    def add(self, ua):
        ua = ua.strip()
        if ua and ua not in self._user_agents:
            with self._lock:
                self._user_agents.append(ua)
            return True
        return False

    def remove(self, ua):
        with self._lock:
            if ua in self._user_agents:
                self._user_agents.remove(ua)
                return True
        return False

    def reset(self):
        """恢复默认UA列表"""
        with self._lock:
            self._user_agents = list(DEFAULT_USER_AGENTS)

    def all(self):
        with self._lock:
            return list(self._user_agents)


class URLFilter:
    """URL过滤管理器：正则屏蔽规则 + 域名白名单，线程安全。

    判定顺序（should_fetch）：
    1. 若配置了域名白名单且URL域名不在白名单内 → 过滤
    2. 任一屏蔽正则命中URL → 过滤
    规则变化（添加/删除/批量设置）实时生效，无需重启爬取任务。
    所有共享数据均受 threading.Lock 保护，可在爬取线程与GUI线程间安全共享。
    """

    MAX_SAMPLES = 100  # 最近被过滤URL样本保留上限

    def __init__(self, block_patterns: list[str] | None = None,
                 allow_domains: list[str] | None = None) -> None:
        """初始化过滤规则。

        :param block_patterns: 初始屏蔽正则列表（非法正则自动忽略）
        :param allow_domains: 初始域名白名单列表
        """
        self._lock = threading.Lock()  # 保护所有共享数据
        self._block_patterns: list[str] = []  # 屏蔽正则（原始字符串）
        self._block_compiled: dict[str, re.Pattern] = {}  # 预编译正则缓存
        self._allow_domains: set[str] = set()  # 域名白名单
        self._filtered_count: int = 0  # 被过滤URL累计计数
        self._filtered_samples: list[str] = []  # 最近被过滤的URL样本
        for pattern in (block_patterns or []):
            self.add_block_pattern(pattern)
        for domain in (allow_domains or []):
            self.add_allow_domain(domain)

    # ---------- 工具方法 ----------
    @staticmethod
    def _normalize_domain(domain: str) -> str:
        """规范化域名：去协议前缀/路径/端口/开头点号并转小写，无法识别时返回空串"""
        domain = (domain or "").strip().lower()
        if not domain:
            return ""
        if "://" in domain:
            domain = urlparse(domain).netloc or domain
        return domain.split("/")[0].split(":")[0].lstrip(".")

    def _record_filtered(self, url: str) -> None:
        """记录一次过滤事件（计数+样本，样本最多保留MAX_SAMPLES条）。调用方须持有锁"""
        self._filtered_count += 1
        self._filtered_samples.append(url)
        if len(self._filtered_samples) > self.MAX_SAMPLES:
            self._filtered_samples = self._filtered_samples[-self.MAX_SAMPLES:]

    # ---------- 屏蔽正则规则 ----------
    def add_block_pattern(self, pattern: str) -> bool:
        """添加屏蔽正则规则并验证正则有效性。

        :return: 添加成功返回True；正则非法或已存在返回False
        """
        pattern = pattern.strip()
        if not pattern:
            return False
        try:
            re.compile(pattern)
        except re.error:
            return False
        with self._lock:
            if pattern in self._block_patterns:
                return False
            self._block_patterns.append(pattern)
            self._block_compiled[pattern] = re.compile(pattern)
            return True

    def remove_block_pattern(self, pattern: str) -> bool:
        """移除指定屏蔽正则规则。

        :return: 移除成功返回True；规则不存在返回False
        """
        with self._lock:
            if pattern in self._block_patterns:
                self._block_patterns.remove(pattern)
                self._block_compiled.pop(pattern, None)
                return True
        return False

    def set_block_patterns(self, patterns: list[str]) -> int:
        """批量设置屏蔽正则（替换现有全部规则），非法正则自动忽略。

        :return: 实际生效的正则数量
        """
        valid: list[str] = []
        for pattern in patterns:
            pattern = str(pattern).strip()
            if not pattern or pattern in valid:
                continue
            try:
                re.compile(pattern)
            except re.error:
                continue
            valid.append(pattern)
        with self._lock:
            self._block_patterns = valid
            self._block_compiled = {p: re.compile(p) for p in valid}
        return len(valid)

    def block_patterns(self) -> list[str]:
        """返回当前全部屏蔽正则（副本，线程安全）"""
        with self._lock:
            return list(self._block_patterns)

    # ---------- 域名白名单 ----------
    def add_allow_domain(self, domain: str) -> bool:
        """添加域名白名单（自动规范化：去协议/路径/端口/开头点号）。

        :return: 添加成功返回True；空值返回False
        """
        domain = self._normalize_domain(domain)
        if not domain:
            return False
        with self._lock:
            self._allow_domains.add(domain)
        return True

    def remove_allow_domain(self, domain: str) -> bool:
        """移除域名白名单。

        :return: 移除成功返回True；域名不存在返回False
        """
        domain = self._normalize_domain(domain)
        with self._lock:
            if domain in self._allow_domains:
                self._allow_domains.remove(domain)
                return True
        return False

    def allow_domains(self) -> list[str]:
        """返回当前全部白名单域名（副本，排序，线程安全）"""
        with self._lock:
            return sorted(self._allow_domains)

    # ---------- 判定与统计 ----------
    def should_fetch(self, url: str) -> bool:
        """判断URL是否应被爬取。

        规则：先检查域名白名单（已配置时必须命中才放行），再检查正则屏蔽规则；
        任一规则命中返回False。任何解析/匹配异常都视为放行，保证爬取不中断。
        """
        try:
            host = urlparse(url).netloc.lower()
        except Exception:
            host = ""
        # 1. 域名白名单：已配置时必须命中才放行
        with self._lock:
            if self._allow_domains:
                allowed = any(host == d or host.endswith("." + d)
                              for d in self._allow_domains)
                if not allowed:
                    self._record_filtered(url)
                    return False
        # 2. 正则屏蔽规则：任一命中即过滤
        with self._lock:
            compiled = list(self._block_compiled.values())
        for regex in compiled:
            try:
                if regex.search(url):
                    self._record_filtered(url)
                    return False
            except re.error:
                continue
        return True

    def get_stats(self) -> dict:
        """返回过滤统计信息（副本，线程安全）。

        :return: 含 block_pattern_count / allow_domain_count / filtered_count / recent_filtered 的字典
        """
        with self._lock:
            return {
                "block_pattern_count": len(self._block_patterns),
                "allow_domain_count": len(self._allow_domains),
                "filtered_count": self._filtered_count,
                "recent_filtered": list(self._filtered_samples),
            }

    # ---------- 配置持久化 ----------
    def save_config(self, filepath: str) -> None:
        """保存屏蔽规则与白名单到JSON文件。

        :raises OSError: 文件写入失败时抛出
        """
        data = {
            "block_patterns": self.block_patterns(),
            "allow_domains": self.allow_domains(),
        }
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def load_config(self, filepath: str) -> bool:
        """从JSON文件加载屏蔽规则与白名单（替换现有配置）。

        :raises ValueError: 配置文件格式错误时抛出
        :raises OSError: 文件读取失败时抛出
        :return: 加载成功返回True
        """
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        patterns = data.get("block_patterns", [])
        domains = data.get("allow_domains", [])
        if not isinstance(patterns, list) or not isinstance(domains, list):
            raise ValueError("配置文件格式错误：block_patterns/allow_domains 必须是列表")
        self.set_block_patterns([str(p) for p in patterns])
        valid_domains: set[str] = set()
        for domain in domains:
            normalized = self._normalize_domain(str(domain))
            if normalized:
                valid_domains.add(normalized)
        with self._lock:
            self._allow_domains = valid_domains
        return True


class DownloadTask:
    """下载队列任务项：携带优先级，PriorityQueue按(priority, seq)出队。
    优先级：页面图片等小资源(0) > 音频(1) > 文档(2) > 视频大文件(3) > 其他(4)。"""

    __slots__ = ("priority", "seq", "url", "max_retries")

    def __init__(self, priority, seq, url, max_retries):
        self.priority = priority
        self.seq = seq
        self.url = url
        self.max_retries = max_retries

    def __lt__(self, other):
        return (self.priority, self.seq) < (other.priority, other.seq)


class ContentDownloader(QThread):
    """媒体下载管理器：
    - 优先级队列 + 独立工作线程池（并发数可配置）
    - 1MB分块下载 + Range断点续传 + 下载速度限制
    - Content-Type/扩展名自动识别资源类型，按 <类型>/<域名> 分目录存储
    - MD5校验（服务器提供校验和时验证）、失败自动重试
    - 相同图片内容去重、图片压缩（Pillow，质量可调）
    - 下载完成后生成资源清单 index.html / resources.txt
    """
    signal_log = pyqtSignal(str)
    signal_item_done = pyqtSignal(str, str, bool, str)   # url, 保存路径, 成功?, 说明
    signal_progress = pyqtSignal(int, int, int, int)     # 已完成, 总数, 成功, 失败
    signal_finish = pyqtSignal(int, int, list, str)      # 成功数, 失败数, 失败URL列表, 清单路径
    signal_disk_status = pyqtSignal(str)                 # 存储空间状态

    CHUNK_SIZE = 1024 * 1024  # 分块下载：每块1MB
    IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".ico", ".avif"}
    VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".avi", ".mov", ".flv", ".wmv", ".m4v", ".ts", ".m3u8"}
    AUDIO_EXTS = {".mp3", ".wav", ".aac", ".ogg", ".flac", ".m4a", ".wma", ".opus"}
    DOCUMENT_EXTS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
                     ".txt", ".zip", ".rar", ".7z", ".epub"}
    TYPE_PRIORITY = {"image": 0, "audio": 1, "document": 2, "video": 3, "other": 4}
    EXT_MAP = {"jpeg": "jpg", "svg+xml": "svg", "x-icon": "ico", "quicktime": "mov",
               "x-msvideo": "avi", "mpeg": "mpg", "octet-stream": "bin"}

    def __init__(self, urls, download_dir, max_workers=4, max_retries=3,
                 rate_limit=0, enable_dedup=True, compress_quality=0,
                 proxy=None, ua_pool=None, db_path="crawler_cache.db"):
        super().__init__()
        self.urls = list(urls)
        self.download_dir = download_dir
        self.max_workers = max(1, max_workers)      # 并发下载数控制
        self.max_retries = max(0, max_retries)      # 失败自动重试次数
        self.rate_limit = max(0, rate_limit)        # 下载速度限制(bytes/s)，0=不限
        self.enable_dedup = enable_dedup            # 相同图片内容去重
        self.compress_quality = compress_quality    # 图片压缩质量(0=不压缩)
        self.proxy = proxy or {}
        self.ua_pool = ua_pool or UserAgentPool()
        self.db_path = db_path

        self._queue = queue.PriorityQueue()  # 优先级队列
        self._stop_event = threading.Event()
        self._active_lock = threading.Lock()
        self._active = 0
        self._done_lock = threading.Lock()
        self._success = 0
        self._failed = 0
        self.failed_urls = []              # 最终失败（重试耗尽）的URL列表
        self._total = 0
        # 图片去重：文件MD5 -> 已保存路径（仅对image类型生效）
        self._dedup_lock = threading.Lock()
        self._image_hashes = {}
        # 资源清单数据
        self._manifest_lock = threading.Lock()
        self._manifest_rows = []

    def stop(self):
        """请求停止下载（正在执行的任务会在下一个分块处退出）"""
        self._stop_event.set()

    # ---------- 资源类型识别与优先级 ----------
    @classmethod
    def _classify_url(cls, url):
        """根据URL扩展名初步推断资源类型"""
        ext = os.path.splitext(urlparse(url).path)[1].lower()
        if ext in cls.IMAGE_EXTS:
            return "image"
        if ext in cls.AUDIO_EXTS:
            return "audio"
        if ext in cls.VIDEO_EXTS:
            return "video"
        if ext in cls.DOCUMENT_EXTS:
            return "document"
        return "other"

    @classmethod
    def _classify_content_type(cls, content_type):
        """根据Content-Type推断资源类型，无法识别时返回None"""
        ct = (content_type or "").lower()
        for prefix, rtype in (("image/", "image"), ("video/", "video"),
                              ("audio/", "audio")):
            if ct.startswith(prefix):
                return rtype
        if ct.startswith("text/") or "pdf" in ct or "word" in ct or \
                "sheet" in ct or "zip" in ct or "compressed" in ct:
            return "document"
        return None

    def _priority_of(self, url):
        """URL入队时的初始优先级（页面资源优先）"""
        return self.TYPE_PRIORITY.get(self._classify_url(url), self.TYPE_PRIORITY["other"])

    @classmethod
    def _guess_extension(cls, content_type):
        """根据Content-Type推断文件扩展名，无法识别时返回.bin"""
        ct = (content_type or "").lower()
        if "/" not in ct:
            return ".bin"
        main, sub = ct.split("/", 1)
        sub = sub.split(";")[0].strip().lower()
        if main not in ("image", "video", "audio", "application", "text"):
            return ".bin"
        return "." + cls.EXT_MAP.get(sub, sub)

    # ---------- 保存路径：按类型/域名分文件夹 ----------
    def _resolve_save_path(self, url, resource_type):
        """保存路径：<下载目录>/<资源类型>/<域名>/<文件名>，文件名无扩展名时用URL哈希占位"""
        parsed = urlparse(url)
        host = parsed.netloc.replace(":", "_") or "unknown_host"
        folder = os.path.join(self.download_dir, resource_type, host)
        os.makedirs(folder, exist_ok=True)
        name = os.path.basename(unquote(parsed.path))
        name = re.sub(r'[\\/:*?"<>|]', "_", name)
        if not name or "." not in name:
            name = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        return os.path.join(folder, name)

    # ---------- MD5 校验 ----------
    @staticmethod
    def _file_md5(path):
        """计算文件MD5（分块读取，避免大文件占满内存）"""
        h = hashlib.md5()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def _header_md5(headers):
        """从响应头提取期望的MD5（支持Digest: md5=hex 与 Content-MD5 base64两种格式）"""
        digest = headers.get("Digest", "")
        if digest.lower().startswith("md5="):
            return digest[4:].strip().lower()
        b64 = headers.get("Content-MD5", "")
        if b64:
            try:
                return base64.b64decode(b64).hex()
            except Exception:
                return None
        return None

    @staticmethod
    def _verify_md5(path, expected_md5):
        return ContentDownloader._file_md5(path).lower() == expected_md5.lower()

    # ---------- 图片去重 / 压缩（存储优化） ----------
    def _dedup_image(self, path, resource_type):
        """相同内容的图片去重：MD5已存在时删除新副本并指向已有文件"""
        if not self.enable_dedup or resource_type != "image":
            return path, ""
        try:
            md5 = self._file_md5(path)
            with self._dedup_lock:
                if md5 in self._image_hashes:
                    existing = self._image_hashes[md5]
                    if existing != path and os.path.exists(existing):
                        os.remove(path)
                        return existing, "内容重复已去重"
                else:
                    self._image_hashes[md5] = path
            return path, ""
        except Exception:
            return path, ""

    def _compress_image(self, path, quality):
        """图片压缩：JPEG/WebP按质量重存，PNG无损优化；压缩后未变小则保留原图"""
        if not HAS_PIL or quality <= 0:
            return path, ""
        ext = os.path.splitext(path)[1].lower()
        if ext not in (".jpg", ".jpeg", ".png", ".webp", ".bmp"):
            return path, ""
        tmp = path + ".compress.tmp"
        try:
            img = Image.open(path)
            if ext in (".jpg", ".jpeg"):
                img = img.convert("RGB")
                img.save(tmp, "JPEG", quality=quality, optimize=True)
            elif ext == ".webp":
                img.save(tmp, "WEBP", quality=quality, optimize=True)
            elif ext == ".png":
                img.save(tmp, "PNG", optimize=True)
            else:
                img.close()
                return path, ""
            img.close()
            if os.path.getsize(tmp) < os.path.getsize(path):
                os.replace(tmp, path)
                return path, f"已压缩(质量{quality})"
            os.remove(tmp)
            return path, ""
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)
            return path, ""

    # ---------- 数据库：记录媒体下载状态 ----------
    def _save_media_state(self, url, file_path, status):
        url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                "INSERT OR REPLACE INTO media_downloads (url_hash, url, file_path, status, downloaded_at) "
                "VALUES (?, ?, ?, ?, datetime('now', 'localtime'))",
                (url_hash, url, file_path, status))
            conn.commit()
            conn.close()
        except Exception:
            pass

    # ---------- 单资源下载流程 ----------
    def _download_one(self, url):
        """下载单个资源：探测类型→确定路径→断点续传→MD5校验→压缩→去重"""
        try:
            # 1. 探测请求：获取Content-Type / Content-Length / 校验和
            probe = requests.get(url, headers={"User-Agent": self.ua_pool.random()},
                                 proxies=self.proxy, timeout=20, stream=True)
            content_type = probe.headers.get("Content-Type", "")
            total_len = int(probe.headers.get("Content-Length") or 0)
            expected_md5 = self._header_md5(probe.headers)
            probe.close()

            # 2. 识别资源类型并确定保存路径
            resource_type = self._classify_content_type(content_type) or self._classify_url(url)
            save_path = self._resolve_save_path(url, resource_type)
            if not os.path.splitext(save_path)[1]:
                save_path += self._guess_extension(content_type)

            # 3. 已完整下载过的文件：服务器提供MD5则校验，通过直接跳过
            existing = os.path.getsize(save_path) if os.path.exists(save_path) else 0
            if total_len > 0 and existing >= total_len:
                if expected_md5:
                    if self._verify_md5(save_path, expected_md5):
                        return True, save_path, "已存在(MD5校验通过)"
                    self.signal_log.emit(f"MD5校验失败，重新下载: {url}")
                    os.remove(save_path)
                    existing = 0
                else:
                    return True, save_path, "已存在，跳过"

            # 4. 断点续传：已有部分文件时发送Range请求追加
            headers = {"User-Agent": self.ua_pool.random()}
            if existing > 0:
                headers["Range"] = f"bytes={existing}-"
            resp = requests.get(url, headers=headers, proxies=self.proxy,
                                timeout=30, stream=True)
            if resp.status_code == 200:
                mode = "wb"     # 服务器不支持Range或文件为空，从头下载
            elif resp.status_code == 206:
                mode = "ab"     # 断点续传，追加写入
            else:
                resp.close()
                return False, "", f"HTTP {resp.status_code}"

            # 5. 1MB分块写入 + 下载速度限制
            start_time = time.time()
            downloaded = existing
            with open(save_path, mode) as f:
                for chunk in resp.iter_content(chunk_size=self.CHUNK_SIZE):
                    if self._stop_event.is_set():
                        resp.close()
                        return False, "", "任务已停止"
                    f.write(chunk)
                    downloaded += len(chunk)
                    if self.rate_limit > 0:
                        elapsed = time.time() - start_time
                        expected = downloaded / self.rate_limit
                        if expected > elapsed:
                            time.sleep(expected - elapsed)
            resp.close()

            # 6. MD5校验：服务器提供校验和时不匹配则删除重下
            if expected_md5 and not self._verify_md5(save_path, expected_md5):
                os.remove(save_path)
                return False, "", "MD5校验失败"

            # 7. 存储优化：压缩 → 去重
            if self.compress_quality > 0:
                save_path, note1 = self._compress_image(save_path, self.compress_quality)
            else:
                note1 = ""
            if self.enable_dedup:
                save_path, note2 = self._dedup_image(save_path, resource_type)
            else:
                note2 = ""

            # 8. 记录清单与数据库
            size = os.path.getsize(save_path) if os.path.exists(save_path) else 0
            md5 = self._file_md5(save_path) if os.path.exists(save_path) else ""
            self._record_manifest(url, save_path, resource_type, size, md5)
            self._save_media_state(url, save_path, "completed")
            note = " | ".join(n for n in (note1, note2) if n)
            return True, save_path, note
        except Exception as e:
            return False, "", f"{type(e).__name__}: {str(e)[:80]}"

    def _record_manifest(self, url, path, resource_type, size, md5):
        with self._manifest_lock:
            self._manifest_rows.append({"url": url, "path": path,
                                        "type": resource_type, "size": size, "md5": md5})

    # ---------- 失败重试 ----------
    def _download_with_retry(self, task):
        """按任务重试次数执行下载，失败指数退避后重试"""
        for attempt in range(1, task.max_retries + 1):
            if self._stop_event.is_set():
                return False, "", "任务已停止"
            ok, path, note = self._download_one(task.url)
            if ok:
                return True, path, note
            if attempt < task.max_retries:
                wait = 2 ** attempt
                self.signal_log.emit(f"下载失败({note})，{wait}s后第{attempt + 1}次重试: {task.url}")
                time.sleep(wait)
        return False, "", f"重试{task.max_retries}次仍失败"

    # ---------- 工作线程 ----------
    def _process_task(self, task):
        ok, path, note = self._download_with_retry(task)
        if ok:
            with self._done_lock:
                self._success += 1
        else:
            with self._done_lock:
                self._failed += 1
                self.failed_urls.append(task.url)
        self.signal_item_done.emit(task.url, path, ok, note)
        with self._done_lock:
            done = self._success + self._failed
        self.signal_progress.emit(done, self._total, self._success, self._failed)

    def _worker(self):
        """独立工作线程：从优先级队列取任务执行，空闲且无活动任务时退出"""
        while not self._stop_event.is_set():
            try:
                task = self._queue.get(timeout=0.5)
            except queue.Empty:
                with self._active_lock:
                    if self._active == 0:
                        break
                continue
            with self._active_lock:
                self._active += 1
            try:
                self._process_task(task)
            finally:
                with self._active_lock:
                    self._active -= 1
                self._queue.task_done()

    # ---------- 存储空间监控 ----------
    @staticmethod
    def _fmt_size(num):
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if num < 1024 or unit == "TB":
                return f"{num:.1f} {unit}"
            num /= 1024

    def _emit_disk_status(self, label=""):
        try:
            usage = shutil.disk_usage(self.download_dir)
            self.signal_disk_status.emit(
                f"{label} | 剩余空间：{self._fmt_size(usage.free)} / 总空间：{self._fmt_size(usage.total)}")
        except Exception:
            pass

    # ---------- 资源清单生成 ----------
    def _generate_manifest(self):
        """生成资源清单 index.html 与 resources.txt，返回清单文件路径"""
        try:
            os.makedirs(self.download_dir, exist_ok=True)
            with self._manifest_lock:
                rows = list(self._manifest_rows)
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            html_lines = [
                "<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'>",
                "<title>下载资源清单</title>",
                "<style>body{font-family:sans-serif;margin:20px}table{border-collapse:collapse;width:100%}",
                "th,td{border:1px solid #ccc;padding:6px;text-align:left;font-size:13px}",
                "th{background:#f0f0f0}code{font-size:12px}</style></head><body>",
                f"<h1>下载资源清单</h1><p>生成时间：{now}　成功：{self._success}　失败：{self._failed}</p>",
                "<table><tr><th>#</th><th>类型</th><th>资源URL</th><th>保存路径</th><th>大小</th><th>MD5</th></tr>",
            ]
            for idx, row in enumerate(rows, 1):
                html_lines.append(
                    f"<tr><td>{idx}</td><td>{row['type']}</td>"
                    f"<td><a href='{row['url']}'>{row['url'][:80]}</a></td>"
                    f"<td><code>{row['path']}</code></td>"
                    f"<td>{self._fmt_size(row['size'])}</td>"
                    f"<td><code>{row['md5'][:12]}</code></td></tr>")
            html_lines.append("</table></body></html>")
            with open(os.path.join(self.download_dir, "index.html"), "w", encoding="utf-8") as f:
                f.write("\n".join(html_lines))

            txt_lines = [f"下载资源清单  生成时间：{now}", "=" * 60, ""]
            for row in rows:
                txt_lines.append(f"[{row['type']}] {row['url']}\n"
                                 f"     → {row['path']} ({self._fmt_size(row['size'])}, MD5:{row['md5']})")
            with open(os.path.join(self.download_dir, "resources.txt"), "w", encoding="utf-8") as f:
                f.write("\n".join(txt_lines))

            manifest = os.path.join(self.download_dir, "index.html")
            self.signal_log.emit(f"📄 资源清单已生成：{manifest}（含{len(rows)}个资源）")
            return manifest
        except Exception as e:
            self.signal_log.emit(f"生成资源清单失败：{str(e)}")
            return ""

    # ---------- 主流程 ----------
    def run(self):
        """入队所有URL，启动工作线程池并发下载，结束后生成清单"""
        total = len(self.urls)
        self._total = total
        if total == 0:
            self.signal_finish.emit(0, 0, [], "")
            return
        self._emit_disk_status("任务开始")
        seq = 0
        for url in self.urls:
            self._queue.put(DownloadTask(self._priority_of(url), seq, url, self.max_retries))
            seq += 1

        workers = [threading.Thread(target=self._worker, daemon=True,
                                    name=f"downloader-{i}")
                   for i in range(self.max_workers)]
        for w in workers:
            w.start()
        for w in workers:
            w.join()

        manifest = self._generate_manifest()
        self._emit_disk_status("任务结束")
        self.signal_finish.emit(self._success, self._failed, list(self.failed_urls), manifest)


class RobotsParser:
    """简单的 robots.txt 解析器，支持 User-agent / Allow / Disallow 规则"""

    def __init__(self, content):
        self.groups = {}  # user-agent -> {"allow": [...], "disallow": [...]}
        if content:
            self.parse(content)

    def parse(self, content):
        """逐行解析 robots.txt，按 User-agent 分组记录 Allow / Disallow 规则"""
        current_agents = []
        for raw_line in content.splitlines():
            line = raw_line.split("#", 1)[0].strip()  # 去掉注释
            if not line or ":" not in line:
                continue
            key, _, value = line.partition(":")
            key = key.strip().lower()
            value = value.strip()
            if key == "user-agent":
                current_agents = [value]
                self.groups.setdefault(value, {"allow": [], "disallow": []})
            elif current_agents:
                if key == "disallow":
                    for agent in current_agents:
                        self.groups[agent]["disallow"].append(value)
                elif key == "allow":
                    for agent in current_agents:
                        self.groups[agent]["allow"].append(value)

    def _match_group(self, user_agent):
        """按最长匹配优先原则返回适用于指定UA的规则组，无匹配时回退到 *"""
        if not user_agent:
            return self.groups.get("*")
        token = user_agent.lower()
        matched = [ag for ag in self.groups if ag != "*" and ag.lower() in token]
        if matched:
            return self.groups[max(matched, key=len)]
        return self.groups.get("*")

    def is_disallowed(self, url, user_agent):
        """判断指定URL是否被robots.txt禁止（Allow优先于Disallow，匹配更长者生效）"""
        rules = self._match_group(user_agent)
        if not rules:
            return False
        path = urlparse(url).path or "/"
        allow_matches = [a for a in rules["allow"] if a and path.startswith(a)]
        disallow_matches = [d for d in rules["disallow"] if d and path.startswith(d)]
        if not disallow_matches:
            return False
        longest_disallow = max(len(d) for d in disallow_matches)
        longest_allow = max((len(a) for a in allow_matches), default=0)
        return longest_disallow > longest_allow


# 递归爬取后台线程
class RecursiveCrawlerThread(QThread):
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
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS crawled_urls (
                url_hash   TEXT PRIMARY KEY,
                url        TEXT NOT NULL,
                status     TEXT NOT NULL DEFAULT 'completed',
                file_path  TEXT DEFAULT '',
                crawled_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            )
        """)
        # 任务表：记录每次爬取任务，status=running表示未完成，供断点续爬检测
        conn.execute("""
            CREATE TABLE IF NOT EXISTS crawl_tasks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                start_url   TEXT NOT NULL,
                max_depth   INTEGER DEFAULT 1,
                status      TEXT NOT NULL DEFAULT 'running',
                created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                finished_at TEXT
            )
        """)
        # 媒体下载记录表
        conn.execute("""
            CREATE TABLE IF NOT EXISTS media_downloads (
                url_hash      TEXT PRIMARY KEY,
                url           TEXT NOT NULL,
                file_path     TEXT DEFAULT '',
                status        TEXT DEFAULT 'completed',
                downloaded_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            )
        """)
        conn.commit()
        # 旧版本数据库没有status/file_path/task_id列时自动补列
        cols = [row[1] for row in conn.execute("PRAGMA table_info(crawled_urls)").fetchall()]
        if "status" not in cols:
            conn.execute("ALTER TABLE crawled_urls ADD COLUMN status TEXT NOT NULL DEFAULT 'completed'")
        if "file_path" not in cols:
            conn.execute("ALTER TABLE crawled_urls ADD COLUMN file_path TEXT DEFAULT ''")
        if "task_id" not in cols:
            conn.execute("ALTER TABLE crawled_urls ADD COLUMN task_id INTEGER DEFAULT 0")
        conn.commit()
        conn.close()

    # ---------- 数据库：加载已爬指纹（任务级隔离，重启续爬跳过依据） ----------
    def _load_visited_from_db(self, task_id=None):
        """加载指定任务的已爬指纹。

        task_id=None（全新任务）时清空指纹集合，保证重新爬取同一站点
        不被历史记录跳过；传入task_id（续爬）时仅跳过该任务已爬的URL。
        """
        self.visited_hashes = set()
        if task_id is None or not os.path.exists(self.db_path):
            return
        try:
            conn = sqlite3.connect(self.db_path)
            rows = conn.execute(
                "SELECT url_hash FROM crawled_urls WHERE task_id=?", (task_id,)).fetchall()
            self.visited_hashes = {row[0] for row in rows}
            conn.close()
        except Exception:
            pass

    # ---------- 数据库：记录任务开始/结束（断点续爬） ----------
    def _create_task(self):
        try:
            conn = sqlite3.connect(self.db_path)
            if self.resume_task_id is not None:
                # 续爬：复用原任务ID，保留其已爬指纹用于跳过
                conn.execute(
                    "UPDATE crawl_tasks SET status='running', finished_at=NULL WHERE id=?",
                    (self.resume_task_id,))
                self.task_id = self.resume_task_id
            else:
                # 新任务：创建独立任务记录
                cur = conn.execute(
                    "INSERT INTO crawl_tasks (start_url, max_depth, status) VALUES (?, ?, 'running')",
                    (self.start_url, self.max_depth))
                self.task_id = cur.lastrowid
            conn.commit()
            conn.close()
        except Exception:
            self.task_id = self.resume_task_id
        # 按任务ID加载已爬指纹：续爬跳过已爬页面，新任务全量爬取
        self._load_visited_from_db(self.task_id)

    def _finish_task(self):
        if self.task_id is None:
            return
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                "UPDATE crawl_tasks SET status='completed', finished_at=datetime('now','localtime') WHERE id=?",
                (self.task_id,))
            conn.commit()
            conn.close()
        except Exception:
            pass

    # ---------- URL 指纹 ----------
    @staticmethod
    def _url_fingerprint(url):
        """URL 指纹：SHA-256，避免直接存储明文且去重稳定"""
        return hashlib.sha256(url.encode("utf-8")).hexdigest()

    # ---------- 数据库：记录已爬URL ----------
    def _save_crawled(self, url):
        url_hash = self._url_fingerprint(url)
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                "INSERT OR IGNORE INTO crawled_urls (url_hash, url, status, file_path, task_id) "
                "VALUES (?, ?, 'completed', '', ?)",
                (url_hash, url, self.task_id or 0))
            conn.commit()
            conn.close()
        except Exception:
            pass
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

    def extract_links(self, html, base_url):
        """从HTML中提取所有合法的绝对链接"""
        links = re.findall(r'<a[^>]+href\s*=\s*["\']([^"\']+)["\']', html, re.IGNORECASE)
        absolute_links = set()
        for link in links:
            full_link = urljoin(base_url, link)
            # 过滤非http协议链接
            if full_link.startswith(('http://', 'https://')):
                absolute_links.add(full_link)
        return absolute_links

    def extract_media_links(self, html_content, base_url):
        """从HTML中提取图片/视频/音频等媒体资源链接（img/video/audio/source的src、data-src、srcset）"""
        media = set()
        patterns = [
            r'<img[^>]+src\s*=\s*["\']([^"\']+)["\']',
            r'<img[^>]+data-src\s*=\s*["\']([^"\']+)["\']',
            r'<img[^>]+srcset\s*=\s*["\']([^"\']+)["\']',
            r'<video[^>]+src\s*=\s*["\']([^"\']+)["\']',
            r'<audio[^>]+src\s*=\s*["\']([^"\']+)["\']',
            r'<source[^>]+src\s*=\s*["\']([^"\']+)["\']',
            r'<source[^>]+data-src\s*=\s*["\']([^"\']+)["\']',
        ]
        for pat in patterns:
            for m in re.finditer(pat, html_content, re.IGNORECASE):
                raw = m.group(1).strip()
                # srcset可能包含多个候选地址（用逗号分隔），只取第一个
                first = raw.split(",")[0].strip().split(" ")[0]
                full_url = urljoin(base_url, first)
                if full_url.startswith(('http://', 'https://')):
                    media.add(full_url)
        return media

    def extract_text(self, html_content):
        """从HTML中提取纯文字内容（去除脚本、样式、标签并还原HTML实体）"""
        # 去除 script/style 块
        content = re.sub(r'<script[^>]*>.*?</script>', ' ', html_content,
                         flags=re.IGNORECASE | re.DOTALL)
        content = re.sub(r'<style[^>]*>.*?</style>', ' ', content,
                         flags=re.IGNORECASE | re.DOTALL)
        # 标签替换为换行
        text = re.sub(r'<[^>]+>', '\n', content)
        # 还原HTML实体
        text = html.unescape(text)
        # 清理空白与空行
        lines = [ln.strip() for ln in text.splitlines()]
        return '\n'.join(ln for ln in lines if ln)

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

                        # 提取当前页链接、媒体资源及页面纯文字
                        page_links = self.extract_links(page_html, url)
                        page_media = self.extract_media_links(page_html, url)
                        page_text = self.extract_text(page_html)
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


class RobotsCheckThread(QThread):
    signal_robots_content = pyqtSignal(str, str)
    signal_error = pyqtSignal(str)

    def __init__(self, target_url):
        super().__init__()
        self.target_url = target_url

    def run(self):
        try:
            parsed_url = urlparse(self.target_url)
            robots_url = urljoin(f"{parsed_url.scheme}://{parsed_url.netloc}", "/robots.txt")
            resp = requests.get(robots_url, timeout=10)
            if resp.status_code == 200:
                self.signal_robots_content.emit(resp.text, robots_url)
            else:
                self.signal_robots_content.emit("该站点未提供robots.txt文件", robots_url)
        except Exception as e:
            self.signal_error.emit(f"获取robots.txt失败：{str(e)}")


class URLFilterDialog(QDialog):
    """URL过滤规则管理对话框。

    功能：屏蔽正则与域名白名单的增删、过滤统计与被过滤URL样本查看、
    规则导入/导出（JSON）。所有操作直接作用于共享的URLFilter实例，实时生效。
    """

    def __init__(self, parent: QWidget | None, url_filter: URLFilter) -> None:
        super().__init__(parent)
        self.url_filter = url_filter
        self.setWindowTitle("URL过滤规则管理")
        self.setMinimumSize(680, 540)
        self._build_ui()
        self._reload()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        # 过滤统计 + 被过滤URL样本
        stats_group = QGroupBox("过滤统计")
        stats_layout = QVBoxLayout(stats_group)
        self.stats_label = QLabel()
        stats_layout.addWidget(self.stats_label)
        self.samples_list = QListWidget()
        self.samples_list.setMaximumHeight(110)
        self.samples_list.setToolTip("最近被过滤的URL（最多保留100条）")
        stats_layout.addWidget(self.samples_list)
        layout.addWidget(stats_group)

        # 左右两栏：屏蔽正则 / 域名白名单
        split_row = QHBoxLayout()

        block_group = QGroupBox("屏蔽正则规则（任一命中即过滤）")
        block_layout = QVBoxLayout(block_group)
        self.block_list = QListWidget()
        self.block_list.setToolTip("匹配任一正则的URL将不会被爬取")
        block_layout.addWidget(self.block_list)
        block_input_row = QHBoxLayout()
        self.block_input = QLineEdit()
        self.block_input.setPlaceholderText("如 ^https://.*/private")
        self.block_add_btn = QPushButton("添加")
        self.block_add_btn.clicked.connect(self._on_add_block)
        self.block_del_btn = QPushButton("删除选中")
        self.block_del_btn.clicked.connect(self._on_del_block)
        block_input_row.addWidget(self.block_input, 1)
        block_input_row.addWidget(self.block_add_btn)
        block_input_row.addWidget(self.block_del_btn)
        block_layout.addLayout(block_input_row)
        split_row.addWidget(block_group, 1)

        allow_group = QGroupBox("域名白名单（配置后仅白名单域名可爬取）")
        allow_layout = QVBoxLayout(allow_group)
        self.allow_list = QListWidget()
        self.allow_list.setToolTip("留空表示不限制域名；配置后只有白名单内的域名会被爬取")
        allow_layout.addWidget(self.allow_list)
        allow_input_row = QHBoxLayout()
        self.allow_input = QLineEdit()
        self.allow_input.setPlaceholderText("如 www.example.com")
        self.allow_add_btn = QPushButton("添加")
        self.allow_add_btn.clicked.connect(self._on_add_allow)
        self.allow_del_btn = QPushButton("删除选中")
        self.allow_del_btn.clicked.connect(self._on_del_allow)
        allow_input_row.addWidget(self.allow_input, 1)
        allow_input_row.addWidget(self.allow_add_btn)
        allow_input_row.addWidget(self.allow_del_btn)
        allow_layout.addLayout(allow_input_row)
        split_row.addWidget(allow_group, 1)
        layout.addLayout(split_row)

        # 底部：导入 / 导出 / 关闭
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        import_btn = QPushButton("导入规则")
        import_btn.setToolTip("从JSON文件加载屏蔽正则与白名单（替换现有配置）")
        import_btn.clicked.connect(self._on_import)
        export_btn = QPushButton("导出规则")
        export_btn.setToolTip("将当前屏蔽正则与白名单保存为JSON文件")
        export_btn.clicked.connect(self._on_export)
        close_btn = QPushButton("关闭")
        close_btn.setDefault(True)
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(import_btn)
        btn_row.addWidget(export_btn)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

    # ---------- 界面刷新 ----------
    def _reload(self) -> None:
        """从url_filter读取最新状态并刷新界面"""
        stats = self.url_filter.get_stats()
        self.stats_label.setText(
            f"屏蔽正则：{stats['block_pattern_count']} 条 | "
            f"白名单域名：{stats['allow_domain_count']} 个 | "
            f"已过滤URL：{stats['filtered_count']} 个")
        self.block_list.clear()
        self.block_list.addItems(self.url_filter.block_patterns())
        self.allow_list.clear()
        self.allow_list.addItems(self.url_filter.allow_domains())
        self.samples_list.clear()
        for url in stats["recent_filtered"]:
            self.samples_list.addItem(url)

    # ---------- 屏蔽正则操作 ----------
    def _on_add_block(self) -> None:
        pattern = self.block_input.text().strip()
        if not pattern:
            QMessageBox.warning(self, "提示", "请输入屏蔽正则表达式")
            return
        if self.url_filter.add_block_pattern(pattern):
            self.block_input.clear()
            self._reload()
        else:
            QMessageBox.warning(self, "提示", "正则表达式无效或已存在，添加失败")

    def _on_del_block(self) -> None:
        item = self.block_list.currentItem()
        if item is None:
            QMessageBox.warning(self, "提示", "请先选中要删除的正则")
            return
        self.url_filter.remove_block_pattern(item.text())
        self._reload()

    # ---------- 域名白名单操作 ----------
    def _on_add_allow(self) -> None:
        domain = self.allow_input.text().strip()
        if not domain:
            QMessageBox.warning(self, "提示", "请输入域名")
            return
        if self.url_filter.add_allow_domain(domain):
            self.allow_input.clear()
            self._reload()
        else:
            QMessageBox.warning(self, "提示", "域名无效")

    def _on_del_allow(self) -> None:
        item = self.allow_list.currentItem()
        if item is None:
            QMessageBox.warning(self, "提示", "请先选中要删除的域名")
            return
        self.url_filter.remove_allow_domain(item.text())
        self._reload()

    # ---------- 配置导入 / 导出 ----------
    def _on_import(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(self, "导入过滤规则", "",
                                                   "JSON 文件 (*.json);;所有文件 (*)")
        if not file_path:
            return
        try:
            self.url_filter.load_config(file_path)
            self._reload()
            QMessageBox.information(self, "导入成功", f"已从 {file_path} 加载过滤规则")
        except (ValueError, OSError) as e:
            QMessageBox.critical(self, "导入失败", f"加载过滤规则失败：{str(e)}")

    def _on_export(self) -> None:
        default_name = f"url_filter_rules_{time.strftime('%Y%m%d_%H%M%S')}.json"
        file_path, _ = QFileDialog.getSaveFileName(self, "导出过滤规则", default_name,
                                                   "JSON 文件 (*.json);;所有文件 (*)")
        if not file_path:
            return
        try:
            self.url_filter.save_config(file_path)
            QMessageBox.information(self, "导出成功", f"已导出过滤规则到：\n{file_path}")
        except OSError as e:
            QMessageBox.critical(self, "导出失败", f"保存过滤规则失败：{str(e)}")


class ExportDialog(QDialog):
    """导出配置对话框：选择导出格式（JSON/CSV/Markdown）与导出字段（可拖拽排序、勾选）"""

    # 可选字段：(字段key, 显示名)
    FIELD_OPTIONS = [
        ("url", "链接地址 (url)"),
        ("domain", "域名 (domain)"),
        ("text", "页面文字 (text)"),
    ]

    def __init__(self, parent=None, default_format="CSV", has_text=True):
        super().__init__(parent)
        self.setWindowTitle("导出配置")
        self.setMinimumWidth(380)

        layout = QVBoxLayout(self)

        # 格式下拉菜单
        fmt_row = QHBoxLayout()
        fmt_row.addWidget(QLabel("导出格式："))
        self.format_combo = QComboBox()
        self.format_combo.addItems(["CSV", "JSON", "Markdown"])
        fmt_row.addWidget(self.format_combo)
        fmt_row.addStretch()
        layout.addLayout(fmt_row)

        # 字段列表（勾选 + 拖拽排序）
        layout.addWidget(QLabel("导出字段（勾选需要包含的字段，可拖拽调整顺序）："))
        self.field_list = QListWidget()
        self.field_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.field_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        for key, label in self.FIELD_OPTIONS:
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, key)
            if key == "text" and not has_text:
                # 无页面文字数据时禁用text字段
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                item.setCheckState(Qt.CheckState.Unchecked)
            else:
                item.setCheckState(Qt.CheckState.Checked)
            self.field_list.addItem(item)
        layout.addWidget(self.field_list)

        # 操作按钮
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        ok_btn = QPushButton("确定")
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self.accept)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(ok_btn)
        layout.addLayout(btn_row)

        # 预选格式
        idx = self.format_combo.findText(default_format)
        if idx >= 0:
            self.format_combo.setCurrentIndex(idx)

    def selected_format(self):
        """返回小写格式名：csv / json / markdown"""
        return self.format_combo.currentText().lower()

    def selected_fields(self):
        """按列表顺序返回已勾选的字段key列表"""
        fields = []
        for i in range(self.field_list.count()):
            item = self.field_list.item(i)
            if (item.flags() & Qt.ItemFlag.ItemIsEnabled and
                    item.checkState() == Qt.CheckState.Checked):
                fields.append(item.data(Qt.ItemDataRole.UserRole))
        return fields


class CrawlerMainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("带递归爬取功能的图形化爬虫工具")
        self.setGeometry(100, 100, 1080, 820)
        self.db_path = "crawler_cache.db"
        self.ua_pool = UserAgentPool()  # 全局UA池，爬取与下载共用
        self.url_filter = URLFilter()  # 全局URL过滤规则，爬取线程共享同一实例
        self._syncing_block_input = False  # 防递归标志：输入框同步时跳过textChanged
        self.media_urls = []  # 本次爬取发现的媒体资源
        self.downloader = None
        self.init_ui()
        # 过滤统计定时刷新（过滤发生在爬取线程，需定时同步到GUI显示）
        self.filter_stats_timer = QTimer(self)
        self.filter_stats_timer.timeout.connect(self._update_filter_stats)
        self.filter_stats_timer.start(1000)
        self.check_unfinished_task()  # 启动时检测是否有未完成的任务

    def init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setSpacing(10)
        main_layout.setContentsMargins(20, 20, 20, 20)

        # 0. 主工具栏：屏蔽URL正则输入框（实时生效，无需重启任务）
        toolbar = QToolBar("主工具栏")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        toolbar.addWidget(QLabel("屏蔽URL正则："))
        self.block_regex_input = QLineEdit()
        self.block_regex_input.setPlaceholderText("如 ^https://www\\.example\\.com/private，多个用分号分隔，留空表示不屏蔽")
        self.block_regex_input.setToolTip("匹配任一正则的URL不会被爬取（多个正则用英文分号;分隔）。修改后实时生效，无需重新启动任务")
        self.block_regex_input.setMinimumWidth(340)
        self.block_regex_input.textChanged.connect(self._on_block_regex_changed)
        toolbar.addWidget(self.block_regex_input)
        toolbar.addSeparator()
        self.filter_stats_label = QLabel("已过滤URL：0")
        self.filter_stats_label.setToolTip("被URL过滤规则（屏蔽正则/白名单）拦截的URL累计数量")
        toolbar.addWidget(self.filter_stats_label)
        self.filter_manage_btn = QPushButton("过滤管理")
        self.filter_manage_btn.setToolTip("管理屏蔽正则、域名白名单，查看被过滤URL样本，导入/导出规则")
        self.filter_manage_btn.clicked.connect(self.on_open_filter_dialog)
        toolbar.addWidget(self.filter_manage_btn)

        # 1. 标签页：爬取配置 / 反爬设置 / 下载管理
        self.tabs = QTabWidget()
        self.crawl_tab = QWidget()
        self.anti_tab = QWidget()
        self.download_tab = QWidget()
        self.tabs.addTab(self.crawl_tab, "爬取配置")
        self.tabs.addTab(self.anti_tab, "反爬设置")
        self.tabs.addTab(self.download_tab, "下载管理")
        main_layout.addWidget(self.tabs, 1)

        self._init_crawl_tab()
        self._init_anti_tab()
        self._init_download_tab()
        self._init_task_manager_tab()  # 任务管理（多目标并发爬取）

        self.pending_crawl_url = ""
        self.robots_content = ""
        self.last_crawl_links = []
        self.last_page_texts = {}

        # 暂停/继续快捷键（不改变UI布局）
        QShortcut(QKeySequence("Ctrl+P"), self,
                  activated=lambda: self._toggle_pause())
        QShortcut(QKeySequence("Ctrl+R"), self,
                  activated=lambda: self._toggle_pause())

    # ---------- 标签页1：爬取配置 ----------
    def _init_crawl_tab(self):
        layout = QVBoxLayout(self.crawl_tab)
        layout.setSpacing(8)

        # 网址输入
        top_layout = QHBoxLayout()
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("请输入起始网址：https://www.example.com")
        top_layout.addWidget(QLabel("起始网址："))
        top_layout.addWidget(self.url_input)
        layout.addLayout(top_layout)

        # 递归参数配置
        param_layout = QHBoxLayout()
        self.depth_spin = QSpinBox()
        self.depth_spin.setRange(0, 5)
        self.depth_spin.setValue(1)
        self.depth_spin.setToolTip("0表示只爬取起始页面，1表示爬取起始页+起始页内所有链接，以此类推")
        self.delay_spin = QDoubleSpinBox()
        self.delay_spin.setRange(0.0, 10.0)
        self.delay_spin.setSingleStep(0.1)
        self.delay_spin.setValue(0.5)
        self.delay_spin.setSuffix(" 秒")
        self.delay_spin.setToolTip("每次HTTP请求之间的基础等待间隔（实际间隔会叠加随机抖动），用于控制请求频率，避免对目标站点造成压力")
        self.crawl_external_check = QCheckBox("允许爬取站外链接")
        param_layout.addWidget(QLabel("递归爬取深度："))
        param_layout.addWidget(self.depth_spin)
        param_layout.addWidget(QLabel("请求间隔："))
        param_layout.addWidget(self.delay_spin)
        param_layout.addWidget(self.crawl_external_check)
        param_layout.addStretch()
        self.start_check_btn = QPushButton("校验robots.txt")
        self.start_check_btn.clicked.connect(self.on_check_robots)
        param_layout.addWidget(self.start_check_btn)
        layout.addLayout(param_layout)

        # 单页最大字符数 + 定向链接过滤
        filter_layout = QHBoxLayout()
        self.max_chars_spin = QSpinBox()
        self.max_chars_spin.setRange(0, 10000000)
        self.max_chars_spin.setSingleStep(10000)
        self.max_chars_spin.setValue(0)
        self.max_chars_spin.setSuffix(" 字符")
        self.max_chars_spin.setToolTip("单个页面最多下载的字符数（0表示不限制），超出部分将被截断，避免下载超大页面")
        self.link_filter_input = QLineEdit()
        self.link_filter_input.setPlaceholderText("如 /product 或 keyword，留空表示不限制")
        self.link_filter_input.setToolTip("定向递归爬取：仅沿URL中包含该关键词的链接继续深入爬取，其余链接只记录不爬取")
        self.max_pages_spin = QSpinBox()
        self.max_pages_spin.setRange(0, 100000)
        self.max_pages_spin.setValue(0)
        self.max_pages_spin.setSuffix(" 页")
        self.max_pages_spin.setToolTip("达到该爬取页数后自动停止（0表示不限制）。手动停止可随时点击「停止爬取」按钮")
        filter_layout.addWidget(QLabel("单页最大字符数："))
        filter_layout.addWidget(self.max_chars_spin)
        filter_layout.addWidget(QLabel("最大爬取页数："))
        filter_layout.addWidget(self.max_pages_spin)
        filter_layout.addWidget(QLabel("定向链接过滤："))
        filter_layout.addWidget(self.link_filter_input)
        filter_layout.addStretch()
        layout.addLayout(filter_layout)

        # robots.txt展示区
        layout.addWidget(QLabel("robots.txt 规则预览："))
        self.robots_display = QTextEdit()
        self.robots_display.setReadOnly(True)
        self.robots_display.setMaximumHeight(120)
        layout.addWidget(self.robots_display)

        # 操作按钮区
        btn_layout = QHBoxLayout()
        self.confirm_crawl_btn = QPushButton("确认启动递归爬取")
        self.confirm_crawl_btn.setEnabled(False)
        self.confirm_crawl_btn.clicked.connect(self.on_start_crawl)
        self.stop_crawl_btn = QPushButton("停止爬取")
        self.stop_crawl_btn.setEnabled(False)
        self.stop_crawl_btn.setToolTip("手动终止当前爬取任务：中断进行中的请求、清理资源，并保存已爬数据与进度状态")
        self.stop_crawl_btn.clicked.connect(self.on_stop_crawl)
        self.export_csv_btn = QPushButton("导出CSV")
        self.export_csv_btn.setEnabled(False)
        self.export_csv_btn.clicked.connect(self.on_export_csv)
        self.export_txt_btn = QPushButton("导出TXT")
        self.export_txt_btn.setEnabled(False)
        self.export_txt_btn.clicked.connect(self.on_export_txt)
        self.clear_btn = QPushButton("清空全部内容")
        self.clear_btn.clicked.connect(self.on_clear_all)
        self.clear_records_btn = QPushButton("清空爬取记录")
        self.clear_records_btn.setToolTip("删除数据库中的已爬URL记录，之后重新爬取同一站点不会被历史记录跳过")
        self.clear_records_btn.clicked.connect(self.on_clear_crawl_records)
        btn_layout.addStretch()
        btn_layout.addWidget(self.confirm_crawl_btn)
        btn_layout.addWidget(self.stop_crawl_btn)
        btn_layout.addWidget(self.export_csv_btn)
        btn_layout.addWidget(self.export_txt_btn)
        btn_layout.addWidget(self.clear_btn)
        btn_layout.addWidget(self.clear_records_btn)
        layout.addLayout(btn_layout)

        # 统计信息
        self.crawl_stats_label = QLabel("已爬页面：0 | 当前层：-/- | 提取链接：0 | 媒体资源：0")
        layout.addWidget(self.crawl_stats_label)

        # 爬取日志与结果展示区
        self.log_display = QTextEdit()
        self.log_display.setReadOnly(True)
        layout.addWidget(QLabel("爬取进度日志："))
        layout.addWidget(self.log_display, 1)

        self.link_display = QTextEdit()
        self.link_display.setReadOnly(True)
        layout.addWidget(QLabel("页面提取到的所有链接汇总："))
        layout.addWidget(self.link_display, 1)

    # ---------- 标签页2：反爬设置 ----------
    def _init_anti_tab(self):
        layout = QVBoxLayout(self.anti_tab)
        layout.setSpacing(12)

        # UA池管理
        ua_group = QGroupBox("User-Agent 池（爬取与下载时随机抽取）")
        ua_layout = QVBoxLayout(ua_group)
        self.ua_list = QListWidget()
        self.ua_list.setMaximumHeight(180)
        self.ua_list.addItems(self.ua_pool.all())
        ua_layout.addWidget(self.ua_list)
        ua_btn_row = QHBoxLayout()
        self.ua_add_input = QLineEdit()
        self.ua_add_input.setPlaceholderText("输入新的User-Agent后点击添加")
        self.ua_add_btn = QPushButton("添加UA")
        self.ua_add_btn.clicked.connect(self.on_add_ua)
        self.ua_del_btn = QPushButton("删除选中")
        self.ua_del_btn.clicked.connect(self.on_del_ua)
        self.ua_reset_btn = QPushButton("恢复默认")
        self.ua_reset_btn.clicked.connect(self.on_reset_ua)
        ua_btn_row.addWidget(self.ua_add_input, 1)
        ua_btn_row.addWidget(self.ua_add_btn)
        ua_btn_row.addWidget(self.ua_del_btn)
        ua_btn_row.addWidget(self.ua_reset_btn)
        ua_layout.addLayout(ua_btn_row)
        layout.addWidget(ua_group)

        # 请求策略：间隔随机抖动
        delay_group = QGroupBox("请求策略")
        delay_layout = QHBoxLayout(delay_group)
        self.jitter_spin = QDoubleSpinBox()
        self.jitter_spin.setRange(0.0, 2.0)
        self.jitter_spin.setSingleStep(0.1)
        self.jitter_spin.setValue(0.3)
        self.jitter_spin.setSuffix(" 倍")
        self.jitter_spin.setToolTip("请求间隔的随机抖动幅度：实际间隔 = 设定间隔 × (1 + 随机抖动值)，降低请求规律性")
        delay_layout.addWidget(QLabel("请求间隔随机抖动幅度："))
        delay_layout.addWidget(self.jitter_spin)
        delay_layout.addWidget(QLabel("（0表示无抖动，值越大间隔波动越明显）"))
        delay_layout.addStretch()
        layout.addWidget(delay_group)

        # 代理设置
        proxy_group = QGroupBox("代理设置")
        proxy_layout = QVBoxLayout(proxy_group)
        self.proxy_check = QCheckBox("启用HTTP代理")
        self.proxy_check.setToolTip("启用后，爬取页面与下载媒体文件都将通过该代理进行")
        proxy_layout.addWidget(self.proxy_check)
        proxy_row = QHBoxLayout()
        proxy_row.addWidget(QLabel("代理地址："))
        self.proxy_input = QLineEdit()
        self.proxy_input.setPlaceholderText("如 http://127.0.0.1:7890 或 http://user:pass@host:port")
        self.proxy_input.setEnabled(False)
        self.proxy_check.toggled.connect(self.proxy_input.setEnabled)
        proxy_row.addWidget(self.proxy_input, 1)
        proxy_layout.addLayout(proxy_row)
        layout.addWidget(proxy_group)

        layout.addStretch()

    # ---------- 标签页3：下载管理 ----------
    def _init_download_tab(self):
        layout = QVBoxLayout(self.download_tab)
        layout.setSpacing(8)

        # 下载设置：目录 + 并发/重试/限速
        setting_group = QGroupBox("下载设置")
        setting_layout = QVBoxLayout(setting_group)
        dir_row = QHBoxLayout()
        dir_row.addWidget(QLabel("下载目录："))
        self.download_dir_input = QLineEdit("downloads")
        self.download_dir_input.setPlaceholderText("媒体文件保存目录，按 资源类型/域名 自动分子文件夹")
        self.download_dir_btn = QPushButton("浏览...")
        self.download_dir_btn.clicked.connect(self.on_choose_download_dir)
        dir_row.addWidget(self.download_dir_input, 1)
        dir_row.addWidget(self.download_dir_btn)
        setting_layout.addLayout(dir_row)

        ctrl_row = QHBoxLayout()
        self.download_workers_spin = QSpinBox()
        self.download_workers_spin.setRange(1, 16)
        self.download_workers_spin.setValue(4)
        self.download_workers_spin.setToolTip("并发下载数：同时进行下载的工作线程数")
        self.download_retries_spin = QSpinBox()
        self.download_retries_spin.setRange(0, 10)
        self.download_retries_spin.setValue(3)
        self.download_retries_spin.setToolTip("下载失败后的自动重试次数（指数退避）")
        self.speed_limit_spin = QSpinBox()
        self.speed_limit_spin.setRange(0, 100000)
        self.speed_limit_spin.setValue(0)
        self.speed_limit_spin.setSuffix(" KB/s")
        self.speed_limit_spin.setToolTip("单文件下载速度上限（0表示不限速）")
        ctrl_row.addWidget(QLabel("并发下载数："))
        ctrl_row.addWidget(self.download_workers_spin)
        ctrl_row.addWidget(QLabel("失败重试次数："))
        ctrl_row.addWidget(self.download_retries_spin)
        ctrl_row.addWidget(QLabel("下载限速："))
        ctrl_row.addWidget(self.speed_limit_spin)
        ctrl_row.addStretch()
        setting_layout.addLayout(ctrl_row)

        # 存储优化选项
        opt_row = QHBoxLayout()
        self.md5_check = QCheckBox("MD5校验")
        self.md5_check.setChecked(True)
        self.md5_check.setToolTip("服务器提供校验和时验证MD5，不匹配则重新下载")
        self.dedup_check = QCheckBox("图片内容去重")
        self.dedup_check.setChecked(True)
        self.dedup_check.setToolTip("相同内容的图片只保留一份（按文件MD5去重）")
        opt_row.addWidget(self.md5_check)
        opt_row.addWidget(self.dedup_check)
        opt_row.addWidget(QLabel("图片压缩质量："))
        self.compress_quality_spin = QSpinBox()
        self.compress_quality_spin.setRange(0, 100)
        self.compress_quality_spin.setValue(0)
        self.compress_quality_spin.setToolTip("对图片重新压缩（0表示不压缩，值越低体积越小但画质越差）")
        opt_row.addWidget(self.compress_quality_spin)
        opt_row.addStretch()
        self.download_btn = QPushButton("开始下载")
        self.download_btn.setEnabled(False)
        self.download_btn.clicked.connect(self.on_start_download)
        self.stop_download_btn = QPushButton("停止下载")
        self.stop_download_btn.setEnabled(False)
        self.stop_download_btn.clicked.connect(self.on_stop_download)
        opt_row.addWidget(self.download_btn)
        opt_row.addWidget(self.stop_download_btn)
        setting_layout.addLayout(opt_row)

        # 存储空间监控（定时刷新）
        self.disk_status_label = QLabel("存储空间：--")
        setting_layout.addWidget(self.disk_status_label)
        self.disk_timer = QTimer(self)
        self.disk_timer.timeout.connect(self._update_disk_status)
        self.disk_timer.start(5000)
        layout.addWidget(setting_group)

        # 媒体资源列表（下载队列输入）
        layout.addWidget(QLabel("媒体资源列表（爬取结果自动收集，也可手动编辑，每行一个URL）："))
        self.media_list = QTextEdit()
        self.media_list.setPlaceholderText("爬取完成后此处会自动列出发现的图片/视频URL，可直接编辑增删。下载时图片等页面资源优先，视频大文件靠后")
        layout.addWidget(self.media_list, 1)

        # 下载统计
        self.download_stats_label = QLabel("已完成：0/0 | 成功：0 | 失败：0 | 队列中：0")
        layout.addWidget(self.download_stats_label)

        # 失败资源列表 + 操作
        fail_row = QHBoxLayout()
        fail_row.addWidget(QLabel("失败资源列表（重试耗尽后记录）："))
        fail_row.addStretch()
        self.retry_failed_btn = QPushButton("重新下载失败资源")
        self.retry_failed_btn.setEnabled(False)
        self.retry_failed_btn.clicked.connect(self.on_retry_failed)
        self.clear_failed_btn = QPushButton("清空失败列表")
        self.clear_failed_btn.setEnabled(False)
        self.clear_failed_btn.clicked.connect(self.on_clear_failed)
        fail_row.addWidget(self.retry_failed_btn)
        fail_row.addWidget(self.clear_failed_btn)
        layout.addLayout(fail_row)
        self.failed_list = QListWidget()
        self.failed_list.setMaximumHeight(120)
        layout.addWidget(self.failed_list)

        # 下载日志
        self.download_log = QTextEdit()
        self.download_log.setReadOnly(True)
        layout.addWidget(QLabel("下载日志："))
        layout.addWidget(self.download_log, 1)

    # ---------- 反爬设置操作 ----------
    def on_add_ua(self):
        ua = self.ua_add_input.text().strip()
        if not ua:
            QMessageBox.warning(self, "提示", "请输入要添加的User-Agent")
            return
        if self.ua_pool.add(ua):
            self.ua_list.addItem(ua)
            self.ua_add_input.clear()
            self.log_display.append(f"✅ 已添加User-Agent（当前池共{len(self.ua_pool.all())}个）")
        else:
            QMessageBox.information(self, "提示", "该User-Agent已存在或无效")

    def on_del_ua(self):
        item = self.ua_list.currentItem()
        if item is None:
            QMessageBox.warning(self, "提示", "请先选中要删除的User-Agent")
            return
        ua = item.text()
        if len(self.ua_pool.all()) <= 1:
            QMessageBox.warning(self, "提示", "UA池至少需要保留一个User-Agent")
            return
        if self.ua_pool.remove(ua):
            self.ua_list.takeItem(self.ua_list.row(item))
            self.log_display.append(f"已删除User-Agent（当前池共{len(self.ua_pool.all())}个）")

    def on_reset_ua(self):
        self.ua_pool.reset()
        self.ua_list.clear()
        self.ua_list.addItems(self.ua_pool.all())
        self.log_display.append("已恢复默认User-Agent池")

    def _get_proxy_config(self):
        """根据反爬设置返回requests可用的代理字典，未启用或未填地址时返回空字典"""
        if not self.proxy_check.isChecked():
            return {}
        addr = self.proxy_input.text().strip()
        if not addr:
            return {}
        return {"http": addr, "https": addr}

    # ---------- 下载管理操作 ----------
    def _update_disk_status(self):
        """定时刷新下载目录所在磁盘的空间使用情况"""
        download_dir = self.download_dir_input.text().strip() or "downloads"
        try:
            usage = shutil.disk_usage(download_dir if os.path.isdir(download_dir) else ".")
            self.disk_status_label.setText(
                f"存储空间：剩余 {ContentDownloader._fmt_size(usage.free)} / "
                f"已用 {ContentDownloader._fmt_size(usage.used)} / "
                f"总容量 {ContentDownloader._fmt_size(usage.total)}")
        except Exception:
            self.disk_status_label.setText("存储空间：无法读取（目录不存在）")

    def on_choose_download_dir(self):
        directory = QFileDialog.getExistingDirectory(self, "选择下载目录",
                                                     self.download_dir_input.text().strip() or ".")
        if directory:
            self.download_dir_input.setText(directory)
            self._update_disk_status()

    def _launch_downloader(self, urls):
        """创建并启动下载器实例（开始下载与重新下载失败资源共用）"""
        download_dir = self.download_dir_input.text().strip() or "downloads"
        try:
            os.makedirs(download_dir, exist_ok=True)
        except Exception as e:
            QMessageBox.critical(self, "错误", f"无法创建下载目录：{str(e)}")
            return

        self.downloader = ContentDownloader(
            urls, download_dir,
            max_workers=self.download_workers_spin.value(),
            max_retries=self.download_retries_spin.value(),
            rate_limit=self.speed_limit_spin.value() * 1024,
            enable_dedup=self.dedup_check.isChecked(),
            compress_quality=self.compress_quality_spin.value(),
            proxy=self._get_proxy_config(),
            ua_pool=self.ua_pool,
            db_path=self.db_path)
        self.downloader.signal_log.connect(self.download_log.append)
        self.downloader.signal_item_done.connect(self.on_download_item_done)
        self.downloader.signal_progress.connect(self.on_download_progress)
        self.downloader.signal_finish.connect(self.on_download_finish)
        self.downloader.signal_disk_status.connect(self.on_download_disk_status)

        self.download_btn.setEnabled(False)
        self.stop_download_btn.setEnabled(True)
        self.retry_failed_btn.setEnabled(False)
        self.download_stats_label.setText(f"已完成：0/{len(urls)} | 成功：0 | 失败：0 | 队列中：{len(urls)}")
        self.download_log.append(f"=== 开始下载 {len(urls)} 个媒体资源到: {download_dir} ===")
        self.downloader.start()

    def on_start_download(self):
        if self.downloader is not None and self.downloader.isRunning():
            QMessageBox.information(self, "提示", "下载任务正在进行中，请先等待或停止")
            return
        urls = [u.strip() for u in self.media_list.toPlainText().splitlines()
                if u.strip().startswith(('http://', 'https://'))]
        if not urls:
            QMessageBox.warning(self, "提示", "媒体资源列表为空，请先完成爬取或手动粘贴媒体URL")
            return
        self._launch_downloader(urls)

    def on_retry_failed(self):
        """将失败列表中的URL重新加入下载队列"""
        if self.downloader is not None and self.downloader.isRunning():
            QMessageBox.information(self, "提示", "当前下载任务仍在运行，请先等待或停止")
            return
        urls = [self.failed_list.item(i).text() for i in range(self.failed_list.count())]
        if not urls:
            QMessageBox.information(self, "提示", "失败列表为空，没有可重新下载的资源")
            return
        self.failed_list.clear()
        self.clear_failed_btn.setEnabled(False)
        self._launch_downloader(urls)

    def on_clear_failed(self):
        self.failed_list.clear()
        self.clear_failed_btn.setEnabled(False)
        self.retry_failed_btn.setEnabled(False)
        self.download_log.append("已清空失败资源列表")

    def on_stop_download(self):
        if self.downloader is not None and self.downloader.isRunning():
            self.downloader.stop()
            self.download_log.append("⏹ 正在停止下载任务（部分文件可能未完成，下次可断点续传）")

    def on_download_item_done(self, url, path, ok, note):
        if ok:
            tail = f" → {path}" + (f"（{note}）" if note else "")
            self.download_log.append(f"✅ {url}{tail}")
        else:
            self.download_log.append(f"❌ {url}（{note}）")
            if self.failed_list.findItems(url, Qt.MatchFlag.MatchExactly):
                return
            self.failed_list.addItem(url)
            self.retry_failed_btn.setEnabled(True)
            self.clear_failed_btn.setEnabled(True)

    def on_download_progress(self, done, total, success, failed):
        remaining = max(0, total - done)
        self.download_stats_label.setText(
            f"已完成：{done}/{total} | 成功：{success} | 失败：{failed} | 队列中：{remaining}")

    def on_download_disk_status(self, text):
        self.disk_status_label.setText(f"存储空间：{text}")

    def on_download_finish(self, success, failed, failed_urls, manifest_path):
        self.download_btn.setEnabled(True)
        self.stop_download_btn.setEnabled(False)
        self.retry_failed_btn.setEnabled(bool(failed_urls))
        self.clear_failed_btn.setEnabled(bool(failed_urls))
        # 失败列表增量补全（防止finish前个别信号丢失）
        for url in failed_urls:
            if not self.failed_list.findItems(url, Qt.MatchFlag.MatchExactly):
                self.failed_list.addItem(url)
        msg = f"媒体下载结束：\n成功 {success} 个，失败 {failed} 个"
        if manifest_path:
            msg += f"\n\n资源清单：\n{manifest_path}"
        if failed_urls:
            msg += f"\n\n{failed} 个资源下载失败，可点击「重新下载失败资源」重试"
        self.download_log.append(f"🎉 媒体下载结束：成功 {success} 个，失败 {failed} 个")
        QMessageBox.information(self, "下载完成", msg)

    # ---------- 断点续爬：启动时检测未完成任务 ----------
    def _get_unfinished_tasks(self):
        """查询数据库中状态为running的爬取任务"""
        if not os.path.exists(self.db_path):
            return []
        try:
            conn = sqlite3.connect(self.db_path)
            rows = conn.execute(
                "SELECT id, start_url, max_depth FROM crawl_tasks WHERE status='running' ORDER BY id"
            ).fetchall()
            conn.close()
            return [{"id": r[0], "start_url": r[1], "max_depth": r[2]} for r in rows]
        except Exception:
            return []

    def _abandon_unfinished_tasks(self):
        """将未完成任务标记为完成（用户选择开始新任务时调用）"""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                "UPDATE crawl_tasks SET status='completed', finished_at=datetime('now','localtime') "
                "WHERE status='running'")
            conn.commit()
            conn.close()
        except Exception:
            pass

    def check_unfinished_task(self):
        """启动时检测未完成任务，提供"继续上次任务"或"开始新任务"选项"""
        tasks = self._get_unfinished_tasks()
        if not tasks:
            return
        task = tasks[0]
        reply = QMessageBox.question(
            self, "发现未完成的任务",
            f"检测到上次存在未完成的爬取任务：\n\n"
            f"起始网址：{task['start_url']}\n"
            f"递归深度：{task['max_depth']}\n\n"
            f"是否继续上次任务？\n（已爬取的页面会自动跳过）\n"
            f"选择\"否\"则放弃上次任务并开始新任务。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            self.pending_crawl_url = task["start_url"]
            self.url_input.setText(task["start_url"])
            self.depth_spin.setValue(task["max_depth"])
            self.log_display.append("=== 检测到未完成任务，正在继续上次任务（已爬页面自动跳过） ===")
            self._start_crawl_with(task["start_url"], task["max_depth"], resume_task_id=task["id"])
        elif reply == QMessageBox.StandardButton.No:
            self._abandon_unfinished_tasks()
            self.log_display.append("已放弃上次未完成任务，请配置新的爬取任务")

    # ---------- URL过滤规则（工具栏输入框 / 过滤管理对话框，共享URLFilter实例） ----------
    def _on_block_regex_changed(self, text):
        """工具栏输入框内容变化：解析分号分隔的多个正则，批量同步到URLFilter（实时生效）"""
        if self._syncing_block_input:
            return  # 程序自动同步文本时跳过，避免递归
        patterns = [p.strip() for p in re.split(r"[;；]", text) if p.strip()]
        self.url_filter.set_block_patterns(patterns)
        self._update_filter_stats()

    def _sync_block_input(self):
        """将URLFilter中的全部屏蔽正则同步回工具栏输入框（分号分隔）"""
        text = ";".join(self.url_filter.block_patterns())
        if text == self.block_regex_input.text():
            return
        self._syncing_block_input = True
        try:
            self.block_regex_input.setText(text)
        finally:
            self._syncing_block_input = False
        self._update_filter_stats()

    def _update_filter_stats(self):
        """刷新工具栏过滤统计显示"""
        stats = self.url_filter.get_stats()
        self.filter_stats_label.setText(f"已过滤URL：{stats['filtered_count']}")

    def on_open_filter_dialog(self):
        """打开URL过滤规则管理对话框（正则/白名单/统计样本/导入导出）"""
        dialog = URLFilterDialog(self, self.url_filter)
        # 关闭时把对话框内的规则变更同步回工具栏输入框
        dialog.accepted.connect(self._sync_block_input)
        dialog.exec()

    def _toggle_pause(self):
        """切换暂停/继续状态"""
        thread = getattr(self, "crawl_thread", None)
        if thread is not None and thread.isRunning():
            if thread._pause_event.is_set():
                thread.pause()
                self.log_display.append("⏸ 已暂停爬取（按 Ctrl+P / Ctrl+R 继续）")
            else:
                thread.resume()
                self.log_display.append("▶ 已继续爬取")

    @staticmethod
    def validate_url(url):
        """校验URL是否合法，返回 (是否合法, 错误信息)"""
        if not url or not url.strip():
            return False, "请输入起始网址"
        url = url.strip()
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False, "网址必须以http://或https://开头"
        if not parsed.netloc:
            return False, "网址无效，请输入包含有效域名的完整网址，例如：https://www.example.com"
        return True, ""

    def on_check_robots(self):
        ok, err_msg = self.validate_url(self.url_input.text())
        if not ok:
            QMessageBox.warning(self, "提示", err_msg)
            return

        self.start_check_btn.setEnabled(False)
        self.robots_display.setText("正在获取robots.txt内容...")
        
        self.robots_thread = RobotsCheckThread(self.url_input.text().strip())
        self.robots_thread.signal_robots_content.connect(self.on_receive_robots)
        self.robots_thread.signal_error.connect(self.on_robots_error)
        self.robots_thread.start()

    def on_receive_robots(self, content, robots_url):
        self.robots_display.setText(f"robots.txt 地址：{robots_url}\n\n{content}")
        self.pending_crawl_url = self.url_input.text().strip()
        self.robots_content = content
        self.start_check_btn.setEnabled(True)

        # 解析robots.txt规则，若禁止爬虫则直接结束爬取流程并弹窗提示
        parser = RobotsParser(content)
        if parser.is_disallowed(self.pending_crawl_url, CRAWLER_USER_AGENT):
            self.confirm_crawl_btn.setEnabled(False)
            self.log_display.append("robots.txt 规则禁止爬虫访问，已终止爬取流程")
            QMessageBox.warning(self, "robots.txt 禁止爬取",
                                f"该站点的 robots.txt 规则禁止爬虫访问：\n\n{self.pending_crawl_url}\n\n已终止爬取流程。")
            return

        self.confirm_crawl_btn.setEnabled(True)

        reply = QMessageBox.question(self, "爬取确认", 
                                     f"已获取站点robots规则，即将启动最大深度为{self.depth_spin.value()}的递归爬取，请确认你已遵守站点爬取规范，是否继续？",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            self.on_start_crawl()

    def on_robots_error(self, error_msg):
        self.robots_display.setText(error_msg)
        self.start_check_btn.setEnabled(True)
        QMessageBox.critical(self, "错误", error_msg)

    def on_start_crawl(self):
        ok, err_msg = self.validate_url(self.pending_crawl_url)
        if not ok:
            QMessageBox.warning(self, "提示", err_msg)
            return

        # 兜底校验：若robots.txt规则禁止爬取则直接结束
        if self.robots_content:
            parser = RobotsParser(self.robots_content)
            if parser.is_disallowed(self.pending_crawl_url, CRAWLER_USER_AGENT):
                self.confirm_crawl_btn.setEnabled(False)
                QMessageBox.warning(self, "robots.txt 禁止爬取",
                                    "该站点的 robots.txt 规则禁止爬虫访问，已终止爬取流程。")
                return

        self._start_crawl_with(self.pending_crawl_url, self.depth_spin.value())

    def _start_crawl_with(self, url, max_depth, resume_task_id=None):
        """启动爬取线程（首次启动与断点续爬共用）

        resume_task_id: 续爬时传入原任务ID，复用其已爬记录实现断点续爬；
                        None 表示全新任务（全量爬取，不受历史记录影响）。
        """
        self.confirm_crawl_btn.setEnabled(False)
        self.log_display.append("=== 递归爬取任务启动 ===")

        # 重置本次任务结果缓存
        self.media_urls = []
        self.last_crawl_links = []
        self.last_page_texts = {}

        self.crawl_thread = RecursiveCrawlerThread(
            url, max_depth,
            crawl_external=self.crawl_external_check.isChecked(),
            request_delay=self.delay_spin.value(),
            max_chars_per_page=self.max_chars_spin.value(),
            link_filter=self.link_filter_input.text().strip(),
            db_path=self.db_path,
            ua_pool=self.ua_pool,
            jitter=self.jitter_spin.value(),
            proxy=self._get_proxy_config(),
            url_filter=self.url_filter,  # 共享过滤规则实例，实时生效
            max_pages=self.max_pages_spin.value(),  # 自动停止阈值
            resume_task_id=resume_task_id)  # 续爬时复用原任务ID

        self.crawl_thread.signal_log.connect(self.log_display.append)
        self.crawl_thread.signal_single_page_done.connect(lambda url, info: self.log_display.append(f"✅ 完成 [{url}] {info}"))
        self.crawl_thread.signal_progress.connect(self.on_crawl_progress)
        self.crawl_thread.signal_media_found.connect(self.on_media_found)
        self.crawl_thread.signal_finish.connect(self.on_crawl_finish)
        self.crawl_thread.signal_stopped.connect(self.on_crawl_stopped)
        self.crawl_thread.signal_error.connect(self.on_crawl_error)
        self.crawl_thread.start()
        # 启动后启用停止按钮（手动停止入口）
        self.stop_crawl_btn.setEnabled(True)

    def on_crawl_progress(self, pages, depth, max_depth):
        self.crawl_stats_label.setText(
            f"已爬页面：{pages} | 当前层：{min(depth + 1, max_depth + 1)}/{max_depth + 1} | "
            f"提取链接：{len(self.last_crawl_links)} | 媒体资源：{len(self.media_urls)}")

    def on_media_found(self, media_urls):
        """爬取线程回传发现的媒体资源列表"""
        self.media_urls = list(media_urls)
        self.media_list.setPlainText("\n".join(self.media_urls))
        self.download_btn.setEnabled(bool(self.media_urls))
        self.log_display.append(f"📎 本次爬取共发现 {len(self.media_urls)} 个媒体资源，可前往【下载管理】标签页下载")
        self.crawl_stats_label.setText(
            f"已爬页面：{len(self.last_crawl_links) if self.last_crawl_links else 0} | 当前层：完成 | "
            f"提取链接：{len(self.last_crawl_links)} | 媒体资源：{len(self.media_urls)}")

    # ---------- 停止爬取（手动/自动） ----------
    def on_stop_crawl(self):
        """手动停止按钮：请求爬取线程停止并清理资源。

        停止过程：设置停止标志 → 中断当前HTTP请求 → 线程在检查点退出 →
        已爬数据已写入数据库且任务保持running状态，重启后可选"继续上次任务"。
        """
        thread = getattr(self, "crawl_thread", None)
        if thread is None or not thread.isRunning():
            self.stop_crawl_btn.setEnabled(False)
            return
        self.stop_crawl_btn.setEnabled(False)
        self.log_display.append("⏹ 正在停止爬取任务：中断请求并保存进度...")
        thread.stop()  # 线程安全：设置标志并关闭进行中的响应

    def on_crawl_stopped(self, total_count, all_links, page_texts):
        """爬取线程手动停止后回传部分结果（GUI线程处理）"""
        self.log_display.append(f"⏹ 爬取任务已停止，累计爬取 {total_count} 个页面（进度已保存，可继续上次任务）")
        self.stop_crawl_btn.setEnabled(False)
        # 输出已收集到的链接
        self.link_display.setText(f"共提取到 {len(all_links)} 个唯一链接（已停止，部分结果）：\n" + "-" * 50 + "\n")
        for idx, link in enumerate(all_links, 1):
            self.link_display.append(f"{idx}. {link}")
        # 缓存部分结果供导出
        self.last_crawl_links = all_links
        self.last_page_texts = page_texts
        self.confirm_crawl_btn.setEnabled(True)
        self.export_csv_btn.setEnabled(True)
        self.export_txt_btn.setEnabled(True)
        self.crawl_stats_label.setText(
            f"已爬页面：{total_count} | 当前层：已停止 | "
            f"提取链接：{len(all_links)} | 媒体资源：{len(self.media_urls)}")
        QMessageBox.information(self, "爬取已停止",
                                f"爬取任务已停止，共爬取 {total_count} 个页面，提取到 {len(all_links)} 个链接。\n"
                                f"进度已保存：重启程序后可选择「继续上次任务」。")

    def on_crawl_finish(self, total_count, all_links, page_texts):
        self.log_display.append(f"\n🎉 全部爬取完成，累计成功爬取 {total_count} 个页面")
        
        # 输出全部提取到的链接
        self.link_display.setText(f"共提取到 {len(all_links)} 个唯一链接：\n" + "-"*50 + "\n")
        for idx, link in enumerate(all_links, 1):
            self.link_display.append(f"{idx}. {link}")
        
        # 缓存结果供导出CSV/TXT使用
        self.last_crawl_links = all_links
        self.last_page_texts = page_texts
        self.confirm_crawl_btn.setEnabled(True)
        self.stop_crawl_btn.setEnabled(False)
        self.export_csv_btn.setEnabled(True)
        self.export_txt_btn.setEnabled(True)
        self.crawl_stats_label.setText(
            f"已爬页面：{total_count} | 当前层：完成 | "
            f"提取链接：{len(all_links)} | 媒体资源：{len(self.media_urls)}")
        QMessageBox.information(self, "爬取完成", f"递归爬取已结束，共爬取 {total_count} 个页面，提取到 {len(all_links)} 个链接，发现 {len(self.media_urls)} 个媒体资源")

    def on_crawl_error(self, error_msg):
        self.log_display.append(error_msg)
        self.confirm_crawl_btn.setEnabled(True)
        self.stop_crawl_btn.setEnabled(False)
        QMessageBox.critical(self, "爬取异常", error_msg)

    def on_export_csv(self):
        """导出按钮：打开导出配置对话框（默认CSV格式）"""
        self._open_export_dialog("CSV")

    def on_export_txt(self):
        """导出按钮：打开导出配置对话框（默认Markdown格式）"""
        self._open_export_dialog("Markdown")

    def _open_export_dialog(self, default_format):
        """打开ExportDialog，按用户选择的格式与字段导出"""
        if not self.last_crawl_links:
            QMessageBox.warning(self, "提示", "当前没有可导出的爬取结果，请先完成一次爬取")
            return

        dialog = ExportDialog(self, default_format=default_format,
                              has_text=bool(self.last_page_texts))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        fmt = dialog.selected_format()
        fields = dialog.selected_fields()
        if not fields:
            QMessageBox.warning(self, "提示", "请至少勾选一个导出字段")
            return

        if fmt == "csv":
            self._export_csv(fields)
        elif fmt == "json":
            self._export_json(fields)
        else:
            self._export_markdown(fields)

    def _row_data(self, link, fields):
        """按字段列表生成一行数据"""
        row = []
        for f in fields:
            if f == "url":
                row.append(link)
            elif f == "domain":
                row.append(urlparse(link).netloc)
            elif f == "text":
                row.append(self.last_page_texts.get(link, ""))
        return row

    def _export_csv(self, fields):
        default_name = f"crawl_results_{time.strftime('%Y%m%d_%H%M%S')}.csv"
        file_path, _ = QFileDialog.getSaveFileName(self, "导出爬取结果到CSV", default_name,
                                                   "CSV 文件 (*.csv);;所有文件 (*)")
        if not file_path:
            return
        try:
            # 使用UTF-8 with BOM，避免Excel打开中文乱码
            with open(file_path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(fields)
                for link in self.last_crawl_links:
                    writer.writerow(self._row_data(link, fields))
            self.log_display.append(f"✅ 已导出 {len(self.last_crawl_links)} 条链接到: {file_path}")
            QMessageBox.information(self, "导出成功", f"已成功导出 {len(self.last_crawl_links)} 条链接到：\n{file_path}")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", f"写入CSV文件失败：{str(e)}")

    def _export_json(self, fields):
        default_name = f"crawl_results_{time.strftime('%Y%m%d_%H%M%S')}.json"
        file_path, _ = QFileDialog.getSaveFileName(self, "导出爬取结果到JSON", default_name,
                                                   "JSON 文件 (*.json);;所有文件 (*)")
        if not file_path:
            return
        try:
            records = []
            for link in self.last_crawl_links:
                row = self._row_data(link, fields)
                records.append(dict(zip(fields, row)))
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(records, f, ensure_ascii=False, indent=2)
            self.log_display.append(f"✅ 已导出 {len(records)} 条记录到: {file_path}")
            QMessageBox.information(self, "导出成功", f"已成功导出 {len(records)} 条记录到：\n{file_path}")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", f"写入JSON文件失败：{str(e)}")

    def _export_markdown(self, fields):
        default_name = f"crawl_results_{time.strftime('%Y%m%d_%H%M%S')}.md"
        file_path, _ = QFileDialog.getSaveFileName(self, "导出爬取结果到Markdown", default_name,
                                                   "Markdown 文件 (*.md);;所有文件 (*)")
        if not file_path:
            return
        try:
            lines = ["| " + " | ".join(fields) + " |",
                     "|" + "|".join(["---"] * len(fields)) + "|"]
            for link in self.last_crawl_links:
                row = self._row_data(link, fields)
                # 表格转义：竖线转义、换行转空格、文字截断防止表格过长
                escaped = []
                for cell in row:
                    cell = str(cell)
                    if len(cell) > 200:
                        cell = cell[:200] + "…"
                    escaped.append(cell.replace("|", "\\|").replace("\n", " "))
                lines.append("| " + " | ".join(escaped) + " |")
            with open(file_path, "w", encoding="utf-8-sig") as f:
                f.write("\n".join(lines))
            self.log_display.append(f"✅ 已导出 {len(self.last_crawl_links)} 条记录到: {file_path}")
            QMessageBox.information(self, "导出成功", f"已成功导出 {len(self.last_crawl_links)} 条记录到：\n{file_path}")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", f"写入Markdown文件失败：{str(e)}")

    def on_clear_all(self):
        self.url_input.clear()
        self.robots_display.clear()
        self.log_display.clear()
        self.link_display.clear()
        self.media_list.clear()
        self.download_log.clear()
        self.failed_list.clear()
        self.confirm_crawl_btn.setEnabled(False)
        self.stop_crawl_btn.setEnabled(False)
        self.export_csv_btn.setEnabled(False)
        self.export_txt_btn.setEnabled(False)
        self.download_btn.setEnabled(False)
        self.retry_failed_btn.setEnabled(False)
        self.clear_failed_btn.setEnabled(False)
        self.crawl_stats_label.setText("已爬页面：0 | 当前层：-/- | 提取链接：0 | 媒体资源：0")
        self.download_stats_label.setText("已完成：0/0 | 成功：0 | 失败：0 | 队列中：0")
        self.pending_crawl_url = ""
        self.robots_content = ""
        self.last_crawl_links = []
        self.last_page_texts = {}
        self.media_urls = []

    def on_clear_crawl_records(self):
        """清空数据库中的已爬URL记录，使重新爬取同一站点不受历史去重影响"""
        if self.crawl_thread is not None and self.crawl_thread.isRunning():
            QMessageBox.warning(self, "提示", "请先停止正在运行的爬取任务，再清空爬取记录。")
            return
        reply = QMessageBox.question(
            self, "清空爬取记录",
            "确定要清空数据库中的已爬URL记录吗？\n\n"
            "清空后重新爬取同一站点时，将不再跳过历史URL（已下载的媒体文件不受影响）。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.execute("SELECT COUNT(*) FROM crawled_urls")
            count = cur.fetchone()[0]
            conn.execute("DELETE FROM crawled_urls")
            conn.commit()
            conn.close()
            self.log_display.append(f"🗑 已清空 {count} 条爬取记录，重新爬取同一站点将不再被历史记录跳过")
        except Exception as e:
            QMessageBox.critical(self, "错误", f"清空爬取记录失败：{str(e)}")

    # ---------- 安全退出与命令行停止 ----------
    # ---------- 任务管理标签页（多目标并发爬取） ----------
    def _init_task_manager_tab(self):
        """初始化「任务管理」标签页。
        延迟导入 multi_task_ui（multi_task 依赖本模块的爬虫线程类，
        运行时导入可避免模块顶层循环依赖）。"""
        from multi_task_ui import TaskManagerTab
        self.task_tab = TaskManagerTab(main_window=self)
        self.tabs.addTab(self.task_tab, "任务管理")

    def _stop_task_manager(self):
        """停止任务管理器中所有运行中的多任务爬虫并保存任务队列（窗口关闭时调用）"""
        task_tab = getattr(self, "task_tab", None)
        if task_tab is not None:
            try:
                task_tab.scheduler.shutdown(save_queue=True)
            except Exception:
                pass

    def request_stop_and_quit(self):
        """命令行/信号触发（如 Ctrl+C）的停止与安全退出。

        停止所有运行中的爬取/下载线程并等待资源清理完成后关闭窗口。
        已爬数据已写入数据库，重启后可选择"继续上次任务"恢复。
        """
        self.log_display.append("⏹ 收到停止指令（Ctrl+C），正在停止任务并安全退出...")
        thread = getattr(self, "crawl_thread", None)
        if thread is not None and thread.isRunning():
            thread.stop()
            thread.wait(5000)  # 等待线程清理资源并退出
        downloader = self.downloader
        if downloader is not None and downloader.isRunning():
            downloader.stop()
            downloader.wait(5000)
        self.close()  # 触发closeEvent完成退出

    def closeEvent(self, event):
        """窗口关闭事件：停止运行中的任务并清理资源后再退出。

        若存在运行中的爬取/下载线程，先询问用户，确认后停止并等待其退出，
        避免残留线程或未释放的连接导致进程无法安全结束。
        """
        # 命令行停止时跳过询问，直接清理退出
        if getattr(self, "_force_close", False):
            self._stop_task_manager()  # 多任务管理器：停止并保存任务队列
            event.accept()
            return
        thread = getattr(self, "crawl_thread", None)
        downloader = self.downloader
        task_tab = getattr(self, "task_tab", None)
        running = (thread is not None and thread.isRunning()) or \
                  (downloader is not None and downloader.isRunning()) or \
                  (task_tab is not None and task_tab.scheduler.has_running_tasks())
        if running:
            reply = QMessageBox.question(
                self, "退出确认",
                "仍有任务正在运行（爬取/下载/多任务管理）。\n\n"
                "停止任务并退出？\n（已爬取的数据与进度会保存，重启后可继续上次任务）",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            if thread is not None and thread.isRunning():
                thread.stop()
                thread.wait(5000)
            if downloader is not None and downloader.isRunning():
                downloader.stop()
                downloader.wait(5000)
            self._stop_task_manager()  # 多任务管理器：停止并保存任务队列
        event.accept()


if __name__ == "__main__":
    import signal

    app = QApplication(sys.argv)
    window = CrawlerMainWindow()
    window.show()

    # Qt事件循环默认阻塞在select上，Python信号处理器无法执行；
    # 用定时器周期性唤醒主线程，使Ctrl+C（SIGINT）能可靠触发命令行停止
    sig_wake_timer = QTimer()
    sig_wake_timer.timeout.connect(lambda: None)
    sig_wake_timer.start(500)

    def _sigint_handler(sig, frame):
        """Ctrl+C命令行停止：停止任务、清理资源后安全退出"""
        window._force_close = True  # 跳过关闭确认
        window.request_stop_and_quit()

    signal.signal(signal.SIGINT, _sigint_handler)
    sys.exit(app.exec())
