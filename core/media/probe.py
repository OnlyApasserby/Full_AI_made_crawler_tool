"""图片尺寸与内容指纹探测（质量过滤与去重的判定依据）。

两种使用方式
------------
- :func:`probe_local` / :func:`probe_bytes`：**不依赖网络**，用于下载完成后
  校验"这张图是否太小/是否是占位图"
- :func:`probe_from_fetcher`：借助抓取器只取前 64KB 头部即判定尺寸，
  在下载前就丢弃明显不合格的图（可选优化，抓取器需支持 ``fetch_prefix``）

Pillow 缺失时退回内置的头部解析器（PNG / JPEG / GIF / BMP / WebP），
保证"尺寸过滤"这一核心能力在主流程中始终可用。
"""

from __future__ import annotations

import io
import os

try:  # 可选依赖：缺失时使用内置头部解析
    from PIL import Image
    HAS_PIL = True
except ImportError:
    Image = None
    HAS_PIL = False

#: 头部探测默认读取的字节数（绝大多数图片格式的尺寸信息都在前 64KB 内）
DEFAULT_PREFIX_BYTES = 64 * 1024
#: 尺寸解析所需的最小数据量
_MIN_HEADER_BYTES = 32


# ---------------------------------------------------------------------------
# 内置头部解析（Pillow 缺失时的兜底）
# ---------------------------------------------------------------------------
def _png_size(data: bytes) -> tuple:
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        return width, height
    return 0, 0


def _gif_size(data: bytes) -> tuple:
    if len(data) >= 10 and data[:6] in (b"GIF87a", b"GIF89a"):
        width = int.from_bytes(data[6:8], "little")
        height = int.from_bytes(data[8:10], "little")
        return width, height
    return 0, 0


def _bmp_size(data: bytes) -> tuple:
    if len(data) >= 26 and data[:2] == b"BM":
        width = int.from_bytes(data[18:22], "little", signed=True)
        height = int.from_bytes(data[22:26], "little", signed=True)
        return abs(width), abs(height)
    return 0, 0


def _jpeg_size(data: bytes) -> tuple:
    """扫描 JPEG 段直到出现 SOFn 帧头，从中读出宽高。"""
    index, length = 2, len(data)
    while index + 9 <= length:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        if marker == 0xD9:
            return 0, 0
        segment_length = int.from_bytes(data[index + 2:index + 4], "big")
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            height = int.from_bytes(data[index + 5:index + 7], "big")
            width = int.from_bytes(data[index + 7:index + 9], "big")
            return width, height
        if segment_length < 2:
            return 0, 0
        index += 2 + segment_length
    return 0, 0


def _webp_size(data: bytes) -> tuple:
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return 0, 0
    chunk = data[12:16]
    if chunk == b"VP8X":  # 扩展格式：画布尺寸为 24 位小端（存储值 = 实际-1）
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height
    if chunk == b"VP8 ":  # 有损格式：跳过帧标签与 0x9d012a 同步码
        if data[23:26] != b"\x9d\x01\x2a":
            return 0, 0
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return width, height
    if chunk == b"VP8L":  # 无损格式：14 位打包
        if data[20] != 0x2F:
            return 0, 0
        bits = int.from_bytes(data[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return width, height
    return 0, 0


_PARSERS = (_png_size, _jpeg_size, _gif_size, _bmp_size, _webp_size)


def probe_bytes(data: bytes) -> tuple:
    """从图片字节（可只含头部）解析尺寸。

    :return: (宽, 高)；无法识别时返回 (0, 0)
    """
    if not data or len(data) < _MIN_HEADER_BYTES:
        return 0, 0
    if HAS_PIL:
        try:
            with Image.open(io.BytesIO(data)) as image:
                size = image.size
                if size and size[0] and size[1]:
                    return int(size[0]), int(size[1])
        except Exception:
            pass
    for parser in _PARSERS:
        try:
            width, height = parser(data)
        except Exception:
            continue
        if width and height:
            return width, height
    return 0, 0


def probe_local(path: str) -> tuple:
    """解析本地图片文件尺寸。

    :return: (宽, 高)；文件不存在或无法解析时返回 (0, 0)
    """
    if not path or not os.path.isfile(path):
        return 0, 0
    try:
        with open(path, "rb") as handle:
            head = handle.read(DEFAULT_PREFIX_BYTES)
    except OSError:
        return 0, 0
    return probe_bytes(head)


def probe_from_fetcher(fetcher, url: str, headers: dict | None = None,
                       size: int = DEFAULT_PREFIX_BYTES) -> tuple:
    """借助抓取器只读取图片头部来判定尺寸（下载前过滤，可选优化）。

    抓取器需提供 ``fetch_prefix(url, size, headers)``；不支持时返回 (0, 0)，
    由调用方退回到"下载后再判定"。
    """
    fetch_prefix = getattr(fetcher, "fetch_prefix", None)
    if fetch_prefix is None:
        return 0, 0
    try:
        data, _error = fetch_prefix(url, size=size, headers=headers)
    except Exception:
        return 0, 0
    return probe_bytes(data)


# ---------------------------------------------------------------------------
# 感知哈希（内容近似去重）
# ---------------------------------------------------------------------------
def dhash(source, hash_size: int = 8) -> int:
    """计算差异哈希：先缩放到 (hash_size+1) x hash_size 灰度图，
    再比较相邻像素得到位串。返回整数，失败时返回 0。

    :param source: 文件路径 或 图片字节
    """
    if not HAS_PIL:
        return 0
    try:
        if isinstance(source, (bytes, bytearray)):
            image = Image.open(io.BytesIO(bytes(source)))
        else:
            image = Image.open(source)
        with image:
            gray = image.convert("L").resize((hash_size + 1, hash_size),
                                             Image.Resampling.LANCZOS)
            pixels = list(gray.getdata())
        bits = 0
        for row in range(hash_size):
            base = row * (hash_size + 1)
            for col in range(hash_size):
                bits = (bits << 1) | int(pixels[base + col] < pixels[base + col + 1])
        return bits
    except Exception:
        return 0


def hamming(left: int, right: int) -> int:
    """两个哈希值的汉明距离（不同比特位数量）。"""
    return bin((left or 0) ^ (right or 0)).count("1")


def is_duplicate_hash(left: int, right: int, threshold: int = 4) -> bool:
    """两个感知哈希是否近似重复（默认允许 4 位差异，约等于肉眼同图）。"""
    if not left or not right:
        return False
    return hamming(left, right) <= max(0, int(threshold))


__all__ = ["HAS_PIL", "DEFAULT_PREFIX_BYTES", "probe_bytes", "probe_local",
           "probe_from_fetcher", "dhash", "hamming", "is_duplicate_hash"]
