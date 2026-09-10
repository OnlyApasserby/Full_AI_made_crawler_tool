"""数据库管理：对 sqlite3 缓存库（主库 / 任务库）的集中访问。

表结构（在 ``ensure_schema`` 中统一创建/迁移）：
- crawled_urls：已爬 URL（URL哈希去重，含任务ID，供断点续爬）
- crawl_tasks：每次爬取任务记录（status=running 表示未完成）
- media_downloads：媒体下载状态记录
- media_items：媒体抓取发现的资源（含分辨率/时长/合集等元信息）

本模块只依赖标准库，供 core（爬虫/下载器）与 manager / ui 层共用，
因此没有对上层模块的引用，不会形成循环导入。
"""

from __future__ import annotations

import hashlib
import os
import sqlite3


def _connect(db_path: str) -> sqlite3.Connection:
    """打开数据库连接并设置忙碌等待超时（多线程并发读写场景更稳）。"""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


# ---------------------------------------------------------------------------
# 建表 / 迁移
# ---------------------------------------------------------------------------
def ensure_schema(db_path: str) -> None:
    """创建全部数据表并对旧库补充缺失列（等效原 RecursiveCrawlerThread._init_db）。

    :raises sqlite3.Error: 数据库不可用时抛出（由调用方决定是否容错）
    """
    conn = _connect(db_path)
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
    # 媒体抓取结果表：记录"发现了什么资源"，与"下载了什么"（media_downloads）分离，
    # 便于过滤统计、导出清单与后续按分辨率/时长二次筛选
    conn.execute("""
        CREATE TABLE IF NOT EXISTS media_items (
            url_hash    TEXT PRIMARY KEY,
            url         TEXT NOT NULL,
            kind        TEXT DEFAULT '',
            source_page TEXT DEFAULT '',
            album       TEXT DEFAULT '',
            title       TEXT DEFAULT '',
            seq         INTEGER DEFAULT 0,
            width       INTEGER DEFAULT 0,
            height      INTEGER DEFAULT 0,
            duration    REAL DEFAULT 0,
            size        INTEGER DEFAULT 0,
            origin      TEXT DEFAULT '',
            task_id     INTEGER DEFAULT 0,
            found_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_media_items_task "
                 "ON media_items (task_id)")
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


# ---------------------------------------------------------------------------
# 爬取任务 / 已爬 URL（断点续爬）
# ---------------------------------------------------------------------------
def url_fingerprint(url: str) -> str:
    """URL 指纹：SHA-256，避免直接存储明文且去重稳定。"""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def load_visited_hashes(db_path: str, task_id) -> set[str]:
    """加载指定任务的已爬指纹集合。

    task_id=None 或库文件不存在时返回空集合（全新任务全量爬取）。
    """
    visited: set[str] = set()
    if task_id is None or not os.path.exists(db_path):
        return visited
    try:
        conn = _connect(db_path)
        rows = conn.execute(
            "SELECT url_hash FROM crawled_urls WHERE task_id=?", (task_id,)).fetchall()
        visited = {row[0] for row in rows}
        conn.close()
    except Exception:
        pass
    return visited


def create_crawl_task(db_path: str, start_url: str, max_depth: int,
                      resume_task_id) -> int | None:
    """登记任务开始（断点续爬依据），返回任务ID。

    - resume_task_id 不为 None：复用原任务ID（续爬），保留其已爬指纹
    - resume_task_id 为 None：插入新任务记录

    内部容错：出错时回退为 resume_task_id（可能为 None）。
    """
    task_id = None
    try:
        conn = _connect(db_path)
        if resume_task_id is not None:
            # 续爬：复用原任务ID，保留其已爬指纹用于跳过
            conn.execute(
                "UPDATE crawl_tasks SET status='running', finished_at=NULL WHERE id=?",
                (resume_task_id,))
            task_id = resume_task_id
        else:
            # 新任务：创建独立任务记录
            cur = conn.execute(
                "INSERT INTO crawl_tasks (start_url, max_depth, status) VALUES (?, ?, 'running')",
                (start_url, max_depth))
            task_id = cur.lastrowid
        conn.commit()
        conn.close()
    except Exception:
        task_id = resume_task_id
    return task_id


def finish_crawl_task(db_path: str, task_id) -> None:
    """将任务标记为已完成（内部容错）。"""
    if task_id is None:
        return
    try:
        conn = _connect(db_path)
        conn.execute(
            "UPDATE crawl_tasks SET status='completed', finished_at=datetime('now','localtime') WHERE id=?",
            (task_id,))
        conn.commit()
        conn.close()
    except Exception:
        pass


def save_crawled_url(db_path: str, url: str, task_id) -> str:
    """记录一条已爬 URL（内部容错），返回其指纹。

    调用方拿到指纹后应立即加入会话内去重集合（与本函数原语义一致）。
    """
    url_hash = url_fingerprint(url)
    try:
        conn = _connect(db_path)
        conn.execute(
            "INSERT OR IGNORE INTO crawled_urls (url_hash, url, status, file_path, task_id) "
            "VALUES (?, ?, 'completed', '', ?)",
            (url_hash, url, task_id or 0))
        conn.commit()
        conn.close()
    except Exception:
        pass
    return url_hash


# ---------------------------------------------------------------------------
# 媒体下载记录
# ---------------------------------------------------------------------------
def save_media_state(db_path: str, url: str, file_path: str, status: str) -> None:
    """记录媒体下载状态（内部容错）。"""
    url_hash = url_fingerprint(url)
    try:
        conn = _connect(db_path)
        conn.execute(
            "INSERT OR REPLACE INTO media_downloads (url_hash, url, file_path, status, downloaded_at) "
            "VALUES (?, ?, ?, ?, datetime('now', 'localtime'))",
            (url_hash, url, file_path, status))
        conn.commit()
        conn.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 媒体抓取结果（media_items）
# ---------------------------------------------------------------------------
def save_media_items(db_path: str, items, task_id=0) -> int:
    """批量写入媒体抓取结果（内部容错），返回成功写入条数。

    只接受带 ``url`` 属性的对象或字典；元信息（分辨率/时长/合集）一并入库，
    供清单导出与后续按质量二次筛选。
    """
    rows = []
    for item in items or []:
        data = item if isinstance(item, dict) else getattr(item, "to_dict", None)
        if callable(data):
            data = data()
        if not isinstance(data, dict):
            continue
        url = str(data.get("url") or "")
        if not url:
            continue
        rows.append((
            url_fingerprint(url), url, str(data.get("kind") or ""),
            str(data.get("source_page") or ""), str(data.get("album") or ""),
            str(data.get("title") or ""), int(data.get("index") or 0),
            int(data.get("width") or 0), int(data.get("height") or 0),
            float(data.get("duration") or 0.0), int(data.get("size") or 0),
            str(data.get("origin") or ""), int(task_id or 0)))
    if not rows:
        return 0
    try:
        conn = _connect(db_path)
        conn.executemany(
            "INSERT OR REPLACE INTO media_items (url_hash, url, kind, source_page, "
            "album, title, seq, width, height, duration, size, origin, task_id, found_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))",
            rows)
        conn.commit()
        conn.close()
        return len(rows)
    except Exception:
        return 0


def load_media_items(db_path: str, task_id=None, limit: int = 0) -> list[dict]:
    """读取媒体抓取结果（内部容错）。

    :param task_id: 指定任务ID；None 表示不限任务
    :param limit: 返回条数上限（0=不限）
    :return: 字典列表，字段与 ``media_items`` 表一致（含 ``index`` 别名）
    """
    if not os.path.exists(db_path):
        return []
    sql = ("SELECT url, kind, source_page, album, title, seq, width, height, "
           "duration, size, origin, task_id FROM media_items")
    params: list = []
    if task_id is not None:
        sql += " WHERE task_id=?"
        params.append(int(task_id))
    sql += " ORDER BY album, seq"
    if limit and limit > 0:
        sql += " LIMIT ?"
        params.append(int(limit))
    try:
        conn = _connect(db_path)
        rows = conn.execute(sql, params).fetchall()
        conn.close()
    except Exception:
        return []
    keys = ("url", "kind", "source_page", "album", "title", "seq", "width",
            "height", "duration", "size", "origin", "task_id")
    result = []
    for row in rows:
        record = dict(zip(keys, row))
        record["index"] = record.pop("seq")
        result.append(record)
    return result


def count_media_items(db_path: str, task_id=None) -> int:
    """统计媒体抓取结果条数（内部容错）。"""
    if not os.path.exists(db_path):
        return 0
    sql = "SELECT COUNT(*) FROM media_items"
    params: list = []
    if task_id is not None:
        sql += " WHERE task_id=?"
        params.append(int(task_id))
    try:
        conn = _connect(db_path)
        count = conn.execute(sql, params).fetchone()[0]
        conn.close()
        return int(count or 0)
    except Exception:
        return 0


def clear_media_items(db_path: str) -> int:
    """清空媒体抓取结果表，返回删除条数（内部容错）。"""
    try:
        conn = _connect(db_path)
        count = conn.execute("SELECT COUNT(*) FROM media_items").fetchone()[0]
        conn.execute("DELETE FROM media_items")
        conn.commit()
        conn.close()
        return int(count or 0)
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# 未完成任务查询（启动续爬检测，供主窗口使用）
# ---------------------------------------------------------------------------
def get_unfinished_tasks(db_path: str) -> list[dict]:
    """查询数据库中状态为 running 的爬取任务（内部容错）。"""
    if not os.path.exists(db_path):
        return []
    try:
        conn = _connect(db_path)
        rows = conn.execute(
            "SELECT id, start_url, max_depth FROM crawl_tasks WHERE status='running' ORDER BY id"
        ).fetchall()
        conn.close()
        return [{"id": r[0], "start_url": r[1], "max_depth": r[2]} for r in rows]
    except Exception:
        return []


def abandon_unfinished_tasks(db_path: str) -> None:
    """将全部未完成任务标记为完成（用户选择开始新任务时调用，内部容错）。"""
    try:
        conn = _connect(db_path)
        conn.execute(
            "UPDATE crawl_tasks SET status='completed', finished_at=datetime('now','localtime') "
            "WHERE status='running'")
        conn.commit()
        conn.close()
    except Exception:
        pass


def clear_crawled_records(db_path: str) -> int:
    """清空 crawled_urls 全部记录，返回删除条数。

    :raises sqlite3.Error: 数据库不可用时抛出（由调用方提示用户）
    """
    conn = _connect(db_path)
    cur = conn.execute("SELECT COUNT(*) FROM crawled_urls")
    count = cur.fetchone()[0]
    conn.execute("DELETE FROM crawled_urls")
    conn.commit()
    conn.close()
    return count


# ---------------------------------------------------------------------------
# 任务级数据库准备（多任务调度器使用：WAL 模式）
# ---------------------------------------------------------------------------
def configure_wal(db_path: str) -> None:
    """为任务级数据库开启 WAL 模式与忙等超时（内部容错）。"""
    try:
        conn = _connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.close()
    except Exception:
        pass


__all__ = [
    "ensure_schema", "url_fingerprint", "load_visited_hashes", "create_crawl_task",
    "finish_crawl_task", "save_crawled_url", "save_media_state",
    "save_media_items", "load_media_items", "count_media_items", "clear_media_items",
    "get_unfinished_tasks", "abandon_unfinished_tasks", "clear_crawled_records",
    "configure_wal",
]
