"""配置管理：集中存放应用级常量、默认路径与默认参数。

其他模块不应散落魔法数字/路径，统一从这里读取；
本模块只依赖标准库，可被任意层引用。
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# 应用信息
# ---------------------------------------------------------------------------
APP_NAME = "带递归爬取功能的图形化爬虫工具"
APP_VERSION = "2.1.0"

# 用于 robots.txt 匹配的爬虫标识
CRAWLER_USER_AGENT = "crawler-tool"

# ---------------------------------------------------------------------------
# 默认文件 / 目录
# ---------------------------------------------------------------------------
DEFAULT_DB_FILENAME = "crawler_cache.db"   # 主缓存数据库文件名
DEFAULT_DOWNLOAD_DIR = "downloads"          # 默认下载目录
DEFAULT_TASK_DATA_DIR = "task_data"         # 多任务管理器的数据根目录

# 包根目录 = 本文件所在目录；资源目录存放默认数据库文件
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
RESOURCES_DIR = os.path.join(ROOT_DIR, "resources")


def ensure_resources_dir() -> str:
    """确保 resources 目录存在，返回其路径。"""
    os.makedirs(RESOURCES_DIR, exist_ok=True)
    return RESOURCES_DIR


def default_db_path() -> str:
    """默认主数据库路径：<包根>/resources/crawler_cache.db。"""
    return os.path.join(ensure_resources_dir(), DEFAULT_DB_FILENAME)


# ---------------------------------------------------------------------------
# 默认爬取 / 下载参数（UI 控件初始值来源）
# ---------------------------------------------------------------------------
DEFAULT_MAX_DEPTH = 1        # 递归深度
DEFAULT_REQUEST_DELAY = 0.5  # 请求间隔（秒）
DEFAULT_JITTER = 0.3         # 间隔随机抖动幅度
DEFAULT_MAX_PAGES = 0        # 自动停止页数上限（0=不限）
DEFAULT_MAX_CHARS = 0        # 单页最大字符数（0=不限）
DEFAULT_LINK_FILTER = ""     # 定向链接过滤关键词
DEFAULT_MAX_RETRIES = 2      # 爬取失败自动重试（任务级）
DEFAULT_CRAWL_EXTERNAL = False

DEFAULT_DOWNLOAD_RETRIES = 3      # 下载失败重试次数
DEFAULT_DOWNLOAD_WORKERS = 4      # 并发下载线程数
DEFAULT_SPEED_LIMIT = 0           # 下载限速 KB/s（0=不限）
DEFAULT_COMPRESS_QUALITY = 0      # 图片压缩质量（0=不压缩）
DEFAULT_DEDUP = True              # 图片内容去重
DEFAULT_MD5_CHECK = True          # MD5 校验

__all__ = [
    "APP_NAME", "APP_VERSION", "CRAWLER_USER_AGENT",
    "DEFAULT_DB_FILENAME", "DEFAULT_DOWNLOAD_DIR", "DEFAULT_TASK_DATA_DIR",
    "ROOT_DIR", "RESOURCES_DIR", "ensure_resources_dir", "default_db_path",
    "DEFAULT_MAX_DEPTH", "DEFAULT_REQUEST_DELAY", "DEFAULT_JITTER",
    "DEFAULT_MAX_PAGES", "DEFAULT_MAX_CHARS", "DEFAULT_LINK_FILTER",
    "DEFAULT_MAX_RETRIES", "DEFAULT_CRAWL_EXTERNAL",
    "DEFAULT_DOWNLOAD_RETRIES", "DEFAULT_DOWNLOAD_WORKERS",
    "DEFAULT_SPEED_LIMIT", "DEFAULT_COMPRESS_QUALITY",
    "DEFAULT_DEDUP", "DEFAULT_MD5_CHECK",
]
