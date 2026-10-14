"""浏览器登录态复用模块的异常体系。

设计约定（与 :mod:`core.fetcher.base` 的 ``FetchError`` / ``FetcherUnavailable``
保持一致的语义分层）：

- 所有本模块的异常都继承 :class:`BrowserSessionError`，调用方可以「一网打尽」
- **环境类**问题（依赖缺失、浏览器未安装、User Data 被占用、路径不存在）
  继承 :class:`SessionUnavailable`：这类问题应提示用户修环境，而不是重试
- **Cookie 类**问题继承 :class:`CookieError`：可降级（如改用 User Data 方案）

消息文本一律"可操作"：写清**发生了什么 + 怎么解决**，界面可直接展示。
"""

from __future__ import annotations


class BrowserSessionError(RuntimeError):
    """本模块所有异常的基类。"""


# ---------------------------------------------------------------------------
# 环境类：属于"环境没准备好"，调用方应提示用户处理而非重试
# ---------------------------------------------------------------------------
class SessionUnavailable(BrowserSessionError):
    """浏览器登录态能力不可用（依赖缺失 / 浏览器缺失 / User Data 被占用）。"""


class BrowserNotInstalled(SessionUnavailable):
    """未找到可用的 Chrome / Edge 可执行文件。"""


class UserDataDirNotFound(SessionUnavailable):
    """User Data 目录不存在（路径写错，或该浏览器从未启动过）。"""


class ProfileNotFound(SessionUnavailable):
    """User Data 目录下没有指定的 Profile（``--profile-directory``）。"""


class BrowserAlreadyRunning(SessionUnavailable):
    """目标 User Data 目录正被一个运行中的浏览器进程占用。

    Chromium 系的浏览器对同一个 ``--user-data-dir`` **独占**：此时再启动一个
    带同样参数的实例，要么静默把请求转交给已有实例（新进程立刻退出，自动化
    拿不到任何控制权），要么直接崩溃并报
    ``The profile appears to be in use by another process``。
    **必须先完全退出该浏览器（含后台驻留进程）再运行本模块。**
    """


class BrowserLaunchFailed(SessionUnavailable):
    """浏览器启动失败（参数不被支持、内核损坏、无显示环境等）。"""


class HeadlessNotSupported(BrowserLaunchFailed):
    """在复用 User Data 的前提下不支持无头模式。

    Chrome 的 ``--headless`` 与「复用同一个 User Data 目录」互斥：无头实例
    无法附着到已有 Profile，且会在退出时与图形实例争抢 Profile 锁。
    登录态复用场景**必须**使用有头模式（或复制一份 Profile 副本再跑无头）。
    """


class LauncherNotAvailable(SessionUnavailable):
    """未安装所选的自动化驱动库（selenium / playwright）。"""


# ---------------------------------------------------------------------------
# Cookie 类：可降级处理
# ---------------------------------------------------------------------------
class CookieError(BrowserSessionError):
    """Cookie 导出 / 持久化 / 加载 / 注入过程中的错误。"""


class CookieExportError(CookieError):
    """没有可导出的 Cookie（例如目标 Profile 未登录、域名不匹配）。

    常见原因：Cookie 尚未由页面 JS 写入（先去访问一次目标站点）、
    目标域名填错、或该 Profile 本来就没登录过该站点。
    """


class CookieFileNotFound(CookieError):
    """Cookie 存储文件不存在。"""


class CookieFileInvalid(CookieError):
    """Cookie 存储文件损坏、格式不识别或校验失败。"""


class CookieDecryptError(CookieError):
    """Cookie 文件解密失败（密钥错误或文件被篡改）。"""


class CookieInjectError(CookieError):
    """Cookie 注入浏览器失败（域名不匹配、字段非法、数量超限等）。"""


# ---------------------------------------------------------------------------
# 登录态类
# ---------------------------------------------------------------------------
class LoginStateError(BrowserSessionError):
    """登录态检查过程本身失败（网络错误、页面打不开等）。

    注意：与"检查成功但发现未登录"区分 —— 后者是 :meth:`check_login`
    返回 ``False``，不是异常。
    """


class NotLoggedIn(LoginStateError):
    """确认当前登录态已失效（需要使用者手动登录一次后重新导出 Cookie）。"""


__all__ = [
    "BrowserSessionError",
    "SessionUnavailable", "BrowserNotInstalled", "UserDataDirNotFound",
    "ProfileNotFound", "BrowserAlreadyRunning", "BrowserLaunchFailed",
    "HeadlessNotSupported", "LauncherNotAvailable",
    "CookieError", "CookieExportError", "CookieFileNotFound", "CookieFileInvalid",
    "CookieDecryptError", "CookieInjectError",
    "LoginStateError", "NotLoggedIn",
]
