# 全AI制作多功能图形化爬虫工具

> Full_AI_made_crawler_tool —— 基于 PyQt6 的功能型网络爬虫工具。

一款图形化的通用网络爬虫工具：从起始 URL 出发，按可配置深度递归抓取页面文字，自动识别并下载页面中的图片、视频、音频与文档资源；内置断点续爬、反爬策略与多目标并发任务管理。适用于**数据采集、内容聚合、网站备份**等场景。

## 功能特性

- 🚀 **递归爬取**：可配置爬取深度，BFS（广度优先）遍历策略
- 📦 **多内容类型**：文字 / 图片 / 视频 / 音频 / 文档一站式抓取下载
- 🔄 **断点续爬**：进度实时入库，重启后自动续传，不重复劳动
- 🛡️ **反爬策略**：UA 池轮换、代理支持、请求延迟抖动
- 🧩 **多任务并发**：多目标站点并行爬取，任务级数据库 / 目录 / 日志隔离，失败自动重试，队列持久化
- 📊 **多格式导出**：CSV / JSON / Markdown，支持单任务或全量汇总导出
- 🎮 **直观 GUI**：PyQt6 界面，快捷键控制（Ctrl+P / Ctrl+R 暂停·恢复）

## 快速开始

```bash
# 1. 克隆项目
git clone https://github.com/OnlyApasserby/Full_AI_made_crawler_tool.git
cd Full_AI_made_crawler_tool

# 2. 安装依赖
pip install -r requirements.txt

# 3. 启动程序
python main.py
```

## 环境要求

- Python 3.10+
- 必需：PyQt6、requests
- 可选：Pillow（图片压缩功能，缺失时自动降级，不影响主流程）

## 项目结构

```
Full_AI_made_crawler_tool/
├── main.py          # 程序入口（Ctrl+C 安全退出）
├── config.py        # 应用配置与默认参数
├── models.py        # 数据模型（任务 / 配置 / 状态）
├── core/            # 爬虫核心：递归爬取、下载、链接过滤、页面解析、robots
├── manager/         # 任务调度：多任务队列、并发线程池、数据库管理
├── ui/              # PyQt6 界面：主窗口、对话框、结果导出、任务管理
├── utils/           # 工具库：UA 池、URL 校验、辅助函数
└── resources/       # 资源文件与默认缓存数据库
```
