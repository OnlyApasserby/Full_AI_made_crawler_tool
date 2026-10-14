"""core —— 核心爬虫功能包（不依赖 UI，可被 manager / ui 引用）。

- crawler：递归爬取线程 ``RecursiveCrawlerThread``
- downloader：媒体下载器（图片/视频） ``ContentDownloader`` / ``DownloadTask``
- filter：URL 过滤规则 ``URLFilter``
- parser：页面解析（提取链接 / 媒体 / 纯文字）
- robots：robots.txt 解析与在线校验线程
- security：广告/钓鱼链接识别与屏蔽正则生成
- media：媒体领域层（类型识别 ``detector``、数据模型 ``models``、探测 ``probe``、
  流媒体 ``hls`` / ``dash`` / ``merger``）
- fetcher：统一抓取层（静态 requests / 动态 playwright）
- extractor：媒体提取器插件体系（注册表 + 通用 HTML/JSON/网络监听提取器）
- paginator：图集/列表翻页策略
- pipeline：媒体去重与质量过滤流水线
- media_crawler：媒体抓取编排线程 ``MediaCrawlThread``
- browser_session：浏览器登录态复用模块（复用使用者**本人**已登录的 User Data /
  Cookie，跳过登录流程并降低风控误判）——统一入口 ``load_browser_session(config)``，
  子模块 config / cookie_store / launcher / session_bridge / login_state /
  anti_detect / registry 各自职责单一、可单独测试；只做指纹一致性与行为拟人化，
  **不实现**验证码绕过 / 破解加密 / 伪造他人身份（见 ``core/browser_session/README.md``）
- ebook_fetcher：电子书正文抓取 ``fetch_book``——从阅读页起始地址自动翻页，
  清洗后把整本书落盘为 UTF-8 纯文本，并输出 JSON 元数据
  （书名 / 章节列表 / 页数 / 抓取时间；遵守 robots.txt，支持 headers/cookies/proxy）
- compliance：合规辅助筛查（robots.txt 校验的扩展）——扫描公开法律页面，
  整理与爬虫/自动化访问/数据采集/请求频率/API/绕过限制相关的条款线索。
  **只做合规线索筛查，不提供法律意见，也不判断能否抓取**（见 ``core/compliance/__init__.py``）

持久化（sqlite）集中在 manager/db_manager，本包通过它读写数据库。
"""
