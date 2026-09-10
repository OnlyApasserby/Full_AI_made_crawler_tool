# 全AI制作多功能图形化爬虫工具

> Full_AI_made_crawler_tool —— 基于 PyQt6 的功能型网络爬虫工具。

一款图形化的通用网络爬虫工具：从起始 URL 出发，按可配置深度递归抓取页面文字，自动识别并下载页面中的图片、视频、音频与文档资源；内置断点续爬、反爬策略与多目标并发任务管理。适用于**数据采集、内容聚合、网站备份**等场景。

## 功能特性

- 🚀 **递归爬取**：可配置爬取深度，BFS（广度优先）遍历策略
- 🖼️ **图像/视频通用抓取**：静态请求 + 浏览器渲染双模式，支持懒加载、滚动加载、图集翻页与 XHR 网络监听
- 🎞️ **流媒体合并**：m3u8（HLS）/ mpd（DASH）自动解析、并发下载分片、AES-128 解密与合并，装 ffmpeg 后自动转封装为 MP4
- 🧩 **站点规则**：`resources/rules/*.json` 用选择器/正则适配特定站点，热加载、可测试，**不改代码**即可扩展
- 🔍 **去重与质量过滤**：URL 归一化去重、内容 MD5、感知哈希近似去重，按最小宽高/体积过滤图标与占位图
- 📦 **多内容类型**：文字 / 图片 / 视频 / 音频 / 文档一站式抓取下载
- 🔄 **断点续爬**：进度实时入库，重启后自动续传，不重复劳动
- 🛡️ **反爬策略**：UA 池轮换、代理支持、请求延迟抖动，浏览器渲染可携带真实 Cookie/Referer 过防盗链
- 🧩 **多任务并发**：多目标站点并行爬取，每个任务可选「递归爬取」或「媒体抓取」，任务级数据库 / 目录 / 日志隔离，失败自动重试，队列持久化
- 📊 **多格式导出**：CSV / JSON / Markdown，支持爬取结果与**媒体清单**（含分辨率/合集/来源页）
- 🎮 **直观 GUI**：PyQt6 界面，快捷键控制（Ctrl+P / Ctrl+R 暂停·恢复）

## 快速开始

```bash
# 1. 克隆项目
git clone https://github.com/OnlyApasserby/Full_AI_made_crawler_tool.git
cd Full_AI_made_crawler_tool

# 2. 安装依赖
pip install -r requirements.txt

# 3. 准备浏览器内核（动态渲染功能需要，二选一）
#    方式A（离线，推荐）：把 Chrome for Testing 压缩包放到项目根目录，程序首次渲染时自动解压
#        chrome-win64.zip
#    方式B（联网）：playwright install chromium

# 4. 启动程序
python main.py
```

## 环境要求

- Python 3.10+
- 必需：PyQt6、requests、playwright（+ 浏览器内核）
- 可选：Pillow（图片压缩、尺寸探测与感知哈希去重，缺失时自动降级）
- 可选：pycryptodome 或 cryptography（HLS 加密流解密，缺失时加密流会给出明确提示）
- 可选：beautifulsoup4 + lxml（站点规则提取器的完整 CSS 选择器；缺失时退回内置简化选择器）
- 可选：ffmpeg（外部程序，加入 PATH 即可；用于流媒体转封装 MP4 与音视频合流，缺失时保留原始容器）

## 打包为单个 exe

```bash
# 1) 安装打包工具（可选依赖）
pip install pyinstaller

# 2) 常规构建：产物 dist\FullAICrawler.exe（约 92MB，窗口程序）
pyinstaller crawler.spec --noconfirm

# 3) 控制台版（排查用，可执行自检）：产物 dist\FullAICrawler-debug.exe
set BUILD_CONSOLE=1
pyinstaller crawler.spec --noconfirm

# 4) 完全自包含（把本地浏览器内核一起打进去，exe 约 650MB）
set BUNDLE_BROWSER=1
pyinstaller crawler.spec --noconfirm
```

**验证打包结果**（控制台版可直接看输出）：

```bash
FullAICrawler-debug.exe --selftest          # 环境自检：路径/数据库/浏览器/Playwright/规则/依赖
FullAICrawler-debug.exe --selftest --gui    # 界面冒烟：创建主窗口数秒后自动退出
```

### 浏览器内核的取舍

