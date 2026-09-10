"""提取器注册表：插件发现、优先级调度与结果合并。

三层"通用化"在这里汇合（按 ``priority`` 从小到大执行）：
1. **站点适配器**（10）：处理签名、加密、私有接口等长尾 —— ``core/extractor/sites/``
2. **规则提取器**（50）：JSON 规则驱动，不改代码适配新站 —— 后续阶段补齐
3. **通用提取器**（60/80/90）：网络事件 / HTML / JSON，零配置覆盖大多数站点

同一 URL 被多个提取器命中时按"先命中者提供请求头，后命中者补齐合集与标题"合并，
使 *网络监听拿到的 Cookie/Referer* 与 *HTML 提取拿到的图集名* 互补。
"""

from __future__ import annotations

import importlib
import pkgutil

from core.extractor.base import BaseExtractor
from core.extractor.generic_html import GenericHTMLExtractor
from core.extractor.generic_json import GenericJSONExtractor
from core.extractor.network import NetworkExtractor
from core.extractor.rule_extractor import RuleExtractor
from core.media.models import MediaConfig, merge_items

#: 站点适配器插件包（放入 .py 文件即自动注册）
SITES_PACKAGE = "core.extractor.sites"

_plugin_classes: list = []
_plugins_loaded = False


# ---------------------------------------------------------------------------
# 注册与发现
# ---------------------------------------------------------------------------
def builtin_classes() -> list:
    """内置提取器类列表（规则层 + 通用层，按 priority 排序后使用）。"""
    return [RuleExtractor, GenericHTMLExtractor, GenericJSONExtractor,
            NetworkExtractor]


def register(extractor_cls) -> bool:
    """注册一个提取器类（重复注册或非法类返回 False）。"""
    if not (isinstance(extractor_cls, type)
            and issubclass(extractor_cls, BaseExtractor)):
        return False
    if extractor_cls in _plugin_classes:
        return False
    _plugin_classes.append(extractor_cls)
    return True


def register_many(classes) -> int:
    """批量注册，返回成功数量。"""
    return sum(1 for cls in classes or [] if register(cls))


def collect_extractor_classes(module) -> list:
    """从一个模块中收集提取器类（优先取模块级 ``EXTRACTORS`` 列表）。"""
    declared = getattr(module, "EXTRACTORS", None)
    if isinstance(declared, (list, tuple)):
        return [cls for cls in declared
                if isinstance(cls, type) and issubclass(cls, BaseExtractor)]
    found = []
    for value in vars(module).values():
        if (isinstance(value, type) and issubclass(value, BaseExtractor)
                and value is not BaseExtractor
                and value.__module__ == module.__name__):
            found.append(value)
    return found


def load_plugins(force: bool = False) -> list:
    """扫描 ``core/extractor/sites`` 自动加载站点适配器。

    新增站点只需在 ``sites/`` 下新增 ``.py`` 文件（可导出 ``EXTRACTORS = [...]``），
    无需修改任何既有代码。

    :return: 本次加载到的提取器类列表
    """
    global _plugins_loaded
    if _plugins_loaded and not force:
        return []
    found: list = []
    try:
        package = importlib.import_module(SITES_PACKAGE)
    except ImportError:
        _plugins_loaded = True
        return found
    for module_info in pkgutil.iter_modules(package.__path__):
        if module_info.name.startswith("_"):
            continue
        try:
            module = importlib.import_module(f"{SITES_PACKAGE}.{module_info.name}")
        except Exception:  # 单个插件导入失败不影响其它插件
            continue
        found.extend(collect_extractor_classes(module))
    register_many(found)
    _plugins_loaded = True
    return found


def plugin_classes() -> list:
    """当前已注册的插件提取器类（副本）。"""
    return list(_plugin_classes)


# ---------------------------------------------------------------------------
# 调度
# ---------------------------------------------------------------------------
def default_extractors(config: MediaConfig | None = None) -> list:
    """构建默认提取器实例列表（插件优先，按 priority 升序）。"""
    load_plugins()
    config = config or MediaConfig()
    classes = list(_plugin_classes) + builtin_classes()
    instances = []
    for cls in classes:
        try:
            instances.append(cls(config))
        except Exception:
            continue
    return sorted(instances, key=lambda extractor: extractor.priority)


def pick(url: str, ctx, config: MediaConfig | None = None, extractors=None) -> list:
    """返回能够处理本次抓取结果的提取器（按 priority 升序）。"""
    extractors = extractors if extractors is not None else default_extractors(config)
    picked = []
    for extractor in extractors:
        try:
            if extractor.can_handle(url, ctx) > 0:
                picked.append(extractor)
        except Exception:
            continue
    return picked


def extract_all(ctx, config: MediaConfig | None = None, extractors=None) -> list:
    """对一次抓取结果跑全部适用的提取器，返回去重合并后的媒体列表。

    单个提取器抛异常不会影响其它提取器（容错），保证"抓到一部分总比全失败好"。
    """
    config = config or MediaConfig()
    extractors = extractors if extractors is not None else default_extractors(config)
    merged: dict = {}
    for extractor in pick(ctx.base_url, ctx, config, extractors):
        try:
            items = list(extractor.extract(ctx))
        except Exception:
            continue
        for item in items:
            if item is None or not item.url:
                continue
            key = item.key()
            existing = merged.get(key)
            if existing is None:
                merged[key] = item
            else:
                merge_items(existing, item)
    return list(merged.values())


__all__ = [
    "SITES_PACKAGE", "builtin_classes", "register", "register_many",
    "collect_extractor_classes", "load_plugins", "plugin_classes",
    "default_extractors", "pick", "extract_all",
]
