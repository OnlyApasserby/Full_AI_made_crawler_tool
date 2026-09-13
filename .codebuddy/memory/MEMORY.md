# MEMORY —— 长期事实与项目约定

项目：`Full_AI_made_crawler_tool`（PyQt6 图形化爬虫工具），工作区 `f:\develop\1919`。

## 架构约定（新增功能请遵循）

- **分层**：`core/`（不依赖 UI，可单测）→ `manager/`（调度与 sqlite）→ `ui/`（PyQt6）。
  `core/__init__.py` 的 docstring 维护模块清单，新增子模块要补一行。
- **常量集中**：所有魔法数字/路径放 `config.py`，用 `DEFAULT_*` / 模块前缀命名并登记进 `__all__`。
- **纯函数分析层**：分析类逻辑写成纯函数 + dataclass（见 `core/security.py`、`core/compliance/`），
  Qt 只出现在 `QThread` 编排线程与 `ui/*_dialog.py` 里。
- **后台任务模式**：`QThread` 子类 + `signal_log/signal_page_done/signal_progress/signal_finish/
  signal_stopped/signal_error/signal_paused/signal_resumed`；`pause()/resume()/stop()` 用
  `threading.Event`（`_pause_event` / `_stop_event`）在任务之间生效。参考
  `core/media_crawler.py` / `core/compliance/scanner.py`。
- **抓取层**：统一走 `core/fetcher`（`create_fetcher` / `RequestsFetcher` / `PlaywrightFetcher`），
  不要直接 `requests.get`，以复用 UA 池、代理、间隔抖动与停止语义。
- **数据库**：所有 sqlite 访问集中在 `manager/db_manager.py`，建表与补列放 `ensure_schema()`；
  函数内部容错（失败返回 0/False/[]），不向上抛。
- **导出**：`ui/export.py` 约定"UTF-8 with BOM（CSV/MD）/ utf-8（JSON）"，写文件失败抛 `OSError` 由 UI 提示。
- **浏览器内核**：优先本地 `chrome-win64.zip`（根目录，可写目录 `resources/browsers/`），
  环境变量 `CRAWLER_CHROME_PATH` 优先级最高。

## 用户偏好

- **不引入新依赖**：优先复用已有库（`requests`、可选 `bs4`/`lxml`、`Pillow`），
  缺失时必须优雅降级并给出明确原因，而不是崩溃或静默跳过。
- **中文注释与文档**：docstring 说明"为什么这么做"，不只是描述代码做了什么。
- **降级要给明确原因**：失败路径必须返回可读、可操作的原因（说明问题在哪、该改哪个配置），
  并保留已经得到的本地结果，不能因为一个环节失败就丢结果。
- **诚实标注边界**：扫不到的东西要显式记录原因（如 PDF 条款页不解析、页面 403/404 只跳过），
  不做"看起来成功"的静默降级。
- 收尾：临时验证脚本用完即删；改动后跑 `read_lints`。

## 合规辅助筛查（core/compliance/）

- **不可越界**：只做合规线索筛查，不提供法律意见、不代替律师、**不下"可以/不可以抓取"的结论**；
  报告结果不参与任何自动放行/阻断（robots.txt 是唯一硬闸门）。
- 用户已确认：LLM 默认开启；API Key 环境变量 `CHATANYWHERE_API_KEY` 优先、UI 输入仅存内存；
  报告落盘 `resources/compliance/` 并入库 `compliance_reports`。
- 引文一律取本地切分原文，模型返回的引文只在通过 `guard.quote_in_source` 校验后才可能被采纳。
