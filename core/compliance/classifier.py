"""本地分类打分：把条款切分结果映射到 6 个关注类别（离线可用基线）。

打分模型（可解释、可调参，不做黑箱）：

    基础分 = Σ 强关键词命中 × 0.45
           + Σ 弱关键词命中 × 0.20
           + Σ 复合模式命中 × （0.45 ~ 0.70）
    语境调整 = × 1.18（含"禁止/不得"等禁止性表述）
             × 0.55（含"不限制/无需"等豁免性表述）
             × 0.85（含"允许/经同意"等许可性表述）
    最终置信度 = min(1.0, 调整后分值)，低于阈值不进报告

**只输出线索，不输出结论**：每个命中的分类、证据词、置信度都只是"值得人工看一眼"
的排序依据；语境调整也不会把"可能放宽"写成"允许抓取"，只会在
:attr:`ClauseHit.context_note` 里提示"需核对适用范围"。
"""

from __future__ import annotations

import config
from core.compliance.keywords import (
    CATEGORY_KEYWORDS, FALSE_POSITIVE_RULES, FALSE_POSITIVE_WINDOW,
    MAX_EVIDENCE_TERMS, NEGATION_HINTS, NEGATION_PENALTY, PATTERNS,
    PERMISSION_HINTS, PERMISSION_NOTE_PENALTY, PROHIBITION_BOOST,
    PROHIBITION_HINTS, STRONG_WEIGHT, WEAK_WEIGHT,
)
from core.compliance.models import (
    CATEGORY_ORDER, CATEGORY_REVIEW_HINTS, ClauseHit,
)

#: 语境说明文案（仅描述"需要核对什么"，不含任何结论性判断）
_NOTE_PROHIBITION = "条款含禁止性表述（如“不得”“严禁”），属于明确设限的线索。"
_NOTE_NEGATION = ("条款含豁免性表述（如“不限制”“无需”），限制强度可能低于字面观感，"
                  "请人工核对该表述的适用范围与前提条件。")
_NOTE_PERMISSION = ("条款含许可性表述（如“允许”“经同意”），可能是附条件的放宽，"
                    "请人工核对是否适用于你的访问方式以及需要满足哪些前提。")


# ---------------------------------------------------------------------------
# 关键词匹配（带误命中抑制）
# ---------------------------------------------------------------------------
def is_false_positive(lowered_text: str, index: int, length: int, term: str) -> bool:
    """判断某次命中是否属于"同词不同义"（如"机器人客服""API 文档""责任限制"）。

    判定窗口**包含命中文本本身**：像 ``api 文档`` 这种超串词条，其误命中标记
    （"文档"）就落在命中区间内部，若只看前后缀会漏判。
    """
    markers = FALSE_POSITIVE_RULES.get(term)
    if not markers:
        return False
    window = lowered_text[max(0, index - FALSE_POSITIVE_WINDOW):
                          index + length + FALSE_POSITIVE_WINDOW]
    return any(marker in window for marker in markers)


def _suppressed(lowered_text: str, index: int, length: int, needle: str) -> bool:
    """命中区间是否被误命中规则否定（超串词条按其中的规则键逐个判定）。"""
    keys = [key for key in FALSE_POSITIVE_RULES if key in needle]
    return any(is_false_positive(lowered_text, index, length, key) for key in keys)


def find_term(text: str, term: str) -> bool:
    """判断条款中是否命中该词（大小写不敏感 + 误命中抑制）。"""
    if not text or not term:
        return False
    lowered = text.lower()
    needle = term.lower()
    cursor = 0
    while True:
        index = lowered.find(needle, cursor)
        if index < 0:
            return False
        if not _suppressed(lowered, index, len(needle), needle):
            return True
        cursor = index + 1


def match_context(text: str) -> tuple:
    """返回 (禁止性, 豁免性, 许可性) 三个语境标记。"""
    lowered = (text or "").lower()
    prohibition = any(hint.lower() in lowered for hint in PROHIBITION_HINTS)
    negation = any(hint.lower() in lowered for hint in NEGATION_HINTS)
    permission = any(hint.lower() in lowered for hint in PERMISSION_HINTS)
    return prohibition, negation, permission


