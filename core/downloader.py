"""媒体下载管理器（图片/视频/文档等）。

``ContentDownloader`` 特性：
- 优先级队列 + 独立工作线程池（并发数可配置）
- 1MB分块下载 + Range断点续传 + 下载速度限制
- Content-Type/扩展名自动识别资源类型，按 <类型>/<域名>[/合集] 分目录存储
- 支持传入 ``MediaItem`` 列表：复用抓取阶段带下来的请求头（防盗链）与命名信息
- MD5校验（服务器提供校验和时验证）、失败自动重试
- 质量校验（MediaConfig）：尺寸/体积不达标的图**下载后丢弃**，
  与 ``core/pipeline`` 的提前过滤构成闭环（提前拿不到尺寸时在此兜底）
- 相同图片内容去重（MD5 + 可选感知哈希）、图片压缩（Pillow，质量可调）
- 下载完成后生成资源清单 index.html / resources.txt

资源类型判定统一委托 ``core.media.detector``（唯一口径），
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
from core.fetcher.base import decode_bytes, guess_encoding
from core.media.detector import (
    EXT_MAP as _DETECTOR_EXT_MAP,
    TYPE_DASH,
    TYPE_PRIORITY as _DETECTOR_TYPE_PRIORITY,
    STREAM_TYPES,
    classify_content_type,
    classify_url,
    guess_extension,
)
from core.media.models import MediaConfig, MediaItem
from core.media.probe import HAS_PIL, dhash, is_duplicate_hash
from core.pipeline import check_local_file
from utils.helpers import format_size
from utils.user_agents import UserAgentPool
from manager.db_manager import save_media_state
from manager.thread_manager import spawn_workers

#: 传入 MediaItem 时的默认命名模板（仅作用于 <类型>/<域名> 之下）
DEFAULT_FILENAME_TEMPLATE = "{album}/{index:03d}_{title}{ext}"
#: 被质量过滤（而非下载失败）的说明前缀
FILTERED_PREFIX = "已过滤："

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
    # 以下常量与判定方法统一来自 core.media.detector（保留类属性以兼容既有引用）
    TYPE_PRIORITY = _DETECTOR_TYPE_PRIORITY
    EXT_MAP = _DETECTOR_EXT_MAP

    def __init__(self, urls, download_dir, max_workers=4, max_retries=3,
                 rate_limit=0, enable_dedup=True, compress_quality=0,
                 proxy=None, ua_pool=None, db_path="crawler_cache.db",
                 referer=None, verify_ssl=True,
                 connect_timeout=10.0, read_timeout=60.0,
                 items=None, filename_template=DEFAULT_FILENAME_TEMPLATE,
                 media_config=None):
        super().__init__()
        self.urls = list(urls)
        # MediaItem 索引：url -> MediaItem（由媒体抓取线程传入，携带请求头与命名信息）
        # 不传 items 时行为与历史版本完全一致（仅按 URL 处理）
        self._items = {}
        for item in (items or []):
            if isinstance(item, MediaItem) and item.url:
                self._items[item.url] = item
        self.filename_template = filename_template or DEFAULT_FILENAME_TEMPLATE
        # 媒体质量配置：尺寸/体积/SVG 过滤与感知去重开关（None=不做质量过滤）
        self.media_config = media_config if isinstance(media_config, MediaConfig) \
            else MediaConfig()
        self.download_dir = download_dir
        self.max_workers = max(1, max_workers)      # 并发下载数控制
        self.max_retries = max(0, max_retries)      # 失败自动重试次数
        self.rate_limit = max(0, rate_limit)        # 下载速度限制(bytes/s)，0=不限
        self.enable_dedup = enable_dedup            # 相同图片内容去重
        self.compress_quality = compress_quality    # 图片压缩质量(0=不压缩)
        self.proxy = proxy or {}
        self.ua_pool = ua_pool or UserAgentPool()
        self.db_path = db_path
        # 请求层配置
        self.referer = referer                      # None=按目标域名自动推导 Referer（防盗链）
        self.verify_ssl = verify_ssl                # 是否校验SSL证书（默认校验）
        # connect / read 分离超时：大文件下载不受连接建立耗时拖累
        self.timeout = (max(1.0, float(connect_timeout)),
                        max(1.0, float(read_timeout)))
        self._local = threading.local()             # 每线程独立的 requests.Session
        self._path_lock = threading.Lock()
        self._path_owner = {}                       # 保存路径 -> 来源URL（同名不同URL防覆盖）

        self._queue = queue.PriorityQueue()  # 优先级队列
        self._stop_event = threading.Event()
        self._active_lock = threading.Lock()
        self._active = 0
        self._done_lock = threading.Lock()
        self._success = 0
        self._failed = 0
        self._filtered = 0
        self.failed_urls = []              # 最终失败（重试耗尽）的URL列表
        self.filtered_urls = []            # 下载后不达质量标准而丢弃的URL列表
        self._total = 0
        # 图片去重：文件MD5 -> 已保存路径（仅对image类型生效）
        self._dedup_lock = threading.Lock()
        self._image_hashes = {}
        # 感知哈希去重：[(dhash, 已保留路径)]（近似同图，需 Pillow）
        self._perceptual_hashes = []
        # 资源清单数据
        self._manifest_lock = threading.Lock()
        self._manifest_rows = []

    def stop(self):
        """请求停止下载（正在执行的任务会在下一个分块处退出）"""
        self._stop_event.set()

    # ---------- 资源类型识别与优先级（委托 core.media.detector） ----------
    @classmethod
    def _classify_url(cls, url):
        """根据URL扩展名初步推断资源类型。

        流媒体（hls/dash）在此归入 ``video``：下载器按普通单文件处理，
        分片合并由可选的 core.media.hls / merger 负责，与此处互不影响。
        """
        kind = classify_url(url)
        return "video" if kind in STREAM_TYPES else kind

    @classmethod
    def _classify_content_type(cls, content_type):
        """根据Content-Type推断资源类型，无法识别时返回None（流媒体同样归入video）"""
        kind = classify_content_type(content_type)
        return "video" if kind in STREAM_TYPES else kind

    def _priority_of(self, url):
        """URL入队时的初始优先级（页面资源优先）"""
        return self.TYPE_PRIORITY.get(self._classify_url(url), self.TYPE_PRIORITY["other"])

    @classmethod
    def _guess_extension(cls, content_type):
        """根据Content-Type推断文件扩展名，无法识别时返回.bin"""
        return guess_extension(content_type)

    # ---------- 保存路径：按类型/域名分文件夹 ----------
    def _resolve_save_path(self, url, resource_type, item=None, ext_override=""):
        """保存路径：<下载目录>/<资源类型>/<域名>[/<模板相对路径>]。

        - 传入 MediaItem 时用其命名模板（合集/序号/标题）生成相对路径，
          未传入时沿用"URL 文件名"的历史行为
        - ``ext_override``：强制最终扩展名（流媒体合并后为 .mp4/.ts，
          与播放列表 URL 的 .m3u8/.mpd 不同）
        - 文件名中的非法字符（\\ / : * ? " < > |）替换为下划线
        - 无扩展名时用URL哈希占位
        - 同一路径已属于其它URL时追加URL哈希后缀，避免同名文件相互覆盖
          （历史遗留的同名文件仍按“同一URL续传”处理，保持断点续传能力）
        """
        parsed = urlparse(url)
        host = parsed.netloc.replace(":", "_") or "unknown_host"
        folder = os.path.join(self.download_dir, resource_type, host)
        rel = item.filename(self.filename_template) if item is not None else ""
        if rel:
            save_dir = os.path.join(folder, os.path.dirname(rel))
            name = os.path.basename(rel)
        else:
            save_dir = folder
            name = unquote(os.path.basename(parsed.path))
        os.makedirs(save_dir, exist_ok=True)
        name = re.sub(r'[\\/:*?"<>|]', "_", name)
        if not name or "." not in name:
            name = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        if ext_override:
            name = os.path.splitext(name)[0] + ext_override
        path = os.path.join(save_dir, name)
        with self._path_lock:
            owner = self._path_owner.get(path)
            if owner is not None and owner != url:
                stem, ext = os.path.splitext(name)
                path = os.path.join(
                    save_dir, f"{stem}_{hashlib.sha256(url.encode('utf-8')).hexdigest()[:8]}{ext}")
            self._path_owner[path] = url
        return path

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
        """图片去重：先按内容 MD5 精确去重，再按感知哈希做近似去重。

        - MD5：字节完全一致（同一文件被多个 URL 引用，最常见）
        - 感知哈希（dhash）：同一张图被压成不同尺寸/质量（不同 CDN 副本），
          需 Pillow 支持，由 ``MediaConfig.dedup_perceptual`` 控制
        :return: (保留路径, 说明)；被去重时返回已保留的那一份
        """
        if not self.enable_dedup or resource_type != "image":
            return path, ""
        try:
            md5 = self._file_md5(path)
            with self._dedup_lock:
                existing = self._image_hashes.get(md5)
                if existing is None:
                    self._image_hashes[md5] = path
                elif existing != path and os.path.exists(existing):
                    os.remove(path)
                    return existing, "内容重复已去重"
            return self._dedup_image_perceptual(path)
        except Exception:
            return path, ""

    def _dedup_image_perceptual(self, path):
        """感知哈希近似去重（同图不同压缩质量/尺寸时命中）。"""
        if not (HAS_PIL and self.media_config.dedup_perceptual):
            return path, ""
        try:
            digest = dhash(path)
        except Exception:
            return path, ""
        if not digest:
            return path, ""
        with self._dedup_lock:
            for known, keeper in self._perceptual_hashes:
                if is_duplicate_hash(digest, known) and os.path.exists(keeper):
                    if os.path.abspath(keeper) != os.path.abspath(path):
                        os.remove(path)
                        return keeper, "感知近似重复已去重"
                    return path, ""
            self._perceptual_hashes.append((digest, path))
        return path, ""

    def _quality_reason(self, path):
        """按 MediaConfig 校验已落盘文件；不合格返回原因（合格返回空串）。"""
        try:
            ok, reason = check_local_file(path, self.media_config)
        except Exception:
            return ""
        return "" if ok else reason

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

    # ---------- HTTP 请求层：Session 复用 + 统一请求头 ----------
    def _get_session(self):
        """返回当前线程的 requests.Session（复用连接池，避免每次请求重新握手）"""
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            adapter = requests.adapters.HTTPAdapter(pool_connections=8,
                                                    pool_maxsize=8, max_retries=0)
            session.mount("http://", adapter)
            session.mount("https://", adapter)
            self._local.session = session
        return session

    def _close_session(self):
        """关闭当前线程的 Session（工作线程退出时调用）"""
        session = getattr(self._local, "session", None)
        if session is not None:
            try:
                session.close()
            finally:
                self._local.session = None

    def _ua_for(self, url):
        """同一URL固定使用同一UA：探测/下载/续传之间保持一致，避免被站点拒绝"""
        uas = self.ua_pool.all()
        if not uas:
            return ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
        return uas[hashlib.sha256(url.encode("utf-8")).digest()[0] % len(uas)]

    def _referer_for(self, url):
        """Referer：显式配置优先，否则按目标域名与协议推导（应对常见防盗链）"""
        if self.referer:
            return self.referer
        parsed = urlparse(url)
        host = parsed.netloc
        if not host:
            return ""
        scheme = parsed.scheme if parsed.scheme in ("http", "https") else "https"
        return f"{scheme}://{host}/"

    def _build_headers(self, url, extra=None):
        """统一请求头：固定UA + 同域Referer + 二进制语义（禁用压缩）。

        传入 MediaItem 时优先沿用抓取阶段带下来的请求头：
        浏览器/XHR 请求使用过的 Cookie、Referer 等能有效绕过常见防盗链。
        优先级：显式 extra(Range) > MediaItem 请求头 > 默认值。
        """
        headers = {
            "User-Agent": self._ua_for(url),
            "Accept": "*/*",
            # 二进制资源禁用压缩：保证 Content-Length / Range 语义与实际字节数一致
            "Accept-Encoding": "identity",
            "Connection": "keep-alive",
        }
        item = self._items.get(url)
        if item is not None and item.headers:
            headers.update({k: v for k, v in item.headers.items() if v})
        # Referer：MediaItem 请求头 > 来源页 > 按目标域名推导（应对常见防盗链）
        if not any(key.lower() == "referer" for key in headers):
            referer = (item.referer if item is not None else "") or self._referer_for(url)
            if referer:
                headers["Referer"] = referer
        if extra:
            headers.update(extra)
        return headers

    @staticmethod
    def _io_error_note(err):
        """把写入异常转成可读提示（磁盘满/无权限/文件被占用）"""
        if isinstance(err, PermissionError):
            return "无写入权限或目标文件被其它程序占用"
        if isinstance(err, OSError) and err.errno == 28:  # ENOSPC
            return "磁盘空间不足，写入失败"
        return f"写入失败 {type(err).__name__}: {str(err)[:60]}"

    def _probe_media(self, session, url):
        """轻量探测：Range: bytes=0-0 只取1字节，获取状态码/类型/总长度/校验和。

        相比直接 GET 整个响应头，探测不再触发大文件传输，降低被限速与超时风险。
        :return: (ok, info, error)；info 含 status / content_type / total / md5
        """
        try:
            resp = session.get(url, headers=self._build_headers(url, {"Range": "bytes=0-0"}),
                               proxies=self.proxy, timeout=self.timeout, stream=True,
                               allow_redirects=True, verify=self.verify_ssl)
        except requests.RequestException as e:
            return False, {}, f"{type(e).__name__}: {str(e)[:80]}"
        try:
            status = resp.status_code
            content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            content_range = resp.headers.get("Content-Range") or ""
            total = 0
            if status == 206 and "/" in content_range:
                try:
                    total = int(content_range.rsplit("/", 1)[1])
                except ValueError:
                    total = 0
            if total <= 0:
                try:
                    total = int(resp.headers.get("Content-Length") or 0)
                except ValueError:
                    total = 0
            info = {"status": status, "content_type": content_type,
                    "total": total, "md5": self._header_md5(resp.headers)}
        finally:
            resp.close()
        if status not in (200, 206):
            return False, info, f"HTTP {status}"
        return True, info, ""

    # ---------- 单资源下载流程 ----------
    def _download_one(self, url):
        """下载单个资源：轻量探测→类型/错误页判定→确定路径→断点续传→完整性校验→MD5→压缩→去重"""
        session = self._get_session()
        item = self._items.get(url)
        # 0. 流媒体（HLS/DASH）：走分片解析与合并流程（启用合并时）
        stream_kind = item.kind if item is not None else classify_url(url)
        if self.media_config.merge_stream and stream_kind in STREAM_TYPES:
            return self._download_stream(url, item, stream_kind)
        try:
            # 1. 探测：状态码 / Content-Type / 总长度 / 校验和
            ok, info, err = self._probe_media(session, url)
            if not ok:
                return False, "", err
            content_type = info["content_type"]
            expected_total = info["total"]
            expected_md5 = info["md5"]
            url_type = self._classify_url(url)
            if url_type == "other" and item is not None and item.kind:
                # 接口式直链（无扩展名）：沿用抓取阶段已判定的类型
                url_type = "video" if item.kind in STREAM_TYPES else item.kind

            # 2. 错误页识别：期望媒体却返回 HTML/JSON（防盗链拦截、链接失效、需要登录）
            if url_type in ("video", "image", "audio") and (
                    content_type.startswith("text/") or
                    content_type in ("application/json", "application/xml")):
                return False, "", f"返回 {content_type} 页面而非媒体文件（可能被拦截或链接失效）"

            # 3. 识别资源类型并确定保存路径
            resource_type = self._classify_content_type(content_type) or url_type
            save_path = self._resolve_save_path(url, resource_type, item)
            if not os.path.splitext(save_path)[1]:
                save_path += self._guess_extension(content_type)

            # 4. 已完整下载过的文件：服务器提供MD5则校验，通过直接跳过
            existing = os.path.getsize(save_path) if os.path.exists(save_path) else 0
            if expected_total > 0 and existing >= expected_total:
                if expected_md5:
                    if self._verify_md5(save_path, expected_md5):
                        return True, save_path, "已存在(MD5校验通过)"
                    self.signal_log.emit(f"MD5校验失败，重新下载: {url}")
                    os.remove(save_path)
                    existing = 0
                else:
                    return True, save_path, "已存在，跳过"

            # 5. 断点续传：已有部分文件时发送Range请求追加
            headers = self._build_headers(url)
            if existing > 0:
                headers["Range"] = f"bytes={existing}-"
            try:
                resp = session.get(url, headers=headers, proxies=self.proxy,
                                   timeout=self.timeout, stream=True,
                                   allow_redirects=True, verify=self.verify_ssl)
            except requests.RequestException as e:
                return False, "", f"{type(e).__name__}: {str(e)[:80]}"

            try:
                if resp.status_code == 206:
                    # 校验续传起点：与本地已有字节不一致则整体重下，避免拼接出损坏文件
                    start = None
                    content_range = resp.headers.get("Content-Range", "")
                    if content_range.startswith("bytes "):
                        seg = content_range[6:].split("/")[0]
                        if "-" in seg:
                            try:
                                start = int(seg.split("-")[0])
                            except ValueError:
                                start = None
                    if start is not None and start != existing:
                        self.signal_log.emit(
                            f"续传起点不一致（服务器{start}/本地{existing}），改为重新下载")
                        mode, existing = "wb", 0
                    else:
                        mode = "ab"
                elif resp.status_code == 200:
                    mode, existing = "wb", 0     # 服务器忽略Range或文件为空，从头下载
                else:
                    return False, "", f"HTTP {resp.status_code}"

                # 下载响应又变成HTML错误页时同样拒绝保存
                resp_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if url_type in ("video", "image", "audio") and resp_type.startswith("text/"):
                    return False, "", f"下载响应为 {resp_type} 页面而非媒体文件"

                # 6. 1MB分块写入 + 下载速度限制
                start_time = time.time()
                downloaded = existing
                with open(save_path, mode) as f:
                    for chunk in resp.iter_content(chunk_size=self.CHUNK_SIZE):
                        if self._stop_event.is_set():
                            return False, "", "任务已停止"
                        if not chunk:
                            continue
                        f.write(chunk)
                        downloaded += len(chunk)
                        if self.rate_limit > 0:
                            elapsed = time.time() - start_time
                            expect = downloaded / self.rate_limit
                            if expect > elapsed:
                                time.sleep(expect - elapsed)
            except requests.RequestException as e:
                # 注意：requests.RequestException 继承自 OSError，必须先于 OSError 捕获
                return False, "", f"传输中断 {type(e).__name__}: {str(e)[:60]}"
            except OSError as e:
                return False, "", self._io_error_note(e)
            finally:
                resp.close()

            # 7. 完整性校验：实际大小必须与 Content-Length 一致，避免截断/错误页被当成功
            actual_size = os.path.getsize(save_path) if os.path.exists(save_path) else 0
            if actual_size <= 0:
                return False, "", "未下载到有效数据"
            if expected_total > 0 and actual_size != expected_total:
                return False, "", (f"下载不完整（{format_size(actual_size)}/"
                                   f"{format_size(expected_total)}），可续传")

            # 8. MD5校验：服务器提供校验和时不匹配则删除重下
            if expected_md5 and not self._verify_md5(save_path, expected_md5):
                os.remove(save_path)
                return False, "", "MD5校验失败"

            # 9. 质量校验：尺寸/体积不达标直接丢弃（与 core/pipeline 的提前过滤闭环）
            #    放在压缩之前：分辨率与原始体积才是质量判定的依据
            reason = self._quality_reason(save_path)
            if reason:
                try:
                    os.remove(save_path)
                except OSError:
                    pass
                return False, "", f"{FILTERED_PREFIX}{reason}"

            # 10. 存储优化：压缩 → 去重
            if self.compress_quality > 0:
                save_path, note1 = self._compress_image(save_path, self.compress_quality)
            else:
                note1 = ""
            if self.enable_dedup:
                save_path, note2 = self._dedup_image(save_path, resource_type)
            else:
                note2 = ""

            # 11. 记录清单与数据库
            size = os.path.getsize(save_path) if os.path.exists(save_path) else 0
            md5 = self._file_md5(save_path) if os.path.exists(save_path) else ""
            self._record_manifest(url, save_path, resource_type, size, md5, item)
            self._save_media_state(url, save_path, "completed")
            note = " | ".join(n for n in (note1, note2) if n)
            return True, save_path, note
        except Exception as e:
            return False, "", f"{type(e).__name__}: {str(e)[:80]}"

    # ---------- 流媒体（HLS / DASH）----------
    def _download_stream(self, url, item, kind):
        """下载并合并流媒体：解析播放列表 → 并发下载分片 → 合并 → （可选）转封装。

        与普通资源的关键差别：产出是**合并后的单个视频文件**，
        而不是播放列表文本；网络细节通过闭包 ``fetch_bytes`` 注入 merger，
        使 merger 保持与 requests 解耦。

        :return: (是否成功, 文件路径, 说明)
        """
        from core.media import dash as dash_module
        from core.media import hls as hls_module
        from core.media import merger

        session = self._get_session()
        base_headers = self._build_headers(url)

        def fetch_bytes(target, byte_range=None, max_bytes=0):
            """按需拉取字节（支持 Range），供分片下载与密钥获取复用。"""
            request_headers = dict(base_headers)
            if byte_range:
                offset, length = byte_range
                request_headers["Range"] = \
                    f"bytes={offset}-{offset + max(1, length) - 1}"
            request_headers["Accept-Encoding"] = "identity"
            try:
                resp = session.get(target, headers=request_headers,
                                   proxies=self.proxy or None, timeout=self.timeout,
                                   stream=True, allow_redirects=True,
                                   verify=self.verify_ssl)
            except requests.RequestException as exc:
                raise merger.StreamError(
                    f"{type(exc).__name__}: {str(exc)[:80]}") from exc
            try:
                if resp.status_code >= 400:
                    raise merger.StreamError(f"HTTP {resp.status_code}")
                data = bytearray()
                for chunk in resp.iter_content(chunk_size=self.CHUNK_SIZE):
                    if self._stop_event.is_set():
                        raise merger.StreamError("任务已停止")
                    if not chunk:
                        continue
                    data.extend(chunk)
                    if max_bytes and len(data) >= max_bytes:
                        break
                return bytes(data)
            except requests.RequestException as exc:
                raise merger.StreamError(
                    f"传输中断 {type(exc).__name__}") from exc
            finally:
                try:
                    resp.close()
                except Exception:
                    pass

        def fetch_text(target):
            payload = fetch_bytes(target)
            return decode_bytes(payload, guess_encoding(payload))

        try:
            self.signal_log.emit(f"🎞 流媒体解析：{url[:110]}")
            if kind == TYPE_DASH:
                representations = dash_module.parse_mpd(fetch_text(url), url)
                plan = dash_module.build_plan(representations)
                self.signal_log.emit(
                    f"　MPD 清单：{len(representations)} 路码流，选择 {plan.label}"
                    + ("（含独立音轨，需 ffmpeg 合流）" if plan.audio_plan else ""))
            else:
                plan = hls_module.build_plan(fetch_text(url), url,
                                             fetch_text=fetch_text,
                                             progress=self.signal_log.emit)

            remux_mode = (self.media_config.stream_remux or "auto").lower()
            if remux_mode not in ("auto", "ffmpeg", "never"):
                remux_mode = "auto"
            suffix = merger.target_suffix(plan, remux_mode)
            if remux_mode != "never" and not merger.has_ffmpeg():
                self.signal_log.emit(
                    "　未找到 ffmpeg：将保留原始分片容器（安装 ffmpeg 后可自动转 MP4）")
            save_path = self._resolve_save_path(url, "video", item,
                                               ext_override=suffix)
            ok, final_path, note = merger.download_and_merge(
                plan, save_path, fetch_bytes=fetch_bytes,
                workers=max(4, self.max_workers), decrypt=True, remux=remux_mode,
                stop_event=self._stop_event, progress=self.signal_log.emit)
        except merger.StreamError as exc:
            return False, "", str(exc)[:160]
        except Exception as exc:
            return False, "", f"{type(exc).__name__}: {str(exc)[:120]}"

        if not ok:
            return False, "", note or "流媒体合并失败"
        size = os.path.getsize(final_path) if os.path.exists(final_path) else 0
        self._record_manifest(url, final_path, "video", size, "", item)
        self._save_media_state(url, final_path, "completed")
        return True, final_path, note or "流媒体合并完成"

    @staticmethod
    def _is_filtered(note):
        """该结果是否属于"质量过滤"而非下载失败（过滤不重试、不计失败数）"""
        return bool(note) and note.startswith(FILTERED_PREFIX)

    def _record_manifest(self, url, path, resource_type, size, md5, item=None):
        """登记资源清单行（含 MediaItem 的合集/分辨率/时长元信息）"""
        row = {"url": url, "path": path, "type": resource_type,
               "size": size, "md5": md5, "album": "", "resolution": "", "duration": 0.0}
        if item is not None:
            row["album"] = item.album
            row["resolution"] = item.resolution
            row["duration"] = item.duration
        with self._manifest_lock:
            self._manifest_rows.append(row)

    # ---------- 失败重试 ----------
    def _download_with_retry(self, task):
        """按任务重试次数执行下载，失败指数退避后重试"""
        for attempt in range(1, task.max_retries + 1):
            if self._stop_event.is_set():
                return False, "", "任务已停止"
            ok, path, note = self._download_one(task.url)
            if ok or self._is_filtered(note):
                return ok, path, note   # 质量过滤属于"已处理"，重试无意义
            if self._is_stream_task(task):
                # 流媒体（HLS/DASH）失败往往涉及整段下载，整体重试代价过高，
                # 交由用户在失败列表中手动重试
                return ok, path, note
            if attempt < task.max_retries:
                wait = 2 ** attempt
                self.signal_log.emit(f"下载失败({note})，{wait}s后第{attempt + 1}次重试: {task.url}")
                time.sleep(wait)
        return False, "", f"重试{task.max_retries}次仍失败"

    def _is_stream_task(self, task):
        """该下载任务是否为流媒体（决定是否跳过整体重试）。"""
        if not self.media_config.merge_stream:
            return False
        item = getattr(task, "item", None)
        if item is not None and getattr(item, "kind", ""):
            return item.kind in STREAM_TYPES
        return classify_url(task.url) in STREAM_TYPES

    # ---------- 工作线程 ----------
    def _process_task(self, task):
        ok, path, note = self._download_with_retry(task)
        if ok:
            with self._done_lock:
                self._success += 1
        elif self._is_filtered(note):
            with self._done_lock:
                self._filtered += 1
                self.filtered_urls.append(task.url)
        else:
            with self._done_lock:
                self._failed += 1
                self.failed_urls.append(task.url)
        self.signal_item_done.emit(task.url, path, ok, note)
        with self._done_lock:
            done = self._success + self._failed + self._filtered
            done_total, success, failed = done, self._success, self._failed
        self.signal_progress.emit(done_total, self._total, success, failed)

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
        self._close_session()  # 线程退出前关闭连接池

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
            summary = (f"成功：{self._success}　失败：{self._failed}　"
                       f"质量过滤：{self._filtered}")
            html_lines = [
                "<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'>",
                "<title>下载资源清单</title>",
                "<style>body{font-family:sans-serif;margin:20px}table{border-collapse:collapse;width:100%}",
                "th,td{border:1px solid #ccc;padding:6px;text-align:left;font-size:13px}",
                "th{background:#f0f0f0}code{font-size:12px}</style></head><body>",
                f"<h1>下载资源清单</h1><p>生成时间：{now}　{summary}</p>",
                "<table><tr><th>#</th><th>类型</th><th>合集</th><th>分辨率</th>"
                "<th>资源URL</th><th>保存路径</th><th>大小</th><th>MD5</th></tr>",
            ]
            for idx, row in enumerate(rows, 1):
                html_lines.append(
                    f"<tr><td>{idx}</td><td>{row['type']}</td>"
                    f"<td>{row.get('album', '')}</td>"
                    f"<td>{row.get('resolution', '')}</td>"
                    f"<td><a href='{row['url']}'>{row['url'][:80]}</a></td>"
                    f"<td><code>{row['path']}</code></td>"
                    f"<td>{format_size(row['size'])}</td>"
                    f"<td><code>{row['md5'][:12]}</code></td></tr>")
            html_lines.append("</table></body></html>")
            with open(os.path.join(self.download_dir, "index.html"), "w", encoding="utf-8") as f:
                f.write("\n".join(html_lines))

            txt_lines = [f"下载资源清单  生成时间：{now}　{summary}", "=" * 60, ""]
            for row in rows:
                extra = " ".join(part for part in (row.get("album", ""),
                                                   row.get("resolution", "")) if part)
                txt_lines.append(f"[{row['type']}]{f' {extra}' if extra else ''} {row['url']}\n"
                                 f"     → {row['path']} ({format_size(row['size'])}, MD5:{row['md5']})")
            if self.filtered_urls:
                txt_lines.extend(["", "-" * 60,
                                  f"# 因质量不达标被丢弃（{len(self.filtered_urls)} 个）："])
                txt_lines.extend(f"  {url}" for url in self.filtered_urls)
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
            self._queue.put(DownloadTask(self._priority_of(url), seq, url,
                                         self.max_retries, self._items.get(url)))
            seq += 1

        self.signal_log.emit(
            f"⏳ 下载队列已就绪：{total} 个任务，{self.max_workers} 个工作线程并发执行")
        if self._items:
            self.signal_log.emit(
                f"　已携带 {len(self._items)} 条媒体元信息（合集/序号/请求头），"
                f"将按模板命名并归档")
        workers = spawn_workers(self.max_workers, self._worker, name_prefix="downloader")
        for w in workers:
            w.join()
        self.signal_log.emit(
            f"🔚 工作线程已全部结束（成功 {self._success} / 失败 {self._failed} / "
            f"质量过滤 {self._filtered}）")
        if self._filtered:
            self.signal_log.emit(
                "　质量过滤：下载后尺寸或体积未达标的资源已丢弃（可调整最小宽高/体积后重试）")

        manifest = self._generate_manifest()
        self._emit_disk_status("任务结束")
        self.signal_finish.emit(self._success, self._failed, list(self.failed_urls), manifest)


__all__ = ["ContentDownloader", "DownloadTask", "DEFAULT_FILENAME_TEMPLATE",
           "FILTERED_PREFIX"]
