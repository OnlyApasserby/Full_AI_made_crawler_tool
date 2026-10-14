"""关键词与模式表：中英双语的条款线索词库（纯数据，无逻辑）。

设计要点
--------
- **强弱分级**：强词（strong）单独出现即可作为线索；弱词（weak）单独出现
  噪音过大（如"程序""限制""下载"），只作为加权项，不单独成条。
- **复合模式**（:data:`PATTERNS`）：真正有价值的线索通常成句出现，
  例如"禁止使用任何自动化程序访问"——比单个关键词可靠得多，因此给更高权重。
- **误命中抑制**（:data:`FALSE_POSITIVE_RULES`）：词表中不少词在别的语境下
  有完全不同的含义（"机器人客服""API 文档""责任限制""代理商"），
  命中时检查邻近窗口，属于其他语义则丢弃该次命中。

词表按分类组织，新增分类只需在 :data:`CATEGORY_KEYWORDS` 与
:data:`PATTERNS` 里补条目，流程代码无需改动。
"""

from __future__ import annotations

import re

from core.compliance.models import (
    CATEGORY_API_USAGE, CATEGORY_AUTOMATION, CATEGORY_CIRCUMVENTION,
    CATEGORY_CRAWLER, CATEGORY_DATA_COLLECTION, CATEGORY_RATE_LIMIT,
)

# ---------------------------------------------------------------------------
# 关键词（中 / 英）
# ---------------------------------------------------------------------------
CATEGORY_KEYWORDS: dict[str, dict[str, tuple]] = {
    CATEGORY_CRAWLER: {
        "strong": (
            "网络爬虫", "爬虫程序", "爬虫软件", "爬虫技术", "爬虫工具", "爬虫协议",
            "蜘蛛程序", "网络蜘蛛", "robots.txt", "机器人排除协议",
            "web crawler", "webcrawler", "crawler", "spider", "spidering",
            "robot crawler", "internet bot", "web robot", "crawl the site",
        ),
        "weak": (
            "爬虫", "爬取", "爬站", "机器人", "无人值守", "bot", "robot",
        ),
    },
    CATEGORY_AUTOMATION: {
        "strong": (
            "自动化访问", "自动化程序", "自动化工具", "自动化脚本", "自动化方式",
            "批量访问", "程序化访问", "机器访问", "自动查询", "自动抓取",
            "automated access", "automated means", "automated tool",
            "automated script", "automated queries", "programmatic access",
            "systematic access", "scripted access", "robotic process",
        ),
        "weak": (
            "自动化", "自动", "批量", "脚本", "程序", "定时任务", "模拟请求",
            "automated", "automation", "script", "batch", "scheduled",
        ),
    },
    CATEGORY_DATA_COLLECTION: {
        "strong": (
            "数据采集", "数据抓取", "数据爬取", "采集数据", "抓取数据", "内容采集",
            "数据挖掘", "批量下载", "批量复制", "数据提取", "数据导出", "镜像站",
            "未经授权采集", "内容转载", "抄袭",
            "data collection", "data harvesting", "data mining", "data scraping",
            "scrape", "scraping", "harvest", "harvesting", "extract data",
            "bulk download", "bulk copy", "reproduce", "republish", "mirror",
        ),
        "weak": (
            "采集", "抓取", "转载", "复制", "引用", "导出", "下载", "提取",
            "extract", "export", "copy", "download", "collect",
        ),
    },
    CATEGORY_RATE_LIMIT: {
        "strong": (
            "请求频率", "访问频率", "访问频次", "请求次数", "访问次数", "并发请求",
            "并发连接", "请求速率", "速率限制", "频率限制", "请求配额", "限速",
            "节流", "单位时间内", "每秒请求", "合理频率", "适度访问", "过度访问",
            "异常流量", "服务器负载", "服务器负担",
            "rate limit", "rate limiting", "request rate", "request frequency",
            "throttle", "throttling", "concurrent request", "concurrency",
            "requests per second", "queries per second", "excessive load",
            "server load", "reasonable frequency", "bandwidth",
        ),
        "weak": (
            "频率", "并发", "间隔", "负载", "过度", "上限", "节流",
            "frequency", "concurrent", "interval", "load", "excessive", "limit",
        ),
    },
    CATEGORY_API_USAGE: {
        "strong": (
            "应用程序接口", "api 接口", "api接口", "api 密钥", "api密钥", "api 文档",
            "开放平台", "开发者接口", "接口调用", "调用次数", "调用限额", "接口配额",
            "接口权限", "开发者密钥", "开发者协议", "接口使用条款", "访问令牌",
            "api access", "application programming interface", "api endpoint",
            "api key", "api token", "api quota", "api terms", "developer agreement",
            "access token", "oauth",
        ),
        "weak": (
            "api", "接口", "密钥", "令牌", "配额", "调用", "开放能力",
            "token", "key", "secret", "quota", "endpoint", "sdk",
        ),
    },
    CATEGORY_CIRCUMVENTION: {
        "strong": (
            "绕过限制", "规避限制", "破解", "反爬", "反爬虫", "逆向工程", "反编译",
            "技术措施", "访问限制", "安全措施", "爬虫对抗", "验证码", "抓包",
            "伪造", "冒充", "伪装", "规避技术", "规避访问控制",
            "circumvent", "circumvention", "bypass", "bypassing", "evade",
            "evasion", "reverse engineer", "reverse engineering", "defeat",
            "technical measures", "anti-bot", "anti-crawler", "captcha",
            "spoof", "forge", "impersonate", "unauthorized access",
        ),
        "weak": (
            "限制措施", "技术手段", "屏蔽", "拦截", "验证", "规避", "绕过",
            "restriction", "measure", "block", "intercept", "verify",
        ),
    },
}

