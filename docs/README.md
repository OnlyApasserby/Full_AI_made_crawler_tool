# 网络爬虫学习方案

> 基于工作区项目 **Full_AI_made_crawler_tool**（`f:/develop/1919`，PyQt6 图形化爬虫工具）整理的爬虫系统学习方案。
> 理念：**先跑起来 → 再读懂 → 再改得动 → 最后自己写**。所有知识点都落到本项目的真实源码上，所有练习都给出可运行的验证方式。

## 1. 方案简介

本方案不另造示例代码，而是把项目本身当作「活的爬虫教科书」：项目里覆盖了爬虫工程的完整技术栈——静态/动态抓取、页面解析、BFS 递归、URL 与内容去重、SQLite 持久化与断点续爬、图片/视频/流媒体（HLS/DASH）下载、反爬与登录态复用、多任务并发调度、合规筛查、测试与打包。

学习路径按 10 个阶段递进，每个阶段固定包含五件套：

1. **阶段目标**——学完后能做什么；
2. **知识点**——概念 + 项目中的实现位置；
3. **源码精读清单**——要读哪些文件、重点看什么；
4. **动手练习**——可运行、可验证；
5. **验收标准**——过关 checklist。

## 2. 适用对象与先修要求

| 项目 | 要求 |
|---|---|
| 编程基础 | Python 函数 / 类 / 装饰器 / 上下文管理器 / 生成器 |
| 工具链 | 会使用 pip、venv、PowerShell / 命令行 |
| 网络基础 | **不需要**，阶段 01 会补齐最小集合 |
| Qt / 前端 | **不需要**，界面只作为"使用方"出现，不要求写 UI |

如果你已经能熟练使用 requests + BeautifulSoup 写脚本，可以从阶段 03 开始，把 01/02 当作查漏补缺的参考。

## 3. 学习路线图

| 阶段 | 主题 | 对应项目源码 | 产出物 |
|---|---|---|---|
| [01](01-爬虫基础与项目架构.md) | 爬虫基础与项目架构 | `main.py`、`config.py`、`models.py`、`README.md` | 跑通的环境 + 手绘架构图 |
| [02](02-静态抓取与页面解析.md) | 静态抓取与页面解析 | `core/fetcher/requests_fetcher.py`、`core/parser.py`、`core/robots.py` | 迷你静态爬虫 |
| [03](03-递归爬取与URL管理.md) | 递归爬取与 URL 管理 | `core/crawler.py`、`core/filter.py`、`core/security.py` | 迷你 BFS 爬虫 |
| [04](04-数据持久化与断点续爬.md) | 数据持久化与断点续爬 | `manager/db_manager.py`、`manager/task_manager.py` | 可续爬的爬虫 |
| [05](05-动态渲染与浏览器自动化.md) | 动态渲染与浏览器自动化 | `core/fetcher/playwright_fetcher.py`、`browser_pool.py`、`chrome_setup.py` | SPA 抓取脚本 |
| [06](06-媒体抓取与流媒体下载.md) | 媒体抓取与流媒体下载 | `core/extractor/`、`core/paginator.py`、`core/pipeline.py`、`core/downloader.py`、`core/media/` | 图站爬虫 + m3u8 下载器 |
| [07](07-反爬策略与登录态复用.md) | 反爬策略与登录态复用 | `core/browser_session/`、`utils/user_agents.py` | 带登录态的采集脚本 |
| [08](08-多任务并发与任务调度.md) | 多任务并发与任务调度 | `manager/task_manager.py`、`thread_manager.py` | 迷你任务调度器 |
| [09](09-合规测试与工程化.md) | 合规、测试与工程化 | `core/compliance/`、`core/robots.py`、`tests/`、`crawler.spec` | 测试全绿 + exe 产物 |
| [10](10-综合实战与验收.md) | 综合实战 | 全链路 | 4 个综合项目 |

**建议节奏**：每阶段 3~7 天；阶段 06（媒体与流媒体）和 07（登录态）内容最重，可各分配 1~2 周。

## 4. 环境准备

```bash
# 1) Python 3.10+（项目要求），建议建虚拟环境
python -m venv .venv
.\.venv\Scripts\activate        # Windows PowerShell

# 2) 安装依赖
pip install -r requirements.txt

# 3) 准备浏览器内核（动态渲染需要，二选一）
#    方式A（离线，推荐）：把 chrome-win64.zip 放到项目根目录，程序首次渲染时自动解压
#    方式B（联网）：playwright install chromium

# 4) 启动程序
python main.py

# 5) 环境自检（路径/数据库/浏览器/Playwright/规则/依赖逐项检查）
python main.py --selftest

# 6) 跑单元测试（不需要浏览器、不访问网络）
python -m pytest tests/ -q
```

可选依赖（缺失均自动降级，不阻断主流程）：`Pillow`（图片处理/感知去重）、`pycryptodome`（HLS AES-128 解密）、`cryptography`（Cookie 加密）、`selenium`（备用驱动）、`pytest`、`pyinstaller`。

## 5. 如何使用本方案

1. **按顺序学**：阶段间有依赖（如 06 依赖 02/05 的抓取与渲染基础）；
2. **源码为主、文档为辅**：每个"源码精读"都要真的打开文件读，文档只给阅读线索；
3. **练习必须落地**：练习都设计成"跑一条命令/写一个脚本能看到结果"，不许停留在"看懂了"；
4. **善用自检**：`python main.py --selftest` 和 `python -m core.browser_session` 是两个随身的只读体检工具；
5. **记录踩坑**：建议每阶段写一篇 `学习笔记`（为什么错、怎么修），阶段 10 的自检清单会用到。

## 6. 合规与法律边界（必读）

本方案与项目本身都坚持以下边界，学习与实战中同样适用：

- **robots.txt 是唯一的硬性闸门**：项目所有自动抓取前先过 `core/robots.py`；
- 只抓取**公开可访问**的数据，控制请求频率（延迟抖动/限速），不对站点造成压力；
- **登录态只复用本人会话**：不绕过验证码、不破解加密、不伪造他人身份；
- 合规辅助筛查（`core/compliance/`）**只做线索筛查**，不构成法律意见，不判断"能不能抓"，最终请人工/法务确认；
- 练习优先使用 `example.com`、`httpbin.org`、公开测试流等安全目标；对真实站点抓取前先确认其条款与 robots.txt。

## 7. 目录导航

```
docs/
├── README.md                          # 本文件：总览与路线图
├── 01-爬虫基础与项目架构.md
├── 02-静态抓取与页面解析.md
├── 03-递归爬取与URL管理.md
├── 04-数据持久化与断点续爬.md
├── 05-动态渲染与浏览器自动化.md
├── 06-媒体抓取与流媒体下载.md
├── 07-反爬策略与登录态复用.md
├── 08-多任务并发与任务调度.md
├── 09-合规测试与工程化.md
├── 10-综合实战与验收.md
└── 附录-项目代码地图.md                # 全模块职责速查（检索调研结论）
```
