"""通用工具函数：时间格式化、时长格式化、名称清洗、大小格式化、域名提取等。

本模块不导入任何 Qt / 第三方依赖，只依赖标准库。
"""

from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urlparse


def now_str() -> str:
    """当前时间的本地化字符串："%Y-%m-%d %H:%M:%S"。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def fmt_duration(seconds) -> str:
    """将秒数格式化为 "HH:MM:SS"。"""
    seconds = max(0, int(seconds or 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def url_domain(url: str) -> str:
    """提取 URL 的域名（netloc），解析失败返回空串。"""
    try:
        return urlparse(url).netloc
    except Exception:
        return ""


def sanitize_name(name: str) -> str:
    """清洗名称以安全用作目录/文件名：非法字符替换为下划线，截断至80字符。"""
    return re.sub(r"[^\w\-.]+", "_", str(name)).strip("_")[:80] or "task"


def format_size(num) -> str:
    """将字节数格式化为人类可读大小（B/KB/MB/GB/TB）。"""
    num = float(num or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"


__all__ = ["now_str", "fmt_duration", "url_domain", "sanitize_name", "format_size"]
