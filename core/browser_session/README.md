# 浏览器登录态复用模块（`core/browser_session`）

> 让爬虫工具直接复用**使用者本人**本地浏览器已登录的会话（User Data / Cookie），
> 从而跳过登录流程，并降低触发网站风控的概率。

本模块是框架内的**可插拔模块**（不是独立脚本）：遵循既有的注册、配置与日志约定，
通过统一入口 `load_browser_session(config)` 使用，生命周期钩子由
`core/browser_session/registry.py` 暴露。

---

## 目录

- [1. 它能做什么、不能做什么](#1-它能做什么不能做什么)
- [2. 快速开始](#2-快速开始)
- [3. 子模块划分](#3-子模块划分)
- [4. 公开接口](#4-公开接口)
- [5. 配置项](#5-配置项)
- [6. 登录态检查规则](#6-登录态检查规则)
- [7. 配置文件示例与环境变量](#7-配置文件示例与环境变量)
- [8. 常用问题（FAQ）](#8-常用问题faq)
- [9. 与框架其它部分的集成](#9-与框架其它部分的集成)
- [10. 测试](#10-测试)

---

## 1. 它能做什么、不能做什么

### 能做

| 能力 | 说明 |
|---|---|
| 复用 User Data | 直接以 `--user-data-dir` + `--profile-directory` 启动 Chrome/Edge，**启动即带登录态** |
| Cookie 导出/导入 | 从已登录浏览器导出 Cookie、加密落盘、注入新会话（User Data 用不了时的回退方案） |
| 多请求层复用 | 同一份登录态喂给 **Selenium / Playwright / requests**，UA 与请求头保持与浏览器一致 |
| 登录态检查 | 统一的 `check_login()`，支持状态码 / 页面关键字 / 选择器三类规则 |
| 降低风控误判 | 真实指纹补全（`navigator.webdriver`、plugins、WebGL、时区、地理位置）+ 行为拟人化 + 代理 |
| 生命周期 | 初始化 / 健康检查 / 销毁钩子，可在界面或自检里统一接线 |

### 不能做（合规红线）

- **不**实现绕过验证码、破解加密、伪造他人身份等任何功能
- **不**提供"绕过风控审核"的承诺；反检测只做**指纹一致性**与**行为拟人化**，
  目的是降低被**误判**为机器人的概率
- **不**判断目标站点是否允许抓取

> ⚠️ 本模块仅用于复用**使用者本人已登录**的会话，服务于合法授权的自动化场景。
> 目标站点若明确禁止自动化访问，请**自行确认合规性**后再使用。
> Cookie 等同账号凭据：导出的文件默认加密，`resources/sessions/` 不应提交到版本库。

---

## 2. 快速开始

### 2.1 最小示例（启动带登录态的浏览器 → 检查登录 → 导出 Cookie → requests 复用）

可直接运行仓库内的示例脚本：

```bash
# 前置条件：目标浏览器**完全退出**（见 §8.1，这是最常见的报错原因）
python examples/browser_session_minimal.py --domain example.com --browser edge \
    --check-url https://example.com/
```

代码层面的等价写法：

```python
from core.browser_session import load_browser_session

# 1) 加载本地浏览器登录态（惰性启动：真正取 driver 时才拉起浏览器）
handle = load_browser_session({"browser": "edge", "domain": "example.com"})
try:
    # 2) 启动带登录态的浏览器
    driver = handle.get_driver()
    driver.get("https://example.com/")

    # 3) 检查登录态
    if not handle.check_login():
        print(handle.relogin_hint())      # 给使用者看的手动重登步骤

    # 4) 导出 Cookie（默认加密；密钥由 CRAWLER_SESSION_KEY 提供）
    handle.export_cookies("resources/sessions/example.com.json")

    # 5) 用 requests 复用同一登录态（UA / Cookie / 代理 / 请求头都一致）
    session = handle.get_requests_session()
    resp = session.get("https://example.com/me")
    print(resp.status_code, len(resp.text))
finally:
    handle.close()                        # 关闭浏览器、释放连接池
```

### 2.2 只用 Cookie 的回退方案（不启动浏览器）

浏览器不能关、服务器没图形环境、或只想发静态请求时用这个：

```python
from core.browser_session import session_from_cookies

handle = session_from_cookies(
    {"domain": "example.com"},
    cookie_path="resources/sessions/example.com.json")
session = handle.get_requests_session()
print(session.get("https://example.com/me").status_code)
handle.close()
```

先用实时模式导出一次 Cookie（`--export-only`）即可长期复用：

```bash
python examples/browser_session_minimal.py --domain example.com --export-only
python examples/browser_session_minimal.py --domain example.com --cookie-only
```

### 2.3 依赖

| 依赖 | 必需性 | 说明 |
|---|---|---|
| `playwright` | 推荐 | 默认驱动。用 `launch_persistent_context` 复用 User Data，**无需额外下载驱动** |
| `selenium` | 可选 | `selenium>=4.6` 自带 Selenium Manager；**首次运行需联网**下载 chromedriver，离线环境请改用 Playwright |
| `requests` | 必需 | 框架已有依赖，用于 requests 层复用与 http 通道探测 |
| `cryptography` | 可选 | Cookie 加密存储（Fernet）。缺失时自动降级为**明文**并在日志/返回值中明确标注 |
| `pyyaml` | 可选 | 仅用于解析 YAML 配置示例；JSON 配置零依赖 |

框架已有的 `resources/browsers/` 本地内核会被自动复用（`CRAWLER_CHROME_PATH`
→ `resources/browsers` → 系统安装 → PATH），**不需要**再执行
`playwright install chromium`。

---

## 3. 子模块划分

每个子模块职责单一、可单独导入与测试：

| 子模块 | 职责 |
|---|---|
| `config.py` | 配置结构 `SessionConfig`、平台默认路径探测、环境变量覆盖、路径校验 |
| `launcher.py` | `BrowserLauncher`：启动带 User Data 的实例、**占用检测**、健康检查 |
| `cookie_store.py` | `CookieStore`：导出 / 加密持久化 / 加载 / 注入 / 规范化 |
| `session_bridge.py` | `SessionBridge`：登录态桥接到 requests / Playwright / 框架抓取层 |
| `login_state.py` | `LoginStateChecker`：登录态判定规则、通道选择、失效重登入口 |
| `anti_detect.py` | `AntiDetection`：反检测脚本、行为拟人化工具、代理接入 |
| `adapters.py` | `SeleniumDriver` / `PlaywrightDriver`：同一套驱动接口的两个实现 |
| `exceptions.py` | 自定义异常（环境类 / Cookie 类 / 登录态类，消息均"可操作"） |
| `registry.py` | `ModuleRegistry` 接入层：模块注册、统一入口、生命周期钩子 |

```
core/browser_session/
├── __init__.py         # 统一入口 load_browser_session / SessionHandle
├── __main__.py         # python -m core.browser_session 只读自检
├── config.py
├── launcher.py
├── cookie_store.py
├── session_bridge.py
├── login_state.py
├── anti_detect.py
├── adapters.py
├── exceptions.py
└── registry.py
```

---

## 4. 公开接口

### 4.1 统一入口

```python
load_browser_session(config=None, *, driver=None, autostart=False,
                     cookie_path="", progress=None, **overrides) -> SessionHandle
```

| 参数 | 说明 |
|---|---|
| `config` | `SessionConfig` / 配置字典 / `None`（全默认） |
| `driver` | 复用外部已创建的驱动（传了就不再启动浏览器） |
| `autostart` | 是否立即启动浏览器（默认 `False` = 惰性启动） |
| `cookie_path` | 顺带从该文件载入 Cookie（纯 Cookie 场景的快捷方式） |
| `progress` | 回调 `progress(消息)`，可把关键步骤写进界面日志 |
| `**overrides` | 直接覆盖配置字段，如 `domain="example.com"` |

### 4.2 `SessionHandle`

| 方法 | 说明 |
|---|---|
| `get_driver()` | 启动（必要时）并返回统一驱动对象 |
| `get_requests_session()` | 返回带登录态与一致指纹的 `requests.Session` |
| `get_playwright_context()` | 返回 Playwright 持久化上下文（可 `new_page()`） |
| `get_fetcher(mode="static")` | 返回框架抓取器 `core.fetcher.BaseFetcher` |
| `fetch(url, **kwargs)` | 按框架 `fetch()` 契约抓取（上层可直接当抓取器用） |
| `check_login()` | `bool`：登录态是否有效 |
| `check_login_detail()` | `CheckResult`：带判定依据与证据 |
| `require_login()` | 未登录时抛 `LoginStateError`（附重登步骤） |
| `relogin_hint()` | 手动重登步骤文本 |
| `open_login_page(url="")` | 打开登录页（供使用者手动登录一次） |
| `export_cookies(path="")` | 导出并落盘，返回写入路径 |
| `load_cookies(path="", inject=True)` | 载入 Cookie；浏览器在运行时立即注入 |
| `cookies()` / `cookie_summary()` | 当前 Cookie 列表 / 不含明文值的摘要 |
| `health_check()` | 结构化健康检查 |
| `close()` | 关闭浏览器、释放连接池（可重复调用） |

`SessionHandle` 也支持 `with` 语句。

### 4.3 驱动解耦

```python
handle.get_driver()            # 返回 BrowserDriver（抽象基类）
driver.get(url)                # 导航
driver.current_url()           # 当前 URL
driver.page_source() / text()  # 页面内容
driver.add_cookies(cookies)    # 注入（返回 (成功数, 失败原因)）
driver.wait_for_selector(sel)  # 元素探测（登录态选择器规则用）
driver.human_click(sel)        # 拟人化点击
driver.quit()                  # 关闭
```

`launcher="playwright"`（默认）与 `launcher="selenium"` 返回**同一套接口**，
上层代码无需区分：

```python
from core.browser_session.adapters import register_launcher, LAUNCHER_ADAPTERS
# 也可注册自定义驱动实现
register_launcher("my-launcher", my_launch_function)
```

---

## 5. 配置项

完整字段见 `SessionConfig`（`core/browser_session/config.py`），默认值集中在框架
`config.py` 的 `DEFAULT_SESSION_*`。常用项：

| 字段 | 默认 | 说明 |
|---|---|---|
| `browser` | `chrome` | `chrome` / `edge` / `chromium` |
| `user_data_dir` | 自动探测 | User Data 根目录；留空按平台探测 |
| `profile` | `Default` | Profile 目录名（`Default` / `Profile 1` …） |
| `launcher` | `playwright` | `playwright` / `selenium` |
| `executable_path` | 复用框架查找链 | 浏览器内核路径 |
| `domain` | 空（**必填**） | 目标域名；决定 Cookie 分组与探测 URL |
| `cookie_store_path` | `resources/sessions/<domain>.json` | Cookie 文件路径 |
| `cookie_encrypt` | `true` | 是否加密落盘（需 `cryptography`） |
| `cookie_key` | 环境变量 | 加密口令，建议用 `CRAWLER_SESSION_KEY` |
| `cookie_accept` | `all` | `same-site` 只保留目标域名族，更克制 |
| `proxy` | `{}` | 代理；住宅代理优先。支持 `{server,username,password}` 与框架的 `{https:...}` 写法 |
| `headless` | `false` | **复用 User Data 时必须 false** |
| `impersonate` | `false` | 使用本地 Profile 的真实指纹 |
| `timezone` / `geolocation` | 空 | 必须与**代理 IP 的地理位置一致** |
| `check_login` | `true` | 启动后是否立即校验登录态 |
| `login_url` | `https://<domain>/` | 探测 URL |
| `logout_markers` / `login_markers` | 空（有内置兜底词） | 判定关键字 |
| `login_status` | `[200]` | 视为已登录的状态码 |
| `logout_selector` / `login_selector` | 空 | 元素级判定（最准） |
| `cookie_assume_valid` | `true` | 无规则可用时的兜底结论 |
| `anti_detect` | `true` | 注入反检测脚本 |
| `delay_mean` / `delay_jitter` | `0` / `0.5` | 行为拟人化停顿（秒） |
| `action_interval` | `0` | 同类操作最小间隔（秒） |
| `rate_limit` | `0` | 每分钟请求上限（0 = 不限） |
| `mouse_steps` | `24` | 鼠标轨迹插值点数 |

### 路径自动探测

`default_user_data_dir(browser)` 覆盖三个平台的默认位置：

| 平台 | Chrome | Edge |
|---|---|---|
| Windows | `%LOCALAPPDATA%\Google\Chrome\User Data` | `%LOCALAPPDATA%\Microsoft\Edge\User Data` |
| macOS | `~/Library/Application Support/Google/Chrome` | `~/Library/Application Support/Microsoft Edge` |
| Linux | `~/.config/google-chrome`（含 snap） | `~/.config/microsoft-edge`（含 snap） |

`Chrome for Testing` 也被探测，但优先级低于正式 Chrome（它通常几乎没有登录态）。

---

## 6. 登录态检查规则

判定按**优先级从上到下**，命中即返回：

1. **登出选择器 / 关键字**（`logout_selector` / `logout_markers`）——最强负信号：
   页面出现"请先登录"时，任何正向信号都不足采信
2. **登录选择器 / 关键字**（`login_selector` / `login_markers`）——出现即有效
3. **状态码**（`login_status`）——命中即有效；未命中即判定未登录
4. **兜底**：以上都没配置时，用"Cookie 是否过期"这一静态判据给出结论，
   结果里会标注 `fallback=True`，并在 `warnings` 中建议补齐规则

三条检测通道（自动选择，也可 `channel=` 强制）：

| 通道 | 何时用 | 特点 |
|---|---|---|
| `browser` | 有运行中的浏览器 | **最准**：能跑选择器、能看最终落点 URL |
| `http` | 没有浏览器但有 Cookie | 快；无 JS 渲染；能看状态码与重定向 |
| `cookie` | 什么都没有 | 静态兜底；只看 Cookie 是否存在/过期 |

```python
result = handle.check_login_detail()
print(result.describe())     # 登录态检查：已登录（browser 通道） — 命中登录关键字：'个人中心'
print(result.logged_in, result.reason, result.evidence, result.fallback)
```

**登录态失效时**：`check_login()` 返回 `False`；`require_login()` 抛
`LoginStateError`，异常消息里带完整重登步骤：

```python
try:
    handle.require_login()
except LoginStateError as exc:
    print(exc)     # 1) 关闭浏览器 2) 启动 3) 手动登录（含人工验证）4) 导出 Cookie 5) 复查
```

> 本模块**不代替使用者登录**，更不做验证码绕过——只把浏览器窗口送到登录页，
> 由使用者完成人工步骤后重新导出 Cookie。

---

## 7. 配置文件示例与环境变量

- `resources/sessions/config.example.yaml`（可读性好，需 `pyyaml` 解析）
- `resources/sessions/config.example.json`（零依赖，含 Cookie 文件格式说明）

```python
import json
from core.browser_session import load_browser_session

with open("resources/sessions/config.example.json", encoding="utf-8") as f:
    data = json.load(f)["browser_session"]
handle = load_browser_session(data)
```

**优先级**：默认值 → 环境变量 → 代码显式传参。

环境变量（前缀 `CRAWLER_SESSION_`）：

```bash
CRAWLER_SESSION_BROWSER=edge
CRAWLER_SESSION_USER_DATA_DIR=D:\edge-userdata
CRAWLER_SESSION_PROFILE=Profile 2
CRAWLER_SESSION_DOMAIN=example.com
CRAWLER_SESSION_COOKIE_STORE=D:\cookies\example.json
CRAWLER_SESSION_COOKIE_ENCRYPT=1
CRAWLER_SESSION_HEADLESS=0
CRAWLER_SESSION_LOGIN_MARKERS=退出登录,个人中心
CRAWLER_SESSION_LOGOUT_MARKERS=请先登录,登录已过期
CRAWLER_SESSION_CHECK_TIMEOUT=8
CRAWLER_SESSION_KEY=<加密口令>            # 也可用 CRAWLER_SESSION_COOKIE_KEY
CRAWLER_CHROME_PATH=D:\chrome-win64\chrome.exe   # 框架既有变量，本模块同样识别
```

### Cookie 存储格式

```json
{
  "version": 1,
  "domain": "example.com",
  "browser": "edge",
  "profile": "Default",
  "exported_at": "2026-01-01 12:00:00",
  "encrypted": false,
  "fingerprint": "3f2a9c1d5e7b8064",
  "cookie_count": 2,
  "domain_counts": {".example.com": 1, "www.example.com": 1},
  "notes": [],
  "cookies": [
    {"name": "sessionid", "value": "...", "domain": ".example.com", "path": "/",
     "secure": true, "httpOnly": true, "expiry": 1893456000, "sameSite": "Lax"}
  ]
}
```

- `fingerprint` 是 Cookie 集合的**短哈希**（不含明文值），用于判断"当前登录态与
  上次导出是否同一份"
- 读取时**自动识别**明文与加密两种格式；加密文件形如
  `#!{"encrypted": true, "algorithm": "fernet", ...}` + 换行 + 密文
- 归一化会容忍：缺失 `domain`（按页面 URL 或目标域名补齐）、
  过期时间的各种写法（Unix 秒 / 毫秒 / Chromium 微秒 / ISO8601 / 日期字符串）、
  `sameSite` 的小写与别名、`__Host-` 前缀（自动改为 host-only）

---

## 8. 常用问题（FAQ）

### 8.1 ⚠️ 必须先关闭浏览器（最高频的问题）

**现象**

```
BrowserAlreadyRunning: 检测到正在运行的浏览器进程占用了该 User Data 目录：
  User Data：C:\Users\me\AppData\Local\Microsoft\Edge\User Data
  Profile  ：…\Default
  **必须先完全退出该浏览器再运行本工具**
```

**原因**：Chromium 对同一个 `--user-data-dir` 是**独占**的。若已有实例在跑，
新进程会把启动请求转交给它然后**自己退出**——自动化侧只能拿到空壳或超时，
这正是"静默失败"的典型来源。本模块因此**主动检测并明确报错**，而不是静默失败。

**解决**

| 平台 | 操作 |
|---|---|
| Windows | 关闭所有浏览器窗口，再在**任务管理器**里结束残留的 `chrome.exe` / `msedge.exe`（含后台驻留），然后重试 |
| macOS | 退出 Chrome/Edge（⌘Q），必要时在「活动监视器」里结束进程 |
| Linux | `pkill -f 'chrome\|chromium\|msedge'` |

**替代方案**：不想关浏览器就用 **Cookie 导出/导入**（`session_from_cookies()` 或
示例的 `--cookie-only`），它不需要独占 User Data 目录。

> 说明：检测以**进程命令行**为准；`lockfile` / `SingletonLock` 在浏览器正常退出后
> 仍会残留，因此**单凭锁文件不会误报**。只有在"拿不到命令行"的受限环境里，
> 才会用「锁文件 + 同名浏览器进程」给出**疑似占用**的保守结论。

### 8.2 「无头模式」与登录态冲突

`headless=true` 与"复用同一个 User Data"互相冲突：无头实例无法附着到已有 Profile，
且会在退出时与图形实例争抢 Profile 锁，容易互相**覆盖 Cookie**。

- 复用登录态时请保持 `headless=false`
- 确实需要无头，请设置 `impersonate=true` 显式承担风险（模块会记 warning），
  或**复制一份 Profile 副本**再对副本跑无头

### 8.3 Cookie 导出为空 / 注入全部失败

| 现象 | 原因与解决 |
|---|---|
| 导出 0 条 | 该 Profile 没登录过目标站点；或浏览器还没访问过站点（Cookie 由页面 JS 写入）→ 先 `driver.get("https://<domain>/")` 再导出 |
| 导出 0 条 | `domain` 配置与实际 Cookie 归属域名不一致 → 用浏览器地址栏域名，不要带 `https://` 路径 |
| 注入失败 `InvalidCookieDomainException` | 已在模块内处理：注入前会先导航到对应域名；若目标域名族不匹配，请检查 `domain` |
| 注入失败且提示"已过期" | 服务端会话已失效 → 在浏览器里手动登录一次后重新导出 |
| 只想保留同站 Cookie | 设置 `cookie_accept: same-site`，避免把第三方跟踪 Cookie 带进自动化会话 |

### 8.4 Cookie 加密

```bash
# Windows
set CRAWLER_SESSION_KEY=你的口令
# Linux / macOS
export CRAWLER_SESSION_KEY=你的口令
python examples/browser_session_minimal.py --domain example.com --export-only
```

- 口令只用于**本地派生密钥**（SHA-256 → Fernet），不会上传到任何地方
- 未安装 `cryptography` 或未设置口令时：**降级为明文**，并在日志与 `store.notes`
  中明确标注"未加密"，绝不静默
- 口令丢失则文件**无法解密**（`CookieDecryptError`，提示密钥不正确）——请自行妥善保管

### 8.5 Selenium 报 `Unable to obtain driver`

Selenium 4.6+ 通过 Selenium Manager 自动下载驱动，**需要联网**。离线环境请：

1. 把 `chromedriver(.exe)` 放到 PATH 或浏览器内核同目录；或
2. **改用默认的 Playwright 驱动**（`launcher="playwright"`，无需额外驱动）；或
3. 为 Selenium Manager 配置代理 / 离线镜像

### 8.6 登录态检查误判 / 拿不到状态码

浏览器通道**读不到 HTTP 状态码**（`status=0`），此时 `login_status` 规则不生效，
模块会记 warning 并退回"页面非空"的弱判据。请改用更强的规则：

```python
{"domain": "example.com",
 "logout_selector": ".login-form",     # 未登录时该元素存在（最准）
 "login_selector": ".user-avatar",
 "logout_markers": ["请先登录", "登录已过期"]}
```

### 8.7 `User Data 目录不存在` / `Profile 不存在`

- 目录不存在：该浏览器从未启动过（目录由浏览器首次运行创建），或路径写错。
  错误消息会提示用 `CRAWLER_SESSION_USER_DATA_DIR` / `user_data_dir` 显式指定
- Profile 不存在：错误消息会**列出可用 Profile**（`Default`、`Profile 1` …），
  把 `profile` 改成其中之一即可

### 8.8 时区 / 地理位置与代理 IP 不匹配

"IP 在美国、时区是 Asia/Shanghai"本身就是很强的风控特征。使用代理时请一并设置：

```python
{"domain": "example.com",
 "proxy": {"server": "http://user:pass@residential:8080"},
 "timezone": "America/New_York",
 "geolocation": [40.7128, -74.0060]}
```

### 8.9 它会不会触发风控？

模块做的是**一致性**与**拟人化**（真实 Profile、真实 UA、随机停顿、鼠标轨迹、
限速），目的是降低被误判的概率，**不承诺绕过任何审核**。请把请求频率控制在
合理的礼貌范围内（`rate_limit` / `delay_mean`），并遵守目标站点的条款与
`robots.txt`。

---

## 9. 与框架其它部分的集成

### 9.1 抓取层复用

```python
handle = load_browser_session({"domain": "example.com", "autostart": True})
fetcher = handle.get_fetcher("static")     # core.fetcher.RequestsFetcher
ctx = fetcher.fetch("https://example.com/me")   # 已带登录态与一致指纹
```

`SessionHandle.fetch(url)` 与 `BaseFetcher.fetch()` 契约一致，因此可把句柄直接
当抓取器交给既有的提取/下载流水线使用。

### 9.2 模块注册与生命周期

```python
from core.browser_session import register_browser_session_module, lifecycle, registry

register_browser_session_module()      # 导入模块时已自动调用一次（重复注册返回 False）
hooks = lifecycle()                    # {"initialize", "health_check", "destroy"}

handle, result = hooks["initialize"]({"domain": "example.com"}, autostart=True)
hooks["health_check"](handle)          # 结构化健康检查
hooks["destroy"](handle)               # 关闭浏览器 + 释放连接池
```

只读自检（不启动浏览器、不写任何文件）：

```bash
python -m core.browser_session
```

### 9.3 日志

所有子模块统一通过 `utils.logging_setup.get_logger("core.browser_session.*")` 输出，
关键步骤均有打点：模块加载、浏览器启动、Cookie 导出/注入、登录态检查结果、
限速与反检测提示。若框架已配置根 logger，日志会照常上传；未配置时自带控制台输出。

---

## 10. 测试

```bash
# 单元测试（不需要浏览器、不访问网络）
python -m pytest tests/ -q
python -m unittest discover -s tests            # 也可以用标准库跑

# 集成测试（真实浏览器 / 真实 User Data；条件不满足会自动 skip）
python -m pytest tests/test_browser_session_integration.py -v

# 真正启动浏览器（默认关闭，避免开发机上意外弹窗）
set CS_INTEGRATION_LAUNCH=1            # Windows
export CS_INTEGRATION_LAUNCH=1         # Linux / macOS
python -m pytest tests/test_browser_session_integration.py -v
```

| 测试文件 | 覆盖对象 |
|---|---|
| `tests/test_browser_session_config.py` | 配置默认值、环境变量覆盖、路径探测、校验异常 |
| `tests/test_browser_session_cookie_store.py` | 规范化（含过期时间各格式）、加密/明文往返、加载与注入、requests 视图 |
| `tests/test_browser_session_launcher.py` | 占用检测（命令行/锁文件/进程名）、可执行文件解析、启动前置校验 |
| `tests/test_browser_session_login_state.py` | 判定规则优先级、三条通道、重登入口 |
| `tests/test_browser_session_bridge.py` | requests/驱动桥接、句柄接口、反检测与限速工具 |
| `tests/test_browser_session_integration.py` | 真实环境探测、**浏览器未关闭必须明确报错**、真实启动、端到端导出复用 |

集成测试可用的环境变量：

| 变量 | 作用 |
|---|---|
| `CS_INTEGRATION_BROWSER` | 指定浏览器（默认 `edge`） |
| `CS_INTEGRATION_USER_DATA_DIR` | 指定 User Data 目录 |
| `CS_INTEGRATION_LAUNCH=1` | 允许真正启动浏览器 |
| `CS_INTEGRATION_DOMAIN` | 探测目标站点（默认 `example.com`） |
