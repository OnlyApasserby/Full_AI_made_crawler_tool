"""core —— 核心爬虫功能包（不依赖 UI，可被 manager / ui 引用）。

- crawler：递归爬取线程 ``RecursiveCrawlerThread``
- downloader：媒体下载器（图片/视频） ``ContentDownloader`` / ``DownloadTask``
- filter：URL 过滤规则 ``URLFilter``
- parser：页面解析（提取链接 / 媒体 / 纯文字）
- robots：robots.txt 解析与在线校验线程

持久化（sqlite）集中在 manager/db_manager，本包通过它读写数据库。
"""
