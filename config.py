"""配置管理：集中存放应用级常量、默认路径与默认参数。

其他模块不应散落魔法数字/路径，统一从这里读取；
本模块只依赖标准库，可被任意层引用。
"""

from __future__ import annotations

import os
import sys

# ---------------------------------------------------------------------------
# 应用信息
# ---------------------------------------------------------------------------
APP_NAME = "带递归爬取功能的图形化爬虫工具"
APP_VERSION = "2.1.0"

# 用于 robots.txt 匹配的爬虫标识
CRAWLER_USER_AGENT = "crawler-tool"

# ---------------------------------------------------------------------------
# 默认文件 / 目录
# ---------------------------------------------------------------------------
DEFAULT_DB_FILENAME = "crawler_cache.db"   # 主缓存数据库文件名
DEFAULT_DOWNLOAD_DIR = "downloads"          # 默认下载目录
DEFAULT_TASK_DATA_DIR = "task_data"         # 多任务管理器的数据根目录

# ---------------------------------------------------------------------------
# 运行模式：源码运行 vs PyInstaller 冻结（打包成 exe 后）
#
# 冻结时必须区分两类目录，否则会因"往只读的解压目录写数据"而崩溃：
# - BUNDLE_DIR：打包内容的**只读**目录（onefile 的 _MEIPASS / onedir 的 _internal）
# - ROOT_DIR ：**可写**的应用目录（onefile 取 exe 所在目录，onedir 取 exe 所在目录）
#   数据库、规则、浏览器内核、下载内容都落在这里，使程序可"绿色便携"运行
# ---------------------------------------------------------------------------
IS_FROZEN = bool(getattr(sys, "frozen", False))
BUNDLE_DIR = str(getattr(sys, "_MEIPASS", "") or "")
ROOT_DIR = (os.path.dirname(os.path.abspath(sys.executable)) if IS_FROZEN
            else os.path.dirname(os.path.abspath(__file__)))

# 可写资源目录（数据库 / 规则 / 浏览器内核 / 规则模板）
RESOURCES_DIR = os.path.join(ROOT_DIR, "resources")
#: 随包内置的只读资源目录（仅冻结时有效）——发布自带规则等默认内容
BUNDLED_RESOURCES_DIR = os.path.join(BUNDLE_DIR, "resources") if BUNDLE_DIR else ""

# 本地浏览器内核解压目录：resources/browsers/<chrome-win64>/chrome.exe
BROWSERS_DIR = os.path.join(RESOURCES_DIR, "browsers")
#: 打包内置的浏览器内核目录（BUNDLE_BROWSER=1 时才有内容）
BUNDLED_BROWSERS_DIR = os.path.join(BUNDLED_RESOURCES_DIR, "browsers") \
    if BUNDLED_RESOURCES_DIR else ""
# 允许放在应用目录的离线内核压缩包名（按顺序查找）
CHROME_ZIP_CANDIDATES = ("chrome-win64.zip", "chrome-win32.zip", "chrome.zip")

# 站点规则目录名（resources/rules/*.json，热加载，无需改代码即可适配站点）
RULES_DIRNAME = "rules"
#: 打包内置的只读规则目录（下划线开头文件同样视为模板，不参与加载）
BUNDLED_RULES_DIR = os.path.join(BUNDLED_RESOURCES_DIR, RULES_DIRNAME) \
    if BUNDLED_RESOURCES_DIR else ""


def ensure_resources_dir() -> str:
    """确保 resources 目录存在，返回其路径。"""
    os.makedirs(RESOURCES_DIR, exist_ok=True)
    return RESOURCES_DIR


def ensure_browsers_dir() -> str:
    """确保浏览器内核目录（resources/browsers）存在，返回其路径。"""
    path = os.path.join(ensure_resources_dir(), "browsers")
    os.makedirs(path, exist_ok=True)
    return path


def default_db_path() -> str:
    """默认主数据库路径：<包根>/resources/crawler_cache.db。"""
    return os.path.join(ensure_resources_dir(), DEFAULT_DB_FILENAME)


def ensure_rules_dir() -> str:
    """确保站点规则目录（resources/rules）存在，返回其路径。

    该目录下的 ``*.json`` 会被 ``core.extractor.rule_extractor`` 自动加载，
    用于在不改代码的前提下适配特定站点（选择器 / 正则 / 翻页规则）。
    """
    path = os.path.join(ensure_resources_dir(), RULES_DIRNAME)
    os.makedirs(path, exist_ok=True)
    return path


