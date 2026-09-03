"""数据模型定义：与 UI / 数据库无关的纯数据与枚举类型。

- 单任务下载队列项  ``DownloadTask``
- 多任务调度状态 / 优先级常量与展示映射
- 多任务配置 ``TaskConfig``（对应 RecursiveCrawlerThread 构造参数 + 任务级控制项）

本模块只依赖标准库，可被 core / manager / ui 任意引用。
"""

from __future__ import annotations

from dataclasses import dataclass, field


class TaskStatus:
    """多任务状态枚举（字符串常量）。"""

    PENDING = "pending"      # 待执行
    RUNNING = "running"      # 执行中
    COMPLETED = "completed"  # 已完成
    PAUSED = "paused"        # 暂停
    FAILED = "failed"        # 失败


# 状态 -> 中文显示文本
STATUS_TEXT = {
    TaskStatus.PENDING: "待执行", TaskStatus.RUNNING: "执行中",
    TaskStatus.COMPLETED: "已完成", TaskStatus.PAUSED: "暂停",
    TaskStatus.FAILED: "失败",
}

# 状态 -> 展示颜色（UI 表格/对话框使用）
STATUS_COLORS = {
    TaskStatus.PENDING: "#808080", TaskStatus.RUNNING: "#1e7e34",
    TaskStatus.COMPLETED: "#1565c0", TaskStatus.PAUSED: "#e65100",
    TaskStatus.FAILED: "#c62828",
}


class TaskPriority:
    """任务优先级：数值越小优先级越高。"""

    HIGH, MEDIUM, LOW = 0, 1, 2


# 优先级 -> 中文显示文本
PRIORITY_TEXT = {TaskPriority.HIGH: "高", TaskPriority.MEDIUM: "中", TaskPriority.LOW: "低"}


@dataclass
class TaskConfig:
    """单个爬取任务的完整配置。

    字段与 ``core.crawler.RecursiveCrawlerThread`` 的构造参数一一对应，
    另含任务级控制项（优先级 / 依赖 / 任务级过滤规则 / 代理等）。
    """

    task_name: str = ""
    start_url: str = ""
    max_depth: int = 1
    crawl_external: bool = False
    request_delay: float = 0.5
    max_chars_per_page: int = 0     # 0 = 不限制
    link_filter: str = ""           # 定向爬取关键词
    jitter: float = 0.3
    max_pages: int = 0              # 0 = 不限制
    max_retries: int = 2
    robots_check: bool = True
    priority: int = TaskPriority.MEDIUM
    depends_on: list = field(default_factory=list)     # 依赖任务ID（需其完成才启动）
    proxy: dict = field(default_factory=dict)
    block_patterns: list = field(default_factory=list)  # 任务级屏蔽正则
    allow_domains: list = field(default_factory=list)   # 任务级域名白名单

    def to_dict(self) -> dict:
        """转为可 JSON 序列化的字典（队列持久化用）。"""
        return {
            "task_name": self.task_name, "start_url": self.start_url,
            "max_depth": self.max_depth, "crawl_external": self.crawl_external,
            "request_delay": self.request_delay, "max_chars_per_page": self.max_chars_per_page,
            "link_filter": self.link_filter, "jitter": self.jitter,
            "max_pages": self.max_pages, "max_retries": self.max_retries,
            "robots_check": self.robots_check, "priority": self.priority,
            "depends_on": list(self.depends_on), "proxy": dict(self.proxy),
            "block_patterns": list(self.block_patterns), "allow_domains": list(self.allow_domains),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TaskConfig":
        """从字典恢复配置（与 :meth:`to_dict` 对称）。"""
        return cls(
            task_name=str(data.get("task_name", "")),
            start_url=str(data.get("start_url", "")),
            max_depth=int(data.get("max_depth", 1)),
            crawl_external=bool(data.get("crawl_external", False)),
            request_delay=float(data.get("request_delay", 0.5)),
            max_chars_per_page=int(data.get("max_chars_per_page", 0)),
            link_filter=str(data.get("link_filter", "")),
            jitter=float(data.get("jitter", 0.3)),
            max_pages=int(data.get("max_pages", 0)),
            max_retries=int(data.get("max_retries", 2)),
            robots_check=bool(data.get("robots_check", True)),
            priority=int(data.get("priority", TaskPriority.MEDIUM)),
            depends_on=[int(x) for x in (data.get("depends_on") or [])],
            proxy=dict(data.get("proxy") or {}),
            block_patterns=[str(x) for x in (data.get("block_patterns") or [])],
            allow_domains=[str(x) for x in (data.get("allow_domains") or [])],
        )


class DownloadTask:
    """下载队列任务项：携带优先级，PriorityQueue 按 (priority, seq) 出队。

    优先级：页面图片等小资源(0) > 音频(1) > 文档(2) > 视频大文件(3) > 其他(4)。
    """

    __slots__ = ("priority", "seq", "url", "max_retries")

    def __init__(self, priority, seq, url, max_retries):
        self.priority = priority
        self.seq = seq
        self.url = url
        self.max_retries = max_retries

    def __lt__(self, other):
        return (self.priority, self.seq) < (other.priority, other.seq)


__all__ = [
    "TaskStatus", "STATUS_TEXT", "STATUS_COLORS",
    "TaskPriority", "PRIORITY_TEXT",
    "TaskConfig", "DownloadTask",
]
