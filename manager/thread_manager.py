"""线程池管理：QThreadPool 异步执行体 + 普通后台工作线程辅助。

- ``ThreadPoolManager``：封装 QThreadPool，用于把耗时 IO（结果/队列落盘）丢到
  后台线程执行而不阻塞 GUI；
- ``spawn_workers``：一次性创建并启动 N 个普通守护工作线程（下载器使用）。
"""

from __future__ import annotations

import threading

from PyQt6.QtCore import QRunnable, QThreadPool


class _IORunnable(QRunnable):
    """把任意可调用对象包装为可在线程池执行的 QRunnable，异常静默吞掉。"""

    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    def run(self):
        try:
            self._fn()
        except Exception:
            pass


class ThreadPoolManager:
    """QThreadPool 的轻量封装：submit / wait_for_done / 并发数控制。"""

    def __init__(self, max_threads: int = 4):
        self._pool = QThreadPool()
        self._pool.setMaxThreadCount(max(1, int(max_threads)))

    def submit(self, fn) -> None:
        """提交一个可调用对象到线程池异步执行。"""
        self._pool.start(_IORunnable(fn))

    def wait_for_done(self, timeout_ms: int = -1) -> bool:
        """等待所有任务完成，返回是否在超时内完成。"""
        return self._pool.waitForDone(timeout_ms)

    def set_max_threads(self, n: int) -> None:
        self._pool.setMaxThreadCount(max(1, int(n)))

    def max_threads(self) -> int:
        return self._pool.maxThreadCount()


def spawn_workers(count: int, target, name_prefix: str = "worker",
                  daemon: bool = True) -> list[threading.Thread]:
    """创建并启动 ``count`` 个守护工作线程，返回线程列表（便于 join）。"""
    count = max(1, int(count))
    workers = [
        threading.Thread(target=target, daemon=daemon, name=f"{name_prefix}-{i}")
        for i in range(count)
    ]
    for w in workers:
        w.start()
    return workers


__all__ = ["ThreadPoolManager", "spawn_workers"]