#: 强 / 弱关键词的权重
STRONG_WEIGHT = 0.45
WEAK_WEIGHT = 0.20

# ---------------------------------------------------------------------------
# 复合模式：权重比单词更高（成句出现，误报率低）
# 元组结构：(编译后的正则, 权重, 展示用词条标签)
# ---------------------------------------------------------------------------
PATTERNS: dict[str, tuple] = {
    CATEGORY_CRAWLER: (
        # 禁止性表述+爬取动作（双向都要覆盖："禁止爬取" 与 "不得使用自动化程序抓取"）
        (re.compile(r"(?:禁止|不得|严禁|不允许|谢绝)[^。；;\n]{0,24}"
                    r"(?:爬虫|爬取|抓取|蜘蛛|crawler|spider)", re.I),
         0.60, "禁止/不得…爬虫"),
        (re.compile(r"(?:爬虫|爬取|抓取|蜘蛛|crawler|spider)[^。；;\n]{0,24}"
                    r"(?:禁止|不得|严禁|不得使用)", re.I),
         0.55, "爬虫…不得使用"),
        (re.compile(r"(?i)\b(?:no|not)\s+(?:crawler|spider|robot)s?\b"), 0.50, "no crawler"),
    ),
    CATEGORY_AUTOMATION: (
        (re.compile(r"(?:禁止|不得|严禁|不允许)[^。；;\n]{0,24}(?:自动化|自动|批量|脚本|程序)", re.I),
         0.55, "禁止…自动化"),
        (re.compile(r"(?:自动化|脚本|程序)[^。；;\n]{0,20}(?:禁止|不得|严禁)", re.I),
         0.50, "自动化…禁止"),
        (re.compile(r"(?i)\bno\s+(?:automated|robotic|scripted)\s+"
                    r"(?:access|queries|means|tools?)"), 0.60, "no automated access"),
    ),
    CATEGORY_DATA_COLLECTION: (
        (re.compile(r"(?:未经|未获|事先未)[^。；;\n]{0,16}(?:授权|许可|同意)"
                    r"[^。；;\n]{0,24}(?:采集|抓取|复制|转载|下载|使用)", re.I),
         0.65, "未经授权…采集"),
        (re.compile(r"(?:禁止|不得|严禁)[^。；;\n]{0,24}(?:采集|抓取|复制|转载|镜像|批量下载|挖掘)", re.I),
         0.60, "禁止…采集"),
        (re.compile(r"(?i)\bwithout\s+(?:prior\s+)?(?:written\s+)?"
                    r"(?:consent|permission|authorization)"), 0.55, "without prior consent"),
    ),
    CATEGORY_RATE_LIMIT: (
        (re.compile(r"(?:每秒|每分钟|每小时|单位时间|单日|每日|每月)[^。；;\n]{0,18}"
                    r"(?:次|个|条|请求|访问|调用)"), 0.55, "单位时间…次数"),
        (re.compile(r"(?:不超过|不得超过|上限为|限制为|控制在|限制在)[^。；;\n]{0,18}"
                    r"(?:次|qps|并发|请求|连接)"), 0.55, "不超过…次"),
        (re.compile(r"(?:合理|适度|正常|适当)[^。；;\n]{0,12}(?:频率|间隔|范围|访问)", re.I),
         0.45, "合理…频率"),
        (re.compile(r"(?i)\b(?:rate|request)\s+limit"), 0.50, "rate limit"),
        (re.compile(r"(?i)\breasonable\s+(?:rate|frequency|volume)"), 0.45, "reasonable rate"),
    ),
    CATEGORY_API_USAGE: (
        (re.compile(r"(?:仅|只|必须|应当)[^。；;\n]{0,12}(?:通过|经由|使用)[^。；;\n]{0,12}"
                    r"(?:api|接口|开放平台)", re.I), 0.55, "仅通过 API"),
        (re.compile(r"(?i)\b(?:api|接口)[^。；;\n]{0,16}(?:密钥|配额|限额|调用次数|频率限制|令牌)",
                    re.I), 0.55, "API 密钥/配额"),
        (re.compile(r"(?i)\bapi\s+(?:key|token|quota|rate|access|terms)"), 0.50, "api key/quota"),
    ),
    CATEGORY_CIRCUMVENTION: (
        (re.compile(r"(?:绕过|规避|破解|反编译|逆向)[^。；;\n]{0,16}"
                    r"(?:技术措施|限制|验证码|反爬|防护|机制|措施)", re.I),
         0.70, "绕过…技术措施"),
        (re.compile(r"(?:不得|禁止|严禁)[^。；;\n]{0,16}(?:绕过|规避|破解|反编译|规避)", re.I),
         0.60, "禁止…绕过"),
        (re.compile(r"(?i)\bcircumvent\s+(?:any\s+)?(?:technical|security|access|protection)"),
         0.70, "circumvent … measures"),
        (re.compile(r"(?i)\b(?:reverse\s+engineer|decompile|disassemble)\b"), 0.60, "reverse engineer"),
    ),
}

