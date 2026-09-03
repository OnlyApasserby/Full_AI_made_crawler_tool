"""验证器：URL 校验等输入合法性判断。

- ``valid_http_url``：仅判断是否可作为 http(s) 链接（供调度器快速过滤）
- ``validate_url``：带中文错误提示的完整校验（供 GUI 调用）
"""

from __future__ import annotations

from urllib.parse import urlparse


def valid_http_url(url: str) -> bool:
    """判断 URL 是否以 http:// 或 https:// 开头且含有效域名。"""
    try:
        return url.startswith(("http://", "https://")) and bool(urlparse(url).netloc)
    except Exception:
        return False


def validate_url(url) -> tuple[bool, str]:
    """校验 URL 是否合法，返回 ``(是否合法, 错误信息)``。"""
    if not url or not str(url).strip():
        return False, "请输入起始网址"
    url = str(url).strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False, "网址必须以http://或https://开头"
    if not parsed.netloc:
        return False, "网址无效，请输入包含有效域名的完整网址，例如：https://www.example.com"
    return True, ""


__all__ = ["valid_http_url", "validate_url"]
