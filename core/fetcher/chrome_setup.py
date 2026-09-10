"""本地 Chromium 内核的发现与解压（离线优先）。

背景
----
``playwright install chromium`` 需要联网下载约 150MB；离线环境下更实用的做法是
把官方 Chrome for Testing 压缩包（如 ``chrome-win64.zip``）放到项目根目录，
由本模块自动解压到 ``resources/browsers/`` 并通过 ``executable_path`` 交给
playwright 使用。

查找优先级（先命中先用）
------------------------
1. 环境变量 ``CRAWLER_CHROME_PATH`` 指定的可执行文件
2. ``resources/browsers/`` 下已解压的内核
3. 项目根目录已解压的内核目录（如 ``chrome-win64/``）
4. 系统已安装的 Chrome / Edge / Chromium
5. PATH 中的 chrome / chromium
6. 以上都没有时，解压项目根目录的离线压缩包（``chrome-win64.zip`` 等）

本模块只依赖标准库。
"""

from __future__ import annotations

import os
import shutil
import threading
import zipfile

from config import (
    BROWSER_EXECUTABLE_ENV, BROWSERS_DIR, BUNDLED_BROWSERS_DIR,
    BUNDLE_DIR, CHROME_ZIP_CANDIDATES, ROOT_DIR, ensure_browsers_dir,
)

#: 可执行文件名（按优先级）
_EXE_NAMES = ("chrome.exe", "chromium.exe", "headless_shell.exe",
              "chrome", "chromium", "chromium-browser")
#: 搜索内核目录时的最大下探层级（chrome-win64/chrome.exe 为 1 层）
_MAX_DEPTH = 3
#: 系统已安装浏览器的常见路径
_SYSTEM_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)

_extract_lock = threading.Lock()
#: 进程级缓存：可执行文件路径（避免每次抓取都遍历目录）
_cached_executable: str | None = None


def _is_executable(path: str) -> bool:
    return bool(path) and os.path.isfile(path)


def _search_dir(base_dir: str, max_depth: int = _MAX_DEPTH) -> str:
    """在目录内广度优先查找浏览器可执行文件（限制层级，避免全盘扫描）。"""
    if not os.path.isdir(base_dir):
        return ""
    base_depth = base_dir.rstrip(os.sep).count(os.sep)
    for current, dirs, files in os.walk(base_dir):
        if current.count(os.sep) - base_depth >= max_depth:
            dirs[:] = []
        for name in _EXE_NAMES:
            if name in files:
                return os.path.join(current, name)
    return ""


def find_local_zip() -> str:
    """查找应用目录（或打包目录）下的离线内核压缩包，未找到返回空串。"""
    for base in (ROOT_DIR, BUNDLE_DIR):
        if not base:
            continue
        for name in CHROME_ZIP_CANDIDATES:
            path = os.path.join(base, name)
            if os.path.isfile(path):
                return path
    return ""


def find_executable(use_cache: bool = True) -> str:
    """按优先级查找可用的浏览器可执行文件，未找到返回空串。"""
    global _cached_executable
    if use_cache and _is_executable(_cached_executable or ""):
        return _cached_executable

    # 1. 环境变量显式指定
    explicit = os.environ.get(BROWSER_EXECUTABLE_ENV, "").strip()
    if _is_executable(explicit):
        _cached_executable = explicit
        return explicit

    # 2/3. 本地内核目录：可写目录 -> 打包内置目录 -> 应用根目录
    for base in (BROWSERS_DIR, BUNDLED_BROWSERS_DIR, ROOT_DIR):
        if not base:
            continue
        found = _search_dir(base)
        if found:
            _cached_executable = found
            return found

    # 4. 系统已安装浏览器
    for candidate in _SYSTEM_CANDIDATES:
        if _is_executable(candidate):
            _cached_executable = candidate
            return candidate

    # 5. PATH
    for name in ("chrome", "chromium", "google-chrome", "msedge"):
        found = shutil.which(name)
        if found:
            _cached_executable = found
            return found
    return ""


def extract_zip(zip_path: str, dest_dir: str = "", progress=None) -> str:
    """解压离线内核包并返回可执行文件路径。

    :param progress: 可选回调 ``progress(消息文本)``，用于把进度写入爬取日志
    :raises OSError: 文件读写失败
    :raises zipfile.BadZipFile: 压缩包损坏
    :raises ValueError: 压缩包包含越权路径（zip slip）
    """
    dest = os.path.realpath(dest_dir or ensure_browsers_dir())
    with zipfile.ZipFile(zip_path) as archive:
        root = dest + os.sep
        for member in archive.infolist():
            target = os.path.realpath(os.path.join(dest, member.filename))
            if target != dest and not target.startswith(root):
                raise ValueError(f"压缩包内含越权路径，已拒绝解压：{member.filename}")
        if progress:
            progress(f"正在解压浏览器内核：{zip_path} → {dest}")
        archive.extractall(dest)
    reset_cache()
    found = _search_dir(dest) or find_executable()
    if progress:
        progress(f"浏览器内核已就绪：{found or '未在解压目录中找到可执行文件'}")
    return found


def ensure_executable(auto_extract: bool = True, progress=None) -> tuple:
    """确保存在可用的浏览器可执行文件（必要时解压离线包）。

    :return: (可执行文件路径, 失败原因)；成功时原因为空串
    """
    found = find_executable()
    if found:
        return found, ""
    if not auto_extract:
        return "", "未找到本地浏览器内核"
    zip_path = find_local_zip()
    if not zip_path:
        return "", "未找到浏览器内核，也未找到可解压的离线包"
    with _extract_lock:
        # 双重检查：可能已被其它线程解压完成
        found = find_executable()
        if found:
            return found, ""
        try:
            found = extract_zip(zip_path, progress=progress)
        except Exception as exc:
            return "", f"解压离线内核包失败：{type(exc).__name__}: {str(exc)[:120]}"
    if found:
        return found, ""
    return "", "解压完成但未找到浏览器可执行文件（请确认压缩包内包含 chrome.exe）"


def describe() -> str:
    """返回当前内核来源的可读描述（用于日志与界面提示）。"""
    found = find_executable()
    if found:
        return found
    zip_path = find_local_zip()
    if zip_path:
        return f"未解压（可自动解压：{zip_path}）"
    return "未找到（可放置 chrome-win64.zip 到项目根目录，或执行 playwright install chromium）"


def reset_cache() -> None:
    """清空可执行文件路径缓存（解压完成或手动更换内核后调用）。"""
    global _cached_executable
    _cached_executable = None


__all__ = ["find_executable", "find_local_zip", "extract_zip",
           "ensure_executable", "describe", "reset_cache"]