def ensure_compliance_dir() -> str:
    """确保合规报告目录（resources/compliance）存在，返回其路径。

    每次扫描的报告除入库外同时落盘为 JSON（便于留档与历史对比）。
    """
    path = os.path.join(ensure_resources_dir(), COMPLIANCE_DIRNAME)
    os.makedirs(path, exist_ok=True)
    return path


def ensure_sessions_dir() -> str:
    """确保登录态（Cookie）存储目录（resources/sessions）存在，返回其路径。

    该目录存放 ``core.browser_session`` 导出的 Cookie JSON / 加密文件；
    Cookie 等同账号凭据，**该目录不应提交到版本库**（见 .gitignore）。
    """
    path = os.path.join(ensure_resources_dir(), SESSIONS_DIRNAME)
    os.makedirs(path, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# 默认爬取 / 下载参数（UI 控件初始值来源）
# ---------------------------------------------------------------------------
DEFAULT_MAX_DEPTH = 1        # 递归深度
DEFAULT_REQUEST_DELAY = 0.5  # 请求间隔（秒）
DEFAULT_JITTER = 0.3         # 间隔随机抖动幅度
DEFAULT_MAX_PAGES = 0        # 自动停止页数上限（0=不限）
DEFAULT_MAX_CHARS = 0        # 单页最大字符数（0=不限）
DEFAULT_LINK_FILTER = ""     # 定向链接过滤关键词
DEFAULT_MAX_RETRIES = 2      # 爬取失败自动重试（任务级）
DEFAULT_CRAWL_EXTERNAL = False

DEFAULT_DOWNLOAD_RETRIES = 3      # 下载失败重试次数
DEFAULT_DOWNLOAD_WORKERS = 4      # 并发下载线程数
DEFAULT_SPEED_LIMIT = 0           # 下载限速 KB/s（0=不限）
DEFAULT_COMPRESS_QUALITY = 0      # 图片压缩质量（0=不压缩）
DEFAULT_DEDUP = True              # 图片内容去重
DEFAULT_MD5_CHECK = True          # MD5 校验

# ---------------------------------------------------------------------------
# 媒体抓取（图像/视频通用化）默认参数
# ---------------------------------------------------------------------------
DEFAULT_MEDIA_ENABLED = False          # 默认跟随原有递归爬取流程，按需开启
DEFAULT_MEDIA_KINDS = ["image", "video"]   # 目标资源类型
DEFAULT_MEDIA_RENDER_MODE = "auto"     # static / dynamic / auto（auto=静态失败再渲染）
DEFAULT_MEDIA_SCROLL = True            # 动态渲染时滚动触发懒加载
DEFAULT_MEDIA_MAX_SCROLL = 8           # 滚动次数上限
DEFAULT_MEDIA_WAIT_TIMEOUT = 15.0      # 等待选择器/网络空闲的超时（秒）
DEFAULT_MEDIA_FOLLOW_PAGINATION = True # 自动跟随图集/列表翻页
DEFAULT_MEDIA_MAX_PAGES = 5            # 翻页页数上限（0=不限）
DEFAULT_MEDIA_MAX_COUNT = 0            # 媒体条数上限（0=不限）
DEFAULT_MEDIA_MIN_WIDTH = 0            # 最小宽度过滤（0=不过滤）
DEFAULT_MEDIA_MIN_HEIGHT = 0           # 最小高度过滤（0=不过滤）
DEFAULT_MEDIA_MIN_SIZE = 0             # 最小体积过滤（KB，0=不过滤）
DEFAULT_MEDIA_DEDUP = True             # URL 归一化 + 内容去重
DEFAULT_MEDIA_DEDUP_PERCEPTUAL = True  # 感知哈希去重（需 Pillow，缺失自动跳过）
DEFAULT_MEDIA_ALLOW_SVG = False        # 是否保留 SVG（默认视为图标排除）
DEFAULT_MEDIA_MERGE_STREAM = False     # HLS/DASH 分片合并（可选功能，默认关闭）
DEFAULT_MEDIA_FILENAME_TEMPLATE = "{album}/{index:03d}_{title}{ext}"
DEFAULT_MEDIA_RENDER_WORKERS = 2       # 并发浏览器渲染任务上限（每实例约占150-250MB）
DEFAULT_MEDIA_PAGE_TIMEOUT = 30.0      # 单页抓取超时（秒）
DEFAULT_MEDIA_PAGE_DELAY = 0.5         # 动态模式翻页间隔（秒）

# 浏览器内核：优先使用本地离线包（项目根目录的 chrome-win64.zip），
# 找不到时才回退到 playwright 官方下载的内核
DEFAULT_BROWSER_AUTO_EXTRACT = True    # 首次渲染时自动解压根目录的离线内核包
# 也可用环境变量显式指定可执行文件（优先级最高）
BROWSER_EXECUTABLE_ENV = "CRAWLER_CHROME_PATH"

# ---------------------------------------------------------------------------
# 合规辅助筛查（robots.txt 校验的扩展；仅合规线索筛查，非法律意见）
#
# 扫描对象仅限目标站点的**公开**法律/政策页面；扫描自身也遵守 robots.txt，
# 并保持"一页一秒左右"的礼貌间隔。报告只作为人工复核线索，不参与任何
# 自动放行/阻断决策。
# ---------------------------------------------------------------------------
COMPLIANCE_DIRNAME = "compliance"        # 报告落盘目录：resources/compliance
COMPLIANCE_MAX_PAGES = 12                # 单次扫描的法律页面数上限
COMPLIANCE_SAME_HOST_ONLY = True         # 仅扫描同站点（同 host）页面
COMPLIANCE_PAGE_DELAY = 1.0              # 页面抓取间隔（秒），礼貌抓取
COMPLIANCE_PAGE_JITTER = 0.3             # 间隔随机抖动幅度
COMPLIANCE_PAGE_TIMEOUT = 15.0           # 单页抓取超时（秒）
COMPLIANCE_MAX_TEXT_CHARS = 200000       # 单页正文长度上限（防超大页面拖慢）
COMPLIANCE_MIN_CONFIDENCE = 0.35         # 低于该置信度的命中不进入报告
COMPLIANCE_MAX_FINDINGS = 200            # 报告条目数上限
COMPLIANCE_RESPECT_ROBOTS = True         # 扫描自身也遵守 robots.txt
COMPLIANCE_QUOTE_CHARS = 400             # 报告中单条引文的最大展示长度

# LLM 辅助检测（chatanywhere，OpenAI 兼容接口）
# 默认开启：一旦配置了 Key 即启用；未配置 Key / 调用失败时自动降级为纯本地检测。
# 只发送候选条款文本片段（默认 top 30），不发送整页 HTML。
LLM_ENABLED = True
LLM_BASE_URL = "https://api.chatanywhere.tech/v1"
LLM_MODEL = "gpt-4o-mini"
LLM_API_KEY_ENV = "CHATANYWHERE_API_KEY"   # 环境变量优先；界面输入仅存内存
LLM_TIMEOUT = 30.0
LLM_MAX_RETRIES = 2
LLM_MAX_CLAUSES = 30                     # 单次最多发送的候选条款数
LLM_MAX_CHARS_PER_CLAUSE = 600           # 单条条款发送长度上限
LLM_TEMPERATURE = 0.0

# ---------------------------------------------------------------------------
# 浏览器登录态复用模块（core/browser_session）默认值
#
# 用途：让抓取流程直接复用**使用者本人**本地浏览器已登录的会话（User Data /
# Cookie），从而跳过登录步骤并降低被风控误判的概率。
#
# 合规约束（模块只提供能力，不替使用者做判断）：
# - 仅用于复用使用者本人已登录的会话，用于合法授权场景
# - 不实现绕过验证码 / 破解加密 / 伪造他人身份等功能
# - 目标站点若明确禁止自动化访问，请使用者自行确认合规性后再使用
# ---------------------------------------------------------------------------
SESSIONS_DIRNAME = "sessions"              # Cookie 存储目录：resources/sessions
BROWSER_SESSION_ENV_PREFIX = "CRAWLER_SESSION_"   # 环境变量覆盖前缀

DEFAULT_SESSION_BROWSER = "chrome"         # chrome / edge（自动探测默认 User Data 目录）
DEFAULT_SESSION_PROFILE = "Default"        # Chromium 配置文件目录名
DEFAULT_SESSION_LAUNCHER = "playwright"    # playwright / selenium
#: 目标域名（探测登录态与 Cookie 分组用；留空则必须由调用方显式指定）
DEFAULT_SESSION_DOMAIN = ""
DEFAULT_SESSION_LOGIN_ENABLED = False      # 执行器模式是否启用登录态复用（默认关闭）
DEFAULT_SESSION_HEADLESS = False           # 复用 User Data 时不建议无头（见模块 README）
DEFAULT_SESSION_IMPERSONATE = False        # 是否使用真实浏览器指纹（复用已有 Profile）
DEFAULT_SESSION_COOKIE_STORE_PATH = ""     # 留空 = resources/sessions/<domain>.json
DEFAULT_SESSION_COOKIE_ENCRYPT = True      # Cookie 落盘加密（需 cryptography，缺失自动降级）
DEFAULT_SESSION_COOKIE_ACCEPT = "all"      # all / same-site（只保留目标域名及其父域的 Cookie）
DEFAULT_SESSION_COOKIE_KEY_ENV = "CRAWLER_SESSION_KEY"   # 加密密钥的环境变量名

DEFAULT_SESSION_CHECK_LOGIN = True         # 是否把「登录态有效性」作为启动前置检查
DEFAULT_SESSION_LOGIN_TIMEOUT = 15.0       # 登录态探测请求超时（秒）
DEFAULT_SESSION_LOGIN_MARKERS = []         # 命中任一关键字视为已登录
DEFAULT_SESSION_LOGOUT_MARKERS = []        # 命中任一关键字视为未登录（优先判断）
DEFAULT_SESSION_LOGIN_STATUS = [200]       # 视为「已登录」的状态码
DEFAULT_SESSION_LOGIN_SELECTOR = ""        # 已登录时该选择器应存在（浏览器层探测用）
DEFAULT_SESSION_LOGOUT_SELECTOR = ""       # 未登录时该选择器存在（浏览器层探测用）
DEFAULT_SESSION_LOGIN_URL = ""             # 留空 = https://<目标域名>/
DEFAULT_SESSION_TEST_URL = ""              # 留空 = 登录探测 URL
DEFAULT_SESSION_CHECK_TIMEOUT = 10.0       # 探测 URL 超时（秒）

DEFAULT_SESSION_ANTI_DETECT = True         # 注入反检测脚本（指纹一致性）
DEFAULT_SESSION_DELAY_MEAN = 0.0           # 行为拟人化：平均操作延迟（秒，0=关闭）
DEFAULT_SESSION_DELAY_JITTER = 0.5         # 平均延迟的抖动幅度（秒）
DEFAULT_SESSION_MIN_ACTION_INTERVAL = 0.0  # 同类操作最小间隔（秒，0=不限）
DEFAULT_SESSION_RATE_LIMIT = 0.0           # 每分钟请求上限（0=不限）
DEFAULT_SESSION_MOUSE_STEPS = 24           # 鼠标轨迹模拟的插值点数
DEFAULT_SESSION_COOKIE_ASSUME_VALID = True  # 纯 Cookie 模式下无规则可用时的兜底结论

__all__ = [
    "APP_NAME", "APP_VERSION", "CRAWLER_USER_AGENT",
    "DEFAULT_DB_FILENAME", "DEFAULT_DOWNLOAD_DIR", "DEFAULT_TASK_DATA_DIR",
    "ROOT_DIR", "RESOURCES_DIR", "BROWSERS_DIR", "CHROME_ZIP_CANDIDATES",
    "BROWSER_EXECUTABLE_ENV", "DEFAULT_BROWSER_AUTO_EXTRACT", "RULES_DIRNAME",
    "IS_FROZEN", "BUNDLE_DIR", "BUNDLED_RESOURCES_DIR", "BUNDLED_BROWSERS_DIR",
    "BUNDLED_RULES_DIR",
    "ensure_resources_dir", "ensure_browsers_dir", "ensure_rules_dir",
    "ensure_compliance_dir", "ensure_sessions_dir", "default_db_path",
    "DEFAULT_MAX_DEPTH", "DEFAULT_REQUEST_DELAY", "DEFAULT_JITTER",
    "DEFAULT_MAX_PAGES", "DEFAULT_MAX_CHARS", "DEFAULT_LINK_FILTER",
    "DEFAULT_MAX_RETRIES", "DEFAULT_CRAWL_EXTERNAL",
    "DEFAULT_DOWNLOAD_RETRIES", "DEFAULT_DOWNLOAD_WORKERS",
    "DEFAULT_SPEED_LIMIT", "DEFAULT_COMPRESS_QUALITY",
    "DEFAULT_DEDUP", "DEFAULT_MD5_CHECK",
    "DEFAULT_MEDIA_ENABLED", "DEFAULT_MEDIA_KINDS", "DEFAULT_MEDIA_RENDER_MODE",
    "DEFAULT_MEDIA_SCROLL", "DEFAULT_MEDIA_MAX_SCROLL", "DEFAULT_MEDIA_WAIT_TIMEOUT",
    "DEFAULT_MEDIA_FOLLOW_PAGINATION", "DEFAULT_MEDIA_MAX_PAGES",
    "DEFAULT_MEDIA_MAX_COUNT", "DEFAULT_MEDIA_MIN_WIDTH", "DEFAULT_MEDIA_MIN_HEIGHT",
    "DEFAULT_MEDIA_MIN_SIZE", "DEFAULT_MEDIA_DEDUP",
    "DEFAULT_MEDIA_DEDUP_PERCEPTUAL", "DEFAULT_MEDIA_ALLOW_SVG",
    "DEFAULT_MEDIA_MERGE_STREAM", "DEFAULT_MEDIA_FILENAME_TEMPLATE",
    "DEFAULT_MEDIA_RENDER_WORKERS", "DEFAULT_MEDIA_PAGE_TIMEOUT",
    "DEFAULT_MEDIA_PAGE_DELAY",
    # 合规辅助筛查
    "COMPLIANCE_DIRNAME", "COMPLIANCE_MAX_PAGES", "COMPLIANCE_SAME_HOST_ONLY",
    "COMPLIANCE_PAGE_DELAY", "COMPLIANCE_PAGE_JITTER", "COMPLIANCE_PAGE_TIMEOUT",
    "COMPLIANCE_MAX_TEXT_CHARS", "COMPLIANCE_MIN_CONFIDENCE",
    "COMPLIANCE_MAX_FINDINGS", "COMPLIANCE_RESPECT_ROBOTS",
    "COMPLIANCE_QUOTE_CHARS",
    # LLM 辅助检测
    "LLM_ENABLED", "LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY_ENV",
    "LLM_TIMEOUT", "LLM_MAX_RETRIES", "LLM_MAX_CLAUSES",
    "LLM_MAX_CHARS_PER_CLAUSE", "LLM_TEMPERATURE",
    # 浏览器登录态复用（core/browser_session）
    "SESSIONS_DIRNAME", "BROWSER_SESSION_ENV_PREFIX",
    "DEFAULT_SESSION_BROWSER", "DEFAULT_SESSION_PROFILE",
    "DEFAULT_SESSION_LAUNCHER", "DEFAULT_SESSION_DOMAIN",
    "DEFAULT_SESSION_LOGIN_ENABLED", "DEFAULT_SESSION_HEADLESS",
    "DEFAULT_SESSION_IMPERSONATE", "DEFAULT_SESSION_COOKIE_STORE_PATH",
    "DEFAULT_SESSION_COOKIE_ENCRYPT", "DEFAULT_SESSION_COOKIE_ACCEPT",
    "DEFAULT_SESSION_COOKIE_KEY_ENV",
    "DEFAULT_SESSION_CHECK_LOGIN", "DEFAULT_SESSION_LOGIN_TIMEOUT",
    "DEFAULT_SESSION_LOGIN_MARKERS", "DEFAULT_SESSION_LOGOUT_MARKERS",
    "DEFAULT_SESSION_LOGIN_STATUS", "DEFAULT_SESSION_LOGIN_SELECTOR",
    "DEFAULT_SESSION_LOGOUT_SELECTOR", "DEFAULT_SESSION_LOGIN_URL",
    "DEFAULT_SESSION_TEST_URL", "DEFAULT_SESSION_CHECK_TIMEOUT",
    "DEFAULT_SESSION_ANTI_DETECT", "DEFAULT_SESSION_DELAY_MEAN",
    "DEFAULT_SESSION_DELAY_JITTER", "DEFAULT_SESSION_MIN_ACTION_INTERVAL",
    "DEFAULT_SESSION_RATE_LIMIT", "DEFAULT_SESSION_MOUSE_STEPS",
    "DEFAULT_SESSION_COOKIE_ASSUME_VALID",
]