# ---------------------------------------------------------------------------
# 语境提示词：用于调整置信度并生成"需人工确认"的说明
# ---------------------------------------------------------------------------
#: 禁止性表述 → 提高置信度（条款确实在设限）
PROHIBITION_HINTS = (
    "禁止", "不得", "严禁", "不允许", "不准", "谢绝", "拒绝", "严禁使用",
    "应当经", "需经", "须经", "必须获得", "未经授权", "未获许可",
    "prohibit", "prohibited", "must not", "shall not", "may not",
    "not permitted", "forbidden", "no unauthorized", "not allowed",
)
#: 许可性表述 → 降低置信度并提示适用范围需核对（注意：这只是"可能放宽"，不是结论）
PERMISSION_HINTS = (
    "允许", "许可", "同意", "授权", "可以", "无需事先", "无需获得",
    "permitted", "allowed", "may crawl", "with consent", "granted",
)
#: 否定/豁免性表述 → 明显降低置信度（如"本公司不限制正常频率访问"）
NEGATION_HINTS = (
    "不限制", "不禁止", "无需", "无须", "不需要", "不必", "不要求", "不加以限制",
    "no restriction", "not require", "without limitation",
)

#: 语境调整系数
PROHIBITION_BOOST = 1.18
NEGATION_PENALTY = 0.55
PERMISSION_NOTE_PENALTY = 0.85

# ---------------------------------------------------------------------------
# 误命中抑制
# ---------------------------------------------------------------------------
#: 命中词 -> 邻近窗口内出现这些标记时视为其他语义，丢弃该次命中
FALSE_POSITIVE_RULES: dict[str, tuple] = {
    "机器人": ("客服", "智能", "聊天", "问答", "在线", "chatbot", "ai "),
    "robot": ("chatbot", "customer"),
    "bot": ("chatbot", "customer service"),
    "api": ("文档", "手册", "示例", "教程", "说明", "doc", "reference",
            "guide", "文档中心", "帮助中心"),
    "接口": ("文档", "手册", "示例", "教程", "说明"),
    "脚本": ("javascript", "css", "脚本文件", "示例脚本", "禁用脚本", "脚本错误"),
    "代理": ("代理商", "代理服务", "代理人", "代理协议", "代理销售"),
    "下载": ("客户端", "应用", "app", "插件", "浏览器"),
    "限制": ("责任", "担保", "赔偿"),
    "复制": ("复制链接", "剪贴板", "复制本页"),
    "secret": ("secrets management",),
}

#: 误命中判定时查看的邻近字符窗口
FALSE_POSITIVE_WINDOW = 14

#: 条款切分参数
MIN_CLAUSE_CHARS = 8
MAX_CLAUSE_CHARS = 1500

#: 单条命中最多保留的证据词数量
MAX_EVIDENCE_TERMS = 6


def category_terms(category: str) -> tuple:
    """返回某分类的全部关键词（强 + 弱）。"""
    entry = CATEGORY_KEYWORDS.get(category) or {}
    return tuple(entry.get("strong", ())) + tuple(entry.get("weak", ()))


__all__ = [
    "CATEGORY_KEYWORDS", "PATTERNS", "STRONG_WEIGHT", "WEAK_WEIGHT",
    "PROHIBITION_HINTS", "PERMISSION_HINTS", "NEGATION_HINTS",
    "PROHIBITION_BOOST", "NEGATION_PENALTY", "PERMISSION_NOTE_PENALTY",
    "FALSE_POSITIVE_RULES", "FALSE_POSITIVE_WINDOW",
    "MIN_CLAUSE_CHARS", "MAX_CLAUSE_CHARS", "MAX_EVIDENCE_TERMS",
    "category_terms",
]
