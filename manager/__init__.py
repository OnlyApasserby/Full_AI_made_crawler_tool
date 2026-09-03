"""manager —— 管理协调层。

- db_manager：数据库集中访问（核心/UI 共用）
- thread_manager：线程池（QThreadPool 落盘 + 后台工作线程）
- task_manager：多任务调度器 ``TaskScheduler`` 与合并导出
"""