`--onefile` 的 exe **每次启动都会把内容解压到临时目录**，因此默认**不打内核**（启动快、体积小）。
打包后的程序按以下顺序自动寻找可用浏览器：

1. 环境变量 `CRAWLER_CHROME_PATH`
2. exe 同级 `resources\browsers\`（把解压好的内核拷到此处即可）
3. 打包内置目录（`BUNDLE_BROWSER=1` 时才有）
4. 系统已安装的 Chrome / Edge
5. PATH 中的 chrome / chromium
6. 以上都没有时，自动解压 exe 同级的 `chrome-win64.zip`

所以**推荐做法**：把 `chrome-win64.zip`（约 200MB）或解压好的 `resources\browsers`
与 exe 放在一起，即可获得完整的动态渲染能力，同时保持 exe 本身体积与启动速度。

### 打包后的目录约定

程序以 exe 所在目录为**可写**应用目录，运行期会在同级生成：

```
FullAICrawler.exe
resources\crawler_cache.db      # 主缓存数据库
resources\rules\                # 用户自建站点规则（随包规则只读内置）
resources\browsers\             # 浏览器内核（自动解压或拷贝）
task_data\                      # 多任务数据
downloads\                      # 下载内容
```

## 使用要点

**图像/视频抓取**：在「爬取配置」页勾选「启用媒体抓取增强流程」，选择目标类型与抓取模式，
点击「确认启动媒体抓取」。结果会写入结果表格并同步到「下载管理」，可点「导出媒体清单」保存元信息。

**流媒体合并**：媒体抓取分组中勾选「合并流媒体」，下载 m3u8/mpd 时会自动下载分片并合并
（未安装 ffmpeg 时输出 `.ts`，装上后自动转 `.mp4`）。

**站点规则**：点工具栏「站点规则」→ 新建/编辑 JSON → 「抓取并测试」验证命中数 → 「保存并重载」立即生效。
规则格式见 `resources/rules/_template.json`（下划线开头的文件不会参与加载）。

**多任务**：在「任务管理」页点「添加任务」，任务类型可选「媒体抓取（图像/视频）」，
全局配置模式下会沿用「爬取配置」页的详细媒体设置。

### 浏览器内核配置

动态渲染（`core/fetcher/playwright_fetcher.py`）需要 Chromium 内核，按以下优先级查找：

1. 环境变量 `CRAWLER_CHROME_PATH` 指定的 `chrome.exe`
2. `resources/browsers/` 下已解压的内核
3. 项目根目录已解压的内核目录（如 `chrome-win64/`）
4. 系统已安装的 Chrome / Edge / Chromium
5. PATH 中的 `chrome` / `chromium`
6. 以上都没有时，自动解压项目根目录的 `chrome-win64.zip`

## 项目结构

```
Full_AI_made_crawler_tool/
├── main.py          # 程序入口（Ctrl+C 安全退出）
├── config.py        # 应用配置与默认参数
├── models.py        # 数据模型（任务 / 配置 / 状态）
├── core/            # 爬虫核心
│   ├── media/       #   媒体领域层：类型识别 / 数据模型 / 尺寸探测 / 流媒体（HLS·DASH·合并）
│   ├── fetcher/     #   统一抓取层：静态 requests + 动态 playwright（含 XHR 网络监听）
│   ├── extractor/   #   媒体提取器插件体系（规则 / 网络事件 / HTML / JSON）
│   ├── paginator.py #   图集/列表翻页策略（下一页链接、数字页码、接口模板）
│   ├── pipeline.py  #   去重与质量过滤流水线（URL 归一化、MD5、感知哈希、尺寸体积）
│   ├── media_crawler.py # 媒体抓取编排线程
│   └── ...          #   递归爬取、下载、链接过滤、页面解析、robots、安全检测
├── manager/         # 任务调度：多任务队列、并发线程池、数据库管理
├── ui/              # PyQt6 界面：主窗口、站点规则对话框、结果导出、任务管理
├── utils/           # 工具库：UA 池、URL 校验、辅助函数
└── resources/       # 资源文件、默认缓存数据库、本地浏览器内核与站点规则
    ├── browsers/    #   离线浏览器内核（chrome-win64，自动解压）
    └── rules/       #   站点规则 *.json（热加载，_template.json 为格式示例）
```
