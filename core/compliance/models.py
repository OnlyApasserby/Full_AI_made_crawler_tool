"""合规辅助筛查数据模型（纯标准库，可独立单测）。

定位（务必保持）
----------------
本模块产出的所有条目都是**待人工确认的线索**：既不表示"可以爬取"，
也不表示"不可以爬取"，更不构成法律意见。因此模型里刻意不设任何
"结论 / 判定 / 是否合法"字段，只保留：

    分类（category） + 原文证据（quote） + 置信度（confidence） + 人工复核提示

也就是"把可疑条款指出来，由人判断"，而不是"工具替人下结论"。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 分类法：需求指定的 6 类
# 可扩展——新增类别只需在此登记 + 在 keywords.py 补词表，流程代码无需改动
# ---------------------------------------------------------------------------
CATEGORY_CRAWLER = "crawler"
CATEGORY_AUTOMATION = "automation"
CATEGORY_DATA_COLLECTION = "data_collection"
CATEGORY_RATE_LIMIT = "rate_limit"
CATEGORY_API_USAGE = "api_usage"
CATEGORY_CIRCUMVENTION = "circumvention"

CATEGORY_ORDER = (
    CATEGORY_CRAWLER,
    CATEGORY_AUTOMATION,
    CATEGORY_DATA_COLLECTION,
    CATEGORY_RATE_LIMIT,
    CATEGORY_API_USAGE,
    CATEGORY_CIRCUMVENTION,
)

CATEGORY_LABELS = {
    CATEGORY_CRAWLER: "爬虫 / 机器人",
    CATEGORY_AUTOMATION: "自动化访问",
    CATEGORY_DATA_COLLECTION: "数据采集",
    CATEGORY_RATE_LIMIT: "请求频率",
    CATEGORY_API_USAGE: "API 使用",
    CATEGORY_CIRCUMVENTION: "绕过限制",
}

#: 每类命中的"人工复核提示"（只描述需要核对什么，不下结论）
CATEGORY_REVIEW_HINTS = {
    CATEGORY_CRAWLER: "请人工确认该条款对爬虫/机器人的约束范围是否覆盖你的访问方式。",
    CATEGORY_AUTOMATION: "请人工确认该条款对自动化/程序化访问的适用条件与例外情形。",
    CATEGORY_DATA_COLLECTION: "请人工确认该条款对数据采集、复制、转载或再利用的限制范围。",
    CATEGORY_RATE_LIMIT: "请人工确认该条款要求的请求频率/并发上限，并据此调整采集节奏。",
    CATEGORY_API_USAGE: "请人工确认是否存在官方 API、密钥或配额要求，以及是否有独立的接口条款。",
    CATEGORY_CIRCUMVENTION: "请人工确认该条款对绕过技术措施/访问限制的界定，并停止任何规避行为。",
}

#: 检测引擎标记
ENGINE_LOCAL = "local"
ENGINE_LOCAL_LLM = "local+llm"

#: 统一的人工复核标签（报告中不允许出现"可以爬/不可以爬"之类的肯定或否定结论）
REVIEW_NEEDED = "需人工确认"

DISCLAIMER_VERSION = "2026-09"


@dataclass
class LegalPage:
    """一个被扫描的公开法律/政策页面。"""

    url: str
    final_url: str = ""
    title: str = ""
    status: int = 0
    chars: int = 0
    fetched_at: str = ""
    error: str = ""
    matched_by: str = ""      # 发现来源：footer / sitemap / path-dict

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.chars)

    def to_dict(self) -> dict:
        return {
            "url": self.url, "final_url": self.final_url, "title": self.title,
            "status": self.status, "chars": self.chars,
            "fetched_at": self.fetched_at, "error": self.error,
            "matched_by": self.matched_by,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "LegalPage":
        data = dict(data or {})
        return cls(
            url=str(data.get("url") or ""),
            final_url=str(data.get("final_url") or ""),
            title=str(data.get("title") or ""),
            status=int(data.get("status") or 0),
            chars=int(data.get("chars") or 0),
            fetched_at=str(data.get("fetched_at") or ""),
            error=str(data.get("error") or ""),
            matched_by=str(data.get("matched_by") or ""),
        )


@dataclass
class Clause:
    """条款切分单元：一段（或一句）候选文本 + 在正文中的字符位置。

    ``text`` 永远来自**本地切分**，是报告中引文的唯一来源；
    LLM 不允许生成引文（只允许给标签与说明），以此杜绝编造原文。
    ``uid`` 用于与 LLM 往返对齐（``页面序号:条款序号``）。
    """

    text: str
    page_index: int = 0
    index: int = 0
    start: int = 0
    end: int = 0
    heading: str = ""
    page_url: str = ""

    @property
    def uid(self) -> str:
        return f"{self.page_index}:{self.index}"

    def to_dict(self) -> dict:
        return {
            "uid": self.uid, "text": self.text, "page_index": self.page_index,
            "index": self.index, "start": self.start, "end": self.end,
            "heading": self.heading, "page_url": self.page_url,
        }


@dataclass
class ClauseHit:
    """一条命中：把"哪类条款 + 原文证据 + 为什么可疑"结构化下来。

    注意 ``quote`` 是原文片段（未经改写），即使原文包含"允许/禁止"等字样
    也必须原样保留——改写证据等于伪造证据。
    """

    category: str
    quote: str
    matched_terms: list = field(default_factory=list)
    source_url: str = ""
    page_title: str = ""
    heading: str = ""
    uid: str = ""
    clause_index: int = 0
    char_offset: int = 0
    confidence: float = 0.0
    context_note: str = ""
    llm_label: str = ""
    llm_note: str = ""
    llm_applied: bool = False
    needs_human_review: bool = True
    review_hint: str = ""

    @property
    def category_label(self) -> str:
        return CATEGORY_LABELS.get(self.category, self.category)

    @property
    def confidence_text(self) -> str:
        """把置信度转成人类可读档位（避免展示"概率"被误读为结论把握）。"""
        if self.confidence >= 0.75:
            return "高"
        if self.confidence >= 0.5:
            return "中"
        return "低"

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "category_label": self.category_label,
            "matched_terms": list(self.matched_terms or []),
            "quote": self.quote,
            "source_url": self.source_url,
            "page_title": self.page_title,
            "heading": self.heading,
            "uid": self.uid,
            "clause_index": self.clause_index,
            "char_offset": self.char_offset,
            "confidence": round(float(self.confidence), 3),
            "confidence_text": self.confidence_text,
            "context_note": self.context_note,
            "llm_label": self.llm_label,
            "llm_note": self.llm_note,
            "llm_applied": bool(self.llm_applied),
            "needs_human_review": bool(self.needs_human_review),
            "review_hint": self.review_hint,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ClauseHit":
        data = dict(data or {})
        return cls(
            category=str(data.get("category") or ""),
            quote=str(data.get("quote") or ""),
            matched_terms=list(data.get("matched_terms") or []),
            source_url=str(data.get("source_url") or ""),
            page_title=str(data.get("page_title") or ""),
            heading=str(data.get("heading") or ""),
            uid=str(data.get("uid") or ""),
            clause_index=int(data.get("clause_index") or 0),
            char_offset=int(data.get("char_offset") or 0),
            confidence=float(data.get("confidence") or 0.0),
            context_note=str(data.get("context_note") or ""),
            llm_label=str(data.get("llm_label") or ""),
            llm_note=str(data.get("llm_note") or ""),
            llm_applied=bool(data.get("llm_applied")),
            needs_human_review=bool(data.get("needs_human_review", True)),
            review_hint=str(data.get("review_hint") or ""),
        )


@dataclass
class CategoryStat:
    """单个分类的命中统计。"""

    category: str
    count: int = 0

    @property
    def label(self) -> str:
        return CATEGORY_LABELS.get(self.category, self.category)

    def to_dict(self) -> dict:
        return {"category": self.category, "label": self.label, "count": self.count}

    @classmethod
    def from_dict(cls, data: dict) -> "CategoryStat":
        data = dict(data or {})
        return cls(category=str(data.get("category") or ""),
                   count=int(data.get("count") or 0))


@dataclass
class GuardStats:
    """红线机制的执行记录（被拦截的结论式表述次数）。"""

    intercepted: int = 0
    dropped_quotes: int = 0      # LLM 编造/失配的引文被丢弃次数
    notes: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"intercepted": int(self.intercepted),
                "dropped_quotes": int(self.dropped_quotes),
                "notes": list(self.notes or [])}

    @classmethod
    def from_dict(cls, data: dict) -> "GuardStats":
        data = dict(data or {})
        return cls(intercepted=int(data.get("intercepted") or 0),
                   dropped_quotes=int(data.get("dropped_quotes") or 0),
                   notes=list(data.get("notes") or []))


@dataclass
class ComplianceReport:
    """结构化合规辅助报告（可序列化为 JSON 入库/落盘）。"""

    start_url: str = ""
    site: str = ""
    scan_time: str = ""
    engine: str = ENGINE_LOCAL
    llm_status: str = ""
    report_path: str = ""        # 本次报告的留档位置（落盘后回填）
    disclaimer: str = ""
    disclaimer_version: str = DISCLAIMER_VERSION
    pages: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    findings: list = field(default_factory=list)
    stats: list = field(default_factory=list)
    robots_summary: dict = field(default_factory=dict)
    next_steps: list = field(default_factory=list)
    guard_stats: GuardStats = field(default_factory=GuardStats)

    # ---------- 便捷访问 ----------
    @property
    def total_hits(self) -> int:
        return len(self.findings or [])

    @property
    def pages_scanned(self) -> int:
        return len([p for p in (self.pages or []) if getattr(p, "ok", False)])

    def count_by_category(self, category: str) -> int:
        return len([h for h in (self.findings or []) if h.category == category])

    def findings_of(self, category: str) -> list:
        return [h for h in (self.findings or []) if h.category == category]

    def stat_rows(self) -> list:
        """按 :data:`CATEGORY_ORDER` 返回统计行（含 0 命中分类，便于看出"未发现"）。"""
        counts = {c: 0 for c in CATEGORY_ORDER}
        for hit in self.findings or []:
            counts[hit.category] = counts.get(hit.category, 0) + 1
        return [CategoryStat(category=c, count=counts.get(c, 0))
                for c in CATEGORY_ORDER]

    # ---------- 序列化 ----------
    def to_dict(self) -> dict:
        return {
            "report_version": "1.0",
            "site": self.site,
            "start_url": self.start_url,
            "scan_time": self.scan_time or time.strftime("%Y-%m-%d %H:%M:%S"),
            "engine": self.engine,
            "llm_status": self.llm_status,
            "report_path": self.report_path,
            "disclaimer": self.disclaimer,
            "disclaimer_version": self.disclaimer_version,
            "pages": [p.to_dict() for p in (self.pages or [])],
            "skipped": [dict(s) for s in (self.skipped or [])],
            "findings": [h.to_dict() for h in (self.findings or [])],
            "stats": [s.to_dict() for s in (self.stats or self.stat_rows())],
            "robots_summary": dict(self.robots_summary or {}),
            "next_steps": list(self.next_steps or []),
            "guard_stats": self.guard_stats.to_dict(),
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_dict(cls, data: dict) -> "ComplianceReport":
        data = dict(data or {})
        report = cls(
            start_url=str(data.get("start_url") or ""),
            site=str(data.get("site") or ""),
            scan_time=str(data.get("scan_time") or ""),
            engine=str(data.get("engine") or ENGINE_LOCAL),
            llm_status=str(data.get("llm_status") or ""),
            report_path=str(data.get("report_path") or ""),
            disclaimer=str(data.get("disclaimer") or ""),
            disclaimer_version=str(data.get("disclaimer_version") or DISCLAIMER_VERSION),
            pages=[LegalPage.from_dict(p) for p in (data.get("pages") or [])],
            skipped=[dict(s) for s in (data.get("skipped") or [])],
            findings=[ClauseHit.from_dict(h) for h in (data.get("findings") or [])],
            stats=[CategoryStat.from_dict(s) for s in (data.get("stats") or [])],
            robots_summary=dict(data.get("robots_summary") or {}),
            next_steps=list(data.get("next_steps") or []),
            guard_stats=GuardStats.from_dict(data.get("guard_stats") or {}),
        )
        return report


__all__ = [
    "CATEGORY_ORDER", "CATEGORY_LABELS", "CATEGORY_REVIEW_HINTS",
    "CATEGORY_CRAWLER", "CATEGORY_AUTOMATION", "CATEGORY_DATA_COLLECTION",
    "CATEGORY_RATE_LIMIT", "CATEGORY_API_USAGE", "CATEGORY_CIRCUMVENTION",
    "ENGINE_LOCAL", "ENGINE_LOCAL_LLM", "REVIEW_NEEDED", "DISCLAIMER_VERSION",
    "LegalPage", "Clause", "ClauseHit", "CategoryStat", "GuardStats",
    "ComplianceReport",
]
