"""媒体下载管理器（图片/视频/文档等）。

``ContentDownloader`` 特性：
- 优先级队列 + 独立工作线程池（并发数可配置）
- 1MB分块下载 + Range断点续传 + 下载速度限制
- Content-Type/扩展名自动识别资源类型，按 <类型>/<域名> 分目录存储
- MD5校验（服务器提供校验和时验证）、失败自动重试
- 相同图片内容去重、图片压缩（Pillow，质量可调）
- 下载完成后生成资源清单 index.html / resources.txt

数据库记录委托给 manager/db_manager（本模块只依赖其叶子模块，无循环导入）。
"""

from __future__ import annotations

import base64
import hashlib
import os
import queue
import re
import shutil
import threading
import time
from urllib.parse import urlparse, unquote

import requests
from PyQt6.QtCore import QThread, pyqtSignal

from models import DownloadTask
from utils.helpers import format_size
from utils.user_agents import UserAgentPool
from manager.db_manager import save_media_state
from manager.thread_manager import spawn_workers

try:  # Pillow 为可选依赖，缺失时跳过图片压缩功能
    from PIL import Image
    HAS_PIL = True
except ImportError:
    Image = None
    HAS_PIL = False


class ContentDownloader(QThread):
    """媒体下载管理器。"""

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
        """记录媒体下载状态到数据库（委托 manager/db_manager，内部容错）"""
        save_media_state(self.db_path, url, file_path, status)

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
    def _emit_disk_status(self, label=""):
        try:
            usage = shutil.disk_usage(self.download_dir)
            self.signal_disk_status.emit(
                f"{label} | 剩余空间：{format_size(usage.free)} / 总空间：{format_size(usage.total)}")
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
                    f"<td>{format_size(row['size'])}</td>"
                    f"<td><code>{row['md5'][:12]}</code></td></tr>")
            html_lines.append("</table></body></html>")
            with open(os.path.join(self.download_dir, "index.html"), "w", encoding="utf-8") as f:
                f.write("\n".join(html_lines))

            txt_lines = [f"下载资源清单  生成时间：{now}", "=" * 60, ""]
            for row in rows:
                txt_lines.append(f"[{row['type']}] {row['url']}\n"
                                 f"     → {row['path']} ({format_size(row['size'])}, MD5:{row['md5']})")
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

        workers = spawn_workers(self.max_workers, self._worker, name_prefix="downloader")
        for w in workers:
            w.join()

        manifest = self._generate_manifest()
        self._emit_disk_status("任务结束")
        self.signal_finish.emit(self._success, self._failed, list(self.failed_urls), manifest)


__all__ = ["ContentDownloader", "DownloadTask"]
