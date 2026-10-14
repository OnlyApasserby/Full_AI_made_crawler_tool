"""core.compliance —— 合规辅助筛查（robots.txt 校验的扩展模块）。

**定位（务必保持）**
本包只做"合规信息辅助筛查"：扫描目标站点**公开**的法律/政策页面，
把与爬虫、自动化访问、数据采集、请求频率、API 使用、绕过限制相关的条款
找出来，连同**原文证据**整理成结构化报告，供人工复核。

它**不提供法律意见、不代替律师、不自动得出"可以爬"或"不可以爬"的结论**，
其输出也不参与任何自动放行/阻断决策——robots.txt 仍是唯一的硬性闸门
（见 :mod:`core.robots` 与 ``ui/main_window.py`` 的既有流程）。

子模块
------
- ``models``     数据模型（分类法、命中等；刻意不含"结论"字段）
- ``keywords``   中英双语关键词与复合模式表（纯数据）
- ``guard``      红线机制：免责声明、结论式表述拦截、引文防幻觉校验
- ``discovery``  公开法律页面的发现（页脚链接 / sitemap / 内置路径词典）
- ``extract``    正文提取与条款切分（引文的唯一来源）
- ``classifier`` 本地分类打分（离线可用基线）
- ``llm``        chatanywhere（OpenAI 兼容）辅助精炼，失败自动降级
- ``report``     结构化报告构建与渲染（JSON / Markdown / HTML）
- ``scanner``    编排线程 ``ComplianceScanThread``（供 UI 后台调用）
"""

from __future__ import annotations

from core.compliance.guard import (
    DISCLAIMER, DISCLAIMER_VERSION, disclaimer_text, ensure_disclaimer,
    quote_in_source, sanitize_verdict,
)
from core.compliance.models import (
    CATEGORY_LABELS, CATEGORY_ORDER, ComplianceReport, ClauseHit, LegalPage,
)

__all__ = [
    "CATEGORY_ORDER", "CATEGORY_LABELS", "ComplianceReport", "ClauseHit",
    "LegalPage", "DISCLAIMER", "DISCLAIMER_VERSION", "disclaimer_text",
    "ensure_disclaimer", "sanitize_verdict", "quote_in_source",
]
