"""分片下载与合并（HLS / DASH 共用底座）。

职责边界
--------
- ``hls`` / ``dash`` 只负责**解析**播放列表，产出本模块定义的 :class:`MediaPlan`
- 本模块负责**下载分片 → 解密 → 顺序合并 → （可选）ffmpeg 转封装**
- 网络细节通过 ``fetch_bytes`` 回调注入，因此本模块不依赖 requests，
  可被下载器、脚本或测试任意复用

关键设计
--------
- **边下边写盘**：分片直接落盘为临时文件，不在内存聚合（视频动辄数 GB）
- **并发 + 有序合并**：并发下载，合并时严格按分片序号拼接
- **中断安全**：全程响应 ``stop_event``，中断或失败时清理临时目录
- **AES-128**：``pycryptodome`` 优先，退回 ``cryptography``；两者都缺失时给出明确提示
- **ffmpeg 可选**：只是转封装（``-c copy``，不重新编码），缺失时保留原始容器
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

#: 分片下载写入块大小
_CHUNK = 1024 * 256
#: 合并阶段的写入块大小（1MB）
_MERGE_CHUNK = 1024 * 1024
#: ffmpeg 转封装超时（秒）
_REMUX_TIMEOUT = 300


class StreamError(RuntimeError):
    """流媒体解析/下载/合并失败。"""


@dataclass
class Segment:
    """单个分片（HLS 的 EXTINF 片段 / DASH 的媒体分片）。"""

    url: str
    duration: float = 0.0
    byte_range: tuple = ()      # (offset, length)；空表示整文件
    sequence: int = 0           # HLS 绝对媒体序号（无显式 IV 时用于推导 IV）

    def range_header(self) -> str:
        """HTTP Range 头（无区间时返回空串）。"""
        if not self.byte_range:
            return ""
        offset, length = self.byte_range
        return f"bytes={offset}-{offset + max(1, length) - 1}"


@dataclass
class MediaPlan:
    """待下载的流媒体计划（HLS/DASH 解析结果的统一形态）。"""

    segments: list = field(default_factory=list)     # list[Segment]
    init_segment: Segment | None = None              # EXT-X-MAP / fMP4 初始化段
    key_uri: str = ""                                # EXT-X-KEY 的密钥地址
    key_iv: str = ""                                 # 显式 IV（十六进制）
    label: str = ""                                  # 日志用描述（分辨率/带宽）
    suffix: str = ".ts"                              # 合并后默认扩展名
    needs_remux: bool = False                        # 是否建议 ffmpeg 转封装
    audio_plan: "MediaPlan | None" = None            # DASH 音轨（有 ffmpeg 时合流）
    live: bool = False                               # 未出现 ENDLIST（直播流）

    @property
    def segment_count(self) -> int:
        return len(self.segments)

    @property
    def total_duration(self) -> float:
        return sum(getattr(seg, "duration", 0.0) or 0.0 for seg in self.segments)

    def is_empty(self) -> bool:
        return not self.segments


# ---------------------------------------------------------------------------
# ffmpeg 探测
# ---------------------------------------------------------------------------
def find_ffmpeg() -> str:
    """在 PATH 中查找 ffmpeg 可执行文件，未找到返回空串。"""
    return shutil.which("ffmpeg") or ""


def has_ffmpeg() -> bool:
    """本机是否可用 ffmpeg（决定能否把分片转封装为 mp4 / 合流音视频）。"""
    return bool(find_ffmpeg())


def target_suffix(plan: MediaPlan, remux: str = "auto") -> str:
    """根据配置与 ffmpeg 可用性决定最终文件扩展名。

    - ``remux="never"``：保留原始容器（HLS 通常为 ``.ts``）
    - 其余情况：有 ffmpeg 且需要转封装时输出 ``.mp4``
    """
    if remux == "never" or plan is None:
        return (plan.suffix if plan else ".ts")
    if has_ffmpeg() and (plan.needs_remux or plan.audio_plan is not None
                         or plan.suffix != ".mp4"):
        return ".mp4"
    return plan.suffix


def remux_to_mp4(src_path: str, out_path: str = "") -> tuple:
    """用 ffmpeg 把合并后的流文件转封装为 mp4（``-c copy``，不重新编码）。

    :return: (是否成功, 输出路径, 说明)
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return False, src_path, "未找到 ffmpeg，保留原始容器"
    if not out_path:
        out_path = os.path.splitext(src_path)[0] + ".mp4"
    command = [ffmpeg, "-y", "-loglevel", "error", "-i", src_path,
               "-c", "copy", "-bsf:a", "aac_adtstoasc", out_path]
    try:
        result = subprocess.run(command, capture_output=True, timeout=_REMUX_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, src_path, f"ffmpeg 调用失败：{type(exc).__name__}"
    if result.returncode != 0 or not os.path.isfile(out_path):
        detail = (result.stderr or b"").decode("utf-8", "replace").strip()[:120]
        return False, src_path, f"ffmpeg 转封装失败：{detail or result.returncode}"
    return True, out_path, "已转封装为 MP4"


def cleanup_temp(temp_dir: str) -> None:
    """清理合并过程产生的临时目录（失败/中断时调用）。"""
    if temp_dir and os.path.isdir(temp_dir):
        shutil.rmtree(temp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 解密
# ---------------------------------------------------------------------------
def parse_iv(value: str) -> bytes:
    """解析十六进制 IV（``0x`` 前缀可省略），非法或为空返回空串。"""
    text = (value or "").strip()
    if text.lower().startswith("0x"):
        text = text[2:]
    if not text or len(text) % 2:
        return b""
    try:
        return bytes.fromhex(text)
    except ValueError:
        return b""


def segment_iv(segment: Segment, explicit: bytes) -> bytes:
    """分片 IV：显式 IV 优先，否则用媒体序号推导（HLS 规范）。"""
    if explicit:
        return explicit
    return int(segment.sequence or 0).to_bytes(16, "big")


def _strip_pkcs7(data: bytes, ts_align: int = 0) -> bytes:
    """去掉 PKCS#7 填充。

    HLS 的 AES-128 在每个分片边界重新开始 CBC，因此**每个分片**都是独立填充的，
    必须逐片去除；只处理最后一片会在拼接处留下垃圾字节，破坏 TS 流。

    ``ts_align``（通常 188）用于防止误删：若原数据恰好是 TS 包长的整数倍、
    而"去掉填充"后不再是整数倍，说明末尾那几个字节本就是有效数据，予以保留。
    """
    if not data:
        return data
    padding = data[-1]
    if not (1 <= padding <= 16):
        return data
    if data[-padding:] != bytes([padding]) * padding:
        return data
    candidate = data[:-padding]
    if not candidate:
        return data
    if ts_align and len(data) % ts_align == 0 and len(candidate) % ts_align != 0:
        return data
    return candidate


def aes128_cbc_decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    """AES-128-CBC 解密（pycryptodome 优先，退回 cryptography）。

    :raises StreamError: 两个可选依赖都不可用
    """
    if not data:
        return data
    remainder = len(data) % 16
    if remainder:
        data = data + b"\x00" * (16 - remainder)
    try:
        from Crypto.Cipher import AES  # pycryptodome
        return AES.new(key, AES.MODE_CBC, iv).decrypt(data)
    except ImportError:
        pass
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        return decryptor.update(data) + decryptor.finalize()
    except ImportError as exc:
        raise StreamError(
            "该流媒体使用 AES-128 加密，需要安装 pycryptodome 或 cryptography "
            "才能解密（pip install pycryptodome）") from exc


# ---------------------------------------------------------------------------
# 下载 / 合并
# ---------------------------------------------------------------------------
def _log(progress, message: str) -> None:
    if progress:
        try:
            progress(message)
        except Exception:
            pass


def _fetch_with_retry(segment: Segment, fetch_bytes, retries: int) -> bytes:
    """下载单个分片（含重试）；全部失败时抛 :class:`StreamError`。"""
    last_error = "未知错误"
    for attempt in range(max(0, retries) + 1):
        try:
            data = fetch_bytes(segment.url, segment.byte_range)
            if data:
                return bytes(data)
            last_error = "分片内容为空"
        except Exception as exc:  # 网络/IO/停止信号统一处理
            last_error = f"{type(exc).__name__}: {str(exc)[:80]}"
        if attempt < retries:
            time.sleep(0.5 * (attempt + 1))
    raise StreamError(f"分片下载失败（{last_error}）：{segment.url[:120]}")


def _download_segments(plan: MediaPlan, temp_dir: str, fetch_bytes, key: bytes,
                       explicit_iv: bytes, workers: int, stop_event,
                       progress, retries: int) -> list:
    """并发下载全部分片并写入临时文件，返回按序排列的文件路径列表。"""
    total = len(plan.segments)
    locked = threading.Lock()
    finished = [0]
    # TS 容器用 188 字节包长做填充误删保护；fMP4 等容器不做对齐校验
    ts_align = 188 if plan.suffix == ".ts" else 0

    def work(index: int, segment: Segment) -> tuple:
        if stop_event is not None and stop_event.is_set():
            raise StreamError("任务已停止")
        data = _fetch_with_retry(segment, fetch_bytes, retries)
        if key:
            data = aes128_cbc_decrypt(data, key, segment_iv(segment, explicit_iv))
            data = _strip_pkcs7(data, ts_align)
        path = os.path.join(temp_dir, f"{index + 1:06d}.part")
        with open(path, "wb") as handle:
            handle.write(data)
        with locked:
            finished[0] += 1
            done = finished[0]
        if done == 1 or done == total or done % max(1, total // 20) == 0:
            _log(progress, f"　分片下载 {done}/{total}")
        return index, path

    results: dict = {}
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        futures = [pool.submit(work, index, segment)
                   for index, segment in enumerate(plan.segments)]
        try:
            for future in as_completed(futures):
                index, path = future.result()
                results[index] = path
        except Exception:
            for future in futures:
                future.cancel()
            raise
    return [results[index] for index in sorted(results)]


def _write_segment(path: str, segment: Segment, fetch_bytes, retries: int) -> None:
    """下载单个分片（初始化段）并落盘。"""
    data = _fetch_with_retry(segment, fetch_bytes, retries)
    with open(path, "wb") as handle:
        handle.write(data)


def _concat(paths: list, out_path: str, stop_event) -> int:
    """按顺序拼接文件（流式写入，返回总字节数）。"""
    total = 0
    with open(out_path, "wb") as target:
        for path in paths:
            if stop_event is not None and stop_event.is_set():
                raise StreamError("任务已停止")
            with open(path, "rb") as source:
                while True:
                    chunk = source.read(_MERGE_CHUNK)
                    if not chunk:
                        break
                    target.write(chunk)
                    total += len(chunk)
    return total


def mux_with_ffmpeg(video_path: str, audio_path: str, out_path: str) -> tuple:
    """用 ffmpeg 把分离的音视频轨合流（DASH 常见形态）。

    :return: (是否成功, 输出路径, 说明)
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return False, video_path, "未找到 ffmpeg，无法合流音视频（已保留视频轨）"
    command = [ffmpeg, "-y", "-loglevel", "error", "-i", video_path, "-i", audio_path,
               "-c", "copy", out_path]
    try:
        result = subprocess.run(command, capture_output=True, timeout=_REMUX_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, video_path, f"ffmpeg 合流失败：{type(exc).__name__}"
    if result.returncode != 0 or not os.path.isfile(out_path):
        detail = (result.stderr or b"").decode("utf-8", "replace").strip()[:120]
        return False, video_path, f"ffmpeg 合流失败：{detail or result.returncode}"
    return True, out_path, "已合流音视频轨"


def download_and_merge(plan: MediaPlan, out_path: str, *, fetch_bytes,
                       workers: int = 8, decrypt: bool = True, remux: str = "auto",
                       stop_event=None, progress=None, retries: int = 2) -> tuple:
    """下载并合并一个流媒体计划。

    :param out_path: 目标文件路径（扩展名由调用方用 :func:`target_suffix` 决定）
    :param fetch_bytes: ``(url, byte_range) -> bytes``，支持 Range 时传入区间
    :param remux: ``auto`` / ``ffmpeg`` / ``never``
    :param progress: 进度回调 ``progress(文本)``
    :return: (是否成功, 最终文件路径, 说明)
    :raises StreamError: 计划为空、密钥获取失败或分片下载失败

    流程：初始化段 + 分片并发下载 → 顺序合并 → （可选）ffmpeg 转封装/合流。
    中断或异常时会清理临时目录，不留下垃圾文件。
    """
    if plan is None or plan.is_empty():
        raise StreamError("播放列表中没有可下载的分片")

    temp_dir = out_path + ".parts"
    cleanup_temp(temp_dir)
    os.makedirs(temp_dir, exist_ok=True)
    try:
        # 1. 密钥（AES-128）
        key = b""
        if plan.key_uri and decrypt:
            _log(progress, "　获取解密密钥…")
            key = _fetch_with_retry(Segment(url=plan.key_uri), fetch_bytes, retries)[:16]
            if len(key) != 16:
                raise StreamError("密钥长度异常（期望 16 字节）")
        explicit_iv = parse_iv(plan.key_iv)

        # 2. 初始化段（fMP4 必需，需拼在所有分片之前）
        parts: list = []
        if plan.init_segment is not None:
            init_path = os.path.join(temp_dir, "000000.init")
            _write_segment(init_path, plan.init_segment, fetch_bytes, retries)
            parts.append(init_path)

        _log(progress, f"　开始下载分片：共 {plan.segment_count} 个"
                       + (f"（{plan.label}）" if plan.label else ""))
        parts.extend(_download_segments(plan, temp_dir, fetch_bytes, key,
                                       explicit_iv, workers, stop_event,
                                       progress, retries))

        # 3. 顺序合并
        need_remux = bool(remux != "never" and has_ffmpeg()
                          and (plan.needs_remux or plan.audio_plan is not None
                               or os.path.splitext(out_path)[1].lower() == ".mp4"))
        merge_target = os.path.join(temp_dir, "merged.tmp") if need_remux else out_path
        total_bytes = _concat(parts, merge_target, stop_event)
        _log(progress, f"　合并完成：{plan.segment_count} 个分片，"
                       f"{total_bytes / 1048576:.1f} MB")

        # 4. 转封装 / 音视频合流
        final_path, note = merge_target, ""
        if remux == "never":
            note = "未启用转封装"
        elif not has_ffmpeg():
            note = "未找到 ffmpeg，已保留原始容器（安装 ffmpeg 后可自动转 MP4）"
        else:
            audio_path = ""
            if plan.audio_plan is not None and not plan.audio_plan.is_empty():
                audio_out = os.path.join(temp_dir, "audio.tmp")
                audio_plan = plan.audio_plan
                audio_ok, audio_result, audio_note = download_and_merge(
                    audio_plan, audio_out, fetch_bytes=fetch_bytes, workers=workers,
                    decrypt=decrypt, remux="never", stop_event=stop_event,
                    progress=progress, retries=retries)
                if audio_ok:
                    audio_path = audio_result
                else:
                    _log(progress, f"　音轨下载失败，仅保留视频轨：{audio_note}")
            if audio_path:
                ok, final_path, note = mux_with_ffmpeg(merge_target, audio_path, out_path)
                if not ok:
                    ok, final_path, note = remux_to_mp4(merge_target, out_path)
            else:
                ok, final_path, note = remux_to_mp4(merge_target, out_path)
            if not ok:  # 转封装失败时退回原始文件，不丢数据
                final_path = out_path if os.path.exists(out_path) else merge_target
                _log(progress, f"　{note}")
        if plan.live:
            note = (note + " | " if note else "") + "直播流：仅合并了当前可得分片"
        return True, final_path, note
    except Exception:
        cleanup_temp(temp_dir)
        raise
    finally:
        cleanup_temp(temp_dir)


__all__ = [
    "StreamError", "Segment", "MediaPlan", "find_ffmpeg", "has_ffmpeg",
    "target_suffix", "remux_to_mp4", "mux_with_ffmpeg", "cleanup_temp",
    "parse_iv", "segment_iv", "aes128_cbc_decrypt", "download_and_merge",
]
