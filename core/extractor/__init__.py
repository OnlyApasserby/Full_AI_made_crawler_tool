"""core.extractor —— 媒体提取器插件体系。

- ``base``：提取器契约 :class:`BaseExtractor`
- ``registry``：注册表（插件发现 / 优先级调度 / 结果合并）
- ``generic_html``：通用 HTML 提取（原图优先、srcset、CSS 背景图、OG 元信息、JS 内嵌）
- ``generic_json``：通用 JSON 提取（接口翻页/列表响应）
- ``network``：网络事件提取（浏览器 XHR / 图片 / 媒体请求）
- ``rule_extractor``：规则驱动提取（``resources/rules/*.json``，热加载）
- ``sites``：站点适配器插件目录（放入 .py 即自动注册）

对外主入口是 :func:`extract_media`：一次抓取结果进，去重合并后的
:class:`~core.media.models.MediaItem` 列表出。
"""

from __future__ import annotations

from core.extractor.base import BaseExtractor
from core.extractor.generic_html import GenericHTMLExtractor
from core.extractor.generic_json import GenericJSONExtractor
from core.extractor.network import NetworkExtractor
from core.extractor.registry import (
    SITES_PACKAGE, builtin_classes, collect_extractor_classes,
    default_extractors, extract_all, load_plugins, pick, plugin_classes,
    register, register_many,
)
from core.extractor.rule_extractor import (
    HAS_BS4, RuleExtractor, SiteRule, delete_rule, detail_links_for, get_rule,
    load_rules, matching_rules, page_rule_for, reload_rules, rules_dir,
    save_rule, select_values, validate_rule,
)
from core.media.models import MediaConfig

# 模块导入时即登记已存在的站点插件（新增插件文件后调用 load_plugins(force=True)）
load_plugins()


def extract_media(ctx, config: MediaConfig | None = None, extractors=None) -> list:
    """从一次抓取结果中提取媒体资源（去重合并后的 MediaItem 列表）。

    :param ctx: :class:`core.fetcher.base.FetchContext`
    :param config: 媒体抓取配置（影响缩略图猜解等提取行为）
    :param extractors: 可选，复用已构建的提取器实例（翻页场景避免重复构建）
    """
    return extract_all(ctx, config=config, extractors=extractors)


__all__ = [
    "BaseExtractor", "GenericHTMLExtractor", "GenericJSONExtractor",
    "NetworkExtractor", "RuleExtractor", "SiteRule", "SITES_PACKAGE",
    "builtin_classes", "collect_extractor_classes", "default_extractors",
    "extract_all", "load_plugins", "pick", "plugin_classes", "register",
    "register_many", "extract_media",
    # 规则管理（界面与脚本共用）
    "HAS_BS4", "rules_dir", "load_rules", "reload_rules", "get_rule",
    "save_rule", "delete_rule", "validate_rule", "matching_rules",
    "page_rule_for", "detail_links_for", "select_values",
]