def context_note(prohibition: bool, negation: bool, permission: bool) -> str:
    """按语境生成"需人工确认"的说明（豁免/许可优先于禁止，说明更贴近事实）。"""
    if negation:
        return _NOTE_NEGATION
    if permission and not prohibition:
        return _NOTE_PERMISSION
    if prohibition:
        return _NOTE_PROHIBITION
    return ""


# ---------------------------------------------------------------------------
# 单条 / 批量打分
# ---------------------------------------------------------------------------
def score_clause(clause, *, min_confidence: float | None = None) -> list:
    """对单条条款打分，返回命中的 :class:`ClauseHit` 列表（可能多条、可能为空）。"""
    threshold = (config.COMPLIANCE_MIN_CONFIDENCE if min_confidence is None
                 else float(min_confidence))
    text = getattr(clause, "text", "") or ""
    if not text:
        return []

    prohibition, negation, permission = match_context(text)
    note = context_note(prohibition, negation, permission)

    results: list = []
    for category in CATEGORY_ORDER:
        weights = CATEGORY_KEYWORDS.get(category) or {}
        score = 0.0
        evidence: list = []

        for pattern, weight, label in PATTERNS.get(category, ()):
            if pattern.search(text):
                score += float(weight)
                evidence.append(label)

        for term in weights.get("strong", ()):
            if find_term(text, term):
                score += STRONG_WEIGHT
                evidence.append(term)

        for term in weights.get("weak", ()):
            if find_term(text, term):
                score += WEAK_WEIGHT
                evidence.append(term)

        if not evidence:
            continue

        score = min(1.0, score)
        if prohibition:
            score = min(1.0, score * PROHIBITION_BOOST)
        if negation:
            score *= NEGATION_PENALTY
        elif permission:
            score *= PERMISSION_NOTE_PENALTY

        if score < threshold:
            continue

        results.append(ClauseHit(
            category=category,
            quote=text,
            matched_terms=evidence[:MAX_EVIDENCE_TERMS],
            source_url=getattr(clause, "page_url", "") or "",
            heading=getattr(clause, "heading", "") or "",
            uid=getattr(clause, "uid", "") or "",
            clause_index=int(getattr(clause, "index", 0) or 0),
            char_offset=int(getattr(clause, "start", 0) or 0),
            confidence=round(score, 3),
            context_note=note,
            needs_human_review=True,
            review_hint=CATEGORY_REVIEW_HINTS.get(category, ""),
        ))
    return results


def classify_clauses(clauses, *, min_confidence: float | None = None,
                     max_findings: int | None = None, page_titles: dict | None = None) -> list:
    """对全部条款打分并整理成报告条目。

    :param page_titles: ``{页面URL: 标题}``，用于给命中补充来源页标题
    :return: :class:`ClauseHit` 列表（按置信度降序、同分按原文顺序）
    """
    limit = (config.COMPLIANCE_MAX_FINDINGS if max_findings is None
             else int(max_findings or 0))
    titles = page_titles or {}
    hits: list = []
    for clause in clauses or []:
        for hit in score_clause(clause, min_confidence=min_confidence):
            hit.page_title = titles.get(hit.source_url, "")
            hits.append(hit)
    hits.sort(key=lambda item: (-item.confidence, item.source_url, item.clause_index))
    if limit > 0:
        hits = hits[:limit]
    return hits


def select_candidates(hits, limit: int = 30) -> list:
    """挑出送 LLM 复核的候选条款（同一条款只送一次，取置信度最高者）。

    只发送候选条款文本片段，不发送整页 HTML——既省 token，也减少不必要的数据外发。
    """
    if limit <= 0:
        return []
    best: dict = {}
    for hit in hits or []:
        key = hit.uid or f"{hit.source_url}#{hit.clause_index}"
        current = best.get(key)
        if current is None or hit.confidence > current.confidence:
            best[key] = hit
    ordered = sorted(best.values(), key=lambda item: -item.confidence)
    return ordered[:limit]


def group_by_category(hits) -> dict:
    """按分类分组（保持 :data:`CATEGORY_ORDER` 顺序，含 0 命中的分类）。"""
    groups = {category: [] for category in CATEGORY_ORDER}
    for hit in hits or []:
        groups.setdefault(hit.category, []).append(hit)
    return groups


__all__ = [
    "is_false_positive", "find_term", "match_context", "context_note",
    "score_clause", "classify_clauses", "select_candidates", "group_by_category",
]
