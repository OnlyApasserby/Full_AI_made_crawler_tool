"""ModuleRegistry 接入层：按框架既有约定注册本模块并暴露生命周期钩子。

框架的模块化约定（与 :mod:`core.extractor.registry` 一致）
----------------------------------------------------------
1. 模块在自己的包内维护一份**模块描述**（名称 / 版本 / 能力 / 生命周期钩子）
2. 通过 ``register_*`` 函数向注册表登记；**重复注册返回 False** 而不报错
3. 框架侧（界面 / 自检 / 任务调度）只依赖"统一入口 + 生命周期钩子"，
   不依赖具体实现

本模块登记的内容
----------------
- 统一入口：:func:`core.browser_session.load_browser_session`
- 生命周期钩子：
  - ``initialize(config)``：装配驱动 / 注入脚本 / 注入 Cookie / 首次登录态检查
  - ``health_check()``：检查浏览器、User Data、驱动与登录态
  - ``destroy()``：关闭浏览器、释放 requests 连接池
- 供其它功能复用的模块级注册表（站点适配器等）：:func:`register_adapter` /
  :func:`adapter_classes`——与提取器插件体系的写法保持一致

为便于脚本与自检直接使用，这里也提供 ``python -m core.browser_session.registry``
形式的自检入口（只读探测，不启动浏览器、不写任何文件）。
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field

from core.browser_session.config import SessionConfig, detect_installed_browsers
from utils.logging_setup import get_logger

logger = get_logger("core.browser_session.registry")

#: 模块名（框架内的唯一标识）
MODULE_NAME = "browser_session"
#: 模块命名空间（配置键前缀，便于界面配置系统按前缀分组）
MODULE_NAMESPACE = "browser_session"
#: 模块提供的公开接口（供界面/自检展示能力清单）
MODULE_CORE_MODULES: tuple = ("config", "cookie_store", "launcher",
                              "session_bridge", "login_state", "anti_detect",
                              "registry")


@dataclass
class ModuleDescriptor:
    """模块描述（注册表登记的单元）。"""

    name: str = MODULE_NAME
    version: str = "1.0.0"
    namespace: str = MODULE_NAMESPACE
    summary: str = "浏览器登录态复用：User Data / Cookie 复用，免登录并降低风控误判"
    entry_point: str = "core.browser_session.load_browser_session"
    capabilities: tuple = (
        "load_local_user_data",      # 加载本地 Chrome / Edge User Data 启动即带登录态
        "cookie_export_import",      # Cookie 导出 / 加密持久化 / 注入
        "session_bridge",            # 登录态传给 Selenium / Playwright / requests
        "login_state_check",         # 统一登录态检查与失效重登入口
        "anti_detection",            # 指纹一致性与行为拟人化（不绕过任何审核）
    )
    submodules: tuple = MODULE_CORE_MODULES
    config_defaults: dict = field(default_factory=dict)
    hooks: tuple = ("initialize", "health_check", "destroy")
    notes: tuple = (
        "仅用于复用使用者本人已登录的会话；不实现验证码绕过 / 破解加密 / 伪造身份",
        "复用 User Data 时 Chromium 对目录独占，必须先完全退出浏览器",
    )

    def to_dict(self) -> dict:
        """转为普通字典（供界面展示 / 序列化）。"""
        return {
            "name": self.name, "version": self.version,
            "namespace": self.namespace, "summary": self.summary,
            "entry_point": self.entry_point,
            "capabilities": list(self.capabilities),
            "submodules": list(self.submodules),
            "config_defaults": dict(self.config_defaults),
            "hooks": list(self.hooks), "notes": list(self.notes),
        }

    def describe(self) -> str:
        """多行可读摘要。"""
        lines = [f"{self.name} v{self.version}（{self.namespace}）：{self.summary}",
                 f"入口：{self.entry_point}",
                 f"能力：{'、'.join(self.capabilities)}",
                 f"子模块：{'、'.join(self.submodules)}",
                 f"钩子：{'、'.join(self.hooks)}"]
        lines += [f"注意：{note}" for note in self.notes]
        return "\n".join(lines)


#: 本模块的注册表（模块名 → 描述）
MODULE_DESCRIPTORS: dict[str, ModuleDescriptor] = {}
#: 已注册的仓库适配器（供其它模块扩展"另一种浏览器 / 另一种请求层"）
_ADAPTER_CLASSES: list = []
#: 当前活动会话（供生命周期钩子统一管理；脚本场景可由调用方自己持有句柄）
_ACTIVE_HANDLES: list = []


def _config_defaults() -> dict:
    """从 :mod:`config` 读取本模块的默认值（保证与框架配置单一来源一致）。"""
    import config as app_config
    return {
        "browser": app_config.DEFAULT_SESSION_BROWSER,
        "profile": app_config.DEFAULT_SESSION_PROFILE,
        "launcher": app_config.DEFAULT_SESSION_LAUNCHER,
        "domain": app_config.DEFAULT_SESSION_DOMAIN,
        "cookie_encrypt": app_config.DEFAULT_SESSION_COOKIE_ENCRYPT,
        "cookie_accept": app_config.DEFAULT_SESSION_COOKIE_ACCEPT,
        "headless": app_config.DEFAULT_SESSION_HEADLESS,
        "check_login": app_config.DEFAULT_SESSION_CHECK_LOGIN,
        "anti_detect": app_config.DEFAULT_SESSION_ANTI_DETECT,
        "sessions_dir": app_config.SESSIONS_DIRNAME,
        "env_prefix": app_config.BROWSER_SESSION_ENV_PREFIX,
    }


def register_browser_session_module(descriptor: ModuleDescriptor | None = None) -> bool:
    """把本模块登记进注册表（重复注册返回 False）。

    同时尝试登记到框架公共注册表（若存在 ``core.registry`` / ``utils.registry``），
    这样"按框架现有注册机制被加载"这件事对本模块是**自动**的，不必改框架代码。
    """
    item = descriptor or ModuleDescriptor(config_defaults=_config_defaults())
    if item.name in MODULE_DESCRIPTORS:
        return False
    MODULE_DESCRIPTORS[item.name] = item
    _register_with_framework(item)
    logger.debug("已注册模块：%s v%s", item.name, item.version)
    return True


def _register_with_framework(descriptor: ModuleDescriptor) -> bool:
    """尽力向框架公共注册表登记（不存在该注册表时静默跳过）。"""
    for module_name in ("core.registry", "utils.registry", "manager.registry"):
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        for function_name in ("register_module", "register"):
            function = getattr(module, function_name, None)
            if callable(function):
                try:
                    function(descriptor)
                except Exception:  # 框架注册表签名不一致时不影响本模块
                    continue
                logger.debug("已通过 %s.%s 登记模块", module_name, function_name)
                return True
    return False


def get_descriptor(name: str = MODULE_NAME) -> ModuleDescriptor | None:
    """取模块描述（未注册时返回 None）。"""
    return MODULE_DESCRIPTORS.get(name)


def registered_modules() -> list:
    """返回已注册的模块名列表（副本）。"""
    return sorted(MODULE_DESCRIPTORS)


def register_adapter(adapter_cls) -> bool:
    """注册一个仓库适配器（重复注册或非法类返回 False）。

    与提取器插件体系一致：新增"另一种浏览器 / 另一种请求层"只需继承
    :class:`core.browser_session.adapters.BrowserDriver` 并调用本函数。
    """
    if not isinstance(adapter_cls, type):
        return False
    if adapter_cls in _ADAPTER_CLASSES:
        return False
    _ADAPTER_CLASSES.append(adapter_cls)
    return True


def adapter_classes() -> list:
    """当前已注册的仓库适配器类（副本）。"""
    return list(_ADAPTER_CLASSES)


# ---------------------------------------------------------------------------
# 生命周期钩子
# ---------------------------------------------------------------------------
def initialize(config: "SessionConfig | dict | None" = None, *,
               autostart: bool = True, cookie_path: str = "",
               check_login: bool | None = None, progress=None,
               handle=None):
    """生命周期钩子 · 初始化：装配会话并（可选）立即校验登录态。

    :param autostart: 是否立刻启动浏览器（界面"启动带登录态的浏览器"按钮用 True）
    :param check_login: 覆盖 ``config.check_login``：为 True 时启动后立刻检查登录态，
        未登录只记警告（不抛异常），并把结果写进返回值
    :param handle: 复用已有句柄而不新建（重复初始化时避免堆积活动会话）
    :return: ``(handle, check_result)``；未做检查时 ``check_result`` 为 None
    """
    from core.browser_session import load_browser_session
    logger.info("生命周期钩子：initialize（autostart=%s）", autostart)
    if handle is None:
        handle = load_browser_session(config, autostart=autostart,
                                      cookie_path=cookie_path, progress=progress)
    elif autostart:
        handle.ensure_driver()
    if not any(item is handle for item in _ACTIVE_HANDLES):
        _ACTIVE_HANDLES.append(handle)
    want_check = handle.config.check_login if check_login is None else bool(check_login)
    result = None
    if autostart and want_check:
        try:
            result = handle.check_login_detail()
            if not result.logged_in:
                logger.warning("登录态检查未通过：%s", result.reason)
                if progress:
                    progress(handle.relogin_hint())
        except Exception as exc:
            logger.warning("登录态检查过程出错（不影响会话本身）：%s", exc)
    return handle, result


def health_check(handle=None) -> dict:
    """生命周期钩子 · 健康检查：会话 + 环境 + 注册表状态。

    :param handle: 指定会话句柄；留空则汇总当前所有活动会话
    """
    if handle is not None:
        report = handle.health_check()
    else:
        report = {"sessions": [item.health_check() for item in active_handles()],
                  "count": len(active_handles())}
    report["module"] = {
        "name": MODULE_NAME, "registered": bool(MODULE_DESCRIPTORS),
        "version": (get_descriptor() or ModuleDescriptor()).version,
    }
    report["environment"] = {
        "installed_browsers": detect_installed_browsers(),
    }
    return report


def destroy(handle=None) -> int:
    """生命周期钩子 · 销毁：关闭浏览器、释放连接池、清空活动会话。

    :param handle: 只销毁指定会话；留空销毁全部
    :return: 实际关闭的会话数
    """
    targets = [handle] if handle is not None else active_handles()
    closed = 0
    for item in targets:
        try:
            item.close()
            closed += 1
        except Exception as exc:
            logger.warning("关闭会话失败（继续清理其它会话）：%s", exc)
        if item in _ACTIVE_HANDLES:
            _ACTIVE_HANDLES.remove(item)
    logger.info("生命周期钩子：destroy 已关闭 %d 个会话", closed)
    return closed


def active_handles() -> list:
    """当前由生命周期钩子托管的会话句柄列表（副本）。"""
    return [item for item in _ACTIVE_HANDLES]


def lifecycle() -> dict:
    """返回生命周期钩子的可调用对象（供界面/调度器统一接线）。"""
    return {"initialize": initialize, "health_check": health_check,
            "destroy": destroy}


def public_api() -> dict:
    """本模块的公开接口清单（名称 → 可调用对象 / 说明）。"""
    from core.browser_session import SessionHandle, load_browser_session
    return {
        "load_browser_session": load_browser_session,
        "session_from_cookies": importlib.import_module(
            "core.browser_session").session_from_cookies,
        "SessionHandle": SessionHandle,
        "SessionHandle.get_driver": SessionHandle.get_driver,
        "SessionHandle.get_requests_session": SessionHandle.get_requests_session,
        "SessionHandle.get_playwright_context": SessionHandle.get_playwright_context,
        "SessionHandle.check_login": SessionHandle.check_login,
        "SessionHandle.export_cookies": SessionHandle.export_cookies,
        "SessionHandle.load_cookies": SessionHandle.load_cookies,
        "SessionHandle.close": SessionHandle.close,
        "lifecycle": lifecycle,
    }


def selftest() -> int:
    """只读自检（不启动浏览器、不写文件），返回进程退出码。

    用法：``python -m core.browser_session.registry``
    """
    print("=== 浏览器登录态复用模块 · 自检 ===")
    descriptor = get_descriptor() or ModuleDescriptor(config_defaults=_config_defaults())
    print(descriptor.describe())
    print(f"注册表：{registered_modules() or '（未注册，将在首次导入时注册）'}")

    print("\n--- 环境探测（只读）---")
    installed = detect_installed_browsers()
    if installed:
        for kind, path in installed.items():
            print(f"[OK]   {kind} User Data：{path}")
    else:
        print("[WARN] 未探测到 Chrome / Edge 的默认 User Data 目录"
              "（可用 CRAWLER_SESSION_USER_DATA_DIR 显式指定）")

    for kind in ("chrome", "edge"):
        config = SessionConfig(browser=kind).finalize()
        state = "存在" if config.user_data_dir else "未找到"
        print(f"[{'OK' if config.user_data_dir else 'WARN'}]   {kind}：User Data {state}"
              f" → {config.user_data_dir or '（留空即为未安装）'}")

    try:
        from core.fetcher import chrome_setup
        found = chrome_setup.find_executable()
        print(f"[{'OK' if found else 'WARN'}]   浏览器内核：{found or '未找到'}")
    except Exception as exc:
        print(f"[WARN] 浏览器内核探测失败：{exc}")

    for name in ("selenium", "playwright"):
        try:
            importlib.import_module(name)
            print(f"[OK]   驱动库 {name}：可用")
        except ImportError:
            print(f"[WARN] 驱动库 {name}：未安装（pip install {name}）")

    from core.browser_session.cookie_store import crypto_available
    print(f"[{'OK' if crypto_available() else 'WARN'}]   Cookie 加密："
          + ("可用（cryptography 已安装）" if crypto_available()
             else "不可用（pip install cryptography，缺失时降级为明文存储）"))
    print("=== 自检结束 ===")
    return 0


# 模块导入即完成登记（与 core.extractor 的 load_plugins() 在导入时执行一致）
register_browser_session_module()


if __name__ == "__main__":  # pragma: no cover - 手动执行的入口
    import sys
    sys.exit(selftest())


__all__ = [
    "MODULE_NAME", "MODULE_NAMESPACE", "MODULE_CORE_MODULES",
    "MODULE_DESCRIPTORS", "ModuleDescriptor",
    "register_browser_session_module", "get_descriptor", "registered_modules",
    "register_adapter", "adapter_classes",
    "initialize", "health_check", "destroy", "active_handles", "lifecycle",
    "public_api", "selftest",
]
