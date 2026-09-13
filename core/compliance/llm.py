"""LLM 辅助标注（chatanywhere，OpenAI 兼容接口）——可选增强，失败自动降级。

职责边界（很窄，这是刻意的）
--------------------------
LLM **只做两件事**：给候选条款打一个更贴切的分类标签、写一句"需要注意什么"的说明。
它不做、也不被允许做：

- 输出法律结论（"可以抓取""非法"…）——提示词禁止 + :func:`guard.sanitize_verdict` 二次拦截
- 生成引文 —— 报告的引文一律取本地切分原文；模型返回的引文只用于**校验**，
  校验不通过（无法在源文本中逐字匹配）就丢弃并计入 ``dropped_quotes``
- 决定报告结构 —— 分类只剩固定 6 项，"可能不相关"也只降权不删条

数据外发范围：**只发送候选条款文本片段**（默认置信度最高的 30 条、每条截断到
600 字符），不发送整页 HTML、不发送 Cookie/请求头。API Key 只从环境变量或
内存读取，不落盘、不入库、不进报告。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

import requests

import config
from core.compliance import guard
from core.compliance.models import (
    CATEGORY_LABELS, CATEGORY_ORDER, ClauseHit,
)

#: 提示词：硬约束写在最前面，明确"不下结论、不编原文、只输出 JSON"
TRIAGE_SYSTEM_PROMPT = """你是"合规信息辅助筛查"工具中的文本标注助手，只负责整理条款线索供人工复核。

硬性约束（必须无条件遵守）：
1. 只做"归类 + 要点标注"。绝对不得输出任何法律结论或判断，例如"可以抓取""不可以抓取"
   "合法""违法""允许爬取""禁止爬取""建议抓取"等表述一律禁止出现。
2. 不得编造原文。quote 字段必须逐字复制输入中的句子，不得改写、拼接或补全。
3. 不确定的一律按"需人工确认"处理，不要给出倾向性意见。
4. 你的输出只是检索与排序用的线索，不构成法律意见，也不代替律师。
5. 只输出 JSON 对象，不要输出解释、前后缀或 Markdown 代码块以外的任何文字。

分类代码（category 只能取以下值之一，无法归类时用 none）：
%s

输出 JSON 结构（items 数组，每条对应一个输入条款）：
{"items":[{"uid":"输入的uid","category":"分类代码或none","label":"不超过20字的要点标签",
"note":"不超过80字的说明，写清需要人工核对什么","relevant":true,"quote":"逐字来自输入的句子"}]}""" % "\n".join(
    f"- {code}：{CATEGORY_LABELS[code]}" for code in CATEGORY_ORDER)

#: 单次请求的 payload 字符预算（超过则分片发送）
REQUEST_CHAR_BUDGET = 12000


@dataclass
class LLMConfig:
    """LLM 调用配置（API Key 只存内存，环境变量优先）。"""

    enabled: bool = True
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    timeout: float = 0.0
    max_retries: int = 0
    max_clauses: int = 0
    max_chars_per_clause: int = 0
    temperature: float = 0.0

    @classmethod
    def resolve(cls, api_key: str = "", enabled: bool | None = None) -> "LLMConfig":
        """按"环境变量优先，界面输入为辅"的规则组装配置。

        :param api_key: 界面临时输入的 Key（仅存内存，不落盘）
        """
        import os

        env_key = os.environ.get(config.LLM_API_KEY_ENV, "").strip()
        return cls(
            enabled=config.LLM_ENABLED if enabled is None else bool(enabled),
            base_url=(config.LLM_BASE_URL or "").rstrip("/"),
            model=config.LLM_MODEL,
            api_key=env_key or (api_key or "").strip(),
            timeout=float(config.LLM_TIMEOUT),
            max_retries=int(config.LLM_MAX_RETRIES),
            max_clauses=int(config.LLM_MAX_CLAUSES),
            max_chars_per_clause=int(config.LLM_MAX_CHARS_PER_CLAUSE),
            temperature=float(config.LLM_TEMPERATURE),
        )

    @property
    def key_source(self) -> str:
        """Key 的来源说明（用于界面提示，不暴露 Key 本身）。"""
        import os
        return ("环境变量" if os.environ.get(config.LLM_API_KEY_ENV, "").strip()
                else ("界面输入（仅本次会话）" if self.api_key else "未配置"))

    def available(self) -> tuple:
        """是否具备调用条件；不可用时返回可读原因。"""
        if not self.enabled:
            return False, "LLM 辅助检测已关闭，本次为纯本地关键词检测"
        if not self.api_key:
            return False, (f"未配置 API Key（环境变量 {config.LLM_API_KEY_ENV} "
                           "或界面输入），本次为纯本地关键词检测")
        if not self.base_url or not self.model:
            return False, "LLM 接口地址或模型名为空，本次为纯本地关键词检测"
        return True, ""


@dataclass
class Refinement:
    """LLM 对单条条款的标注结果（不含引文，引文一律用本地原文）。"""

    uid: str
    category: str = ""
    label: str = ""
    note: str = ""
    relevant: bool = True
    extra: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------
class LLMClient:
    """chatanywhere（OpenAI 兼容）辅助标注客户端。

    任何异常都不向上抛：调用失败即返回空结果并给出降级原因，
    保证"没有 LLM 也能出报告"。
    """

    def __init__(self, llm_config: LLMConfig | None = None):
        self.config = llm_config or LLMConfig.resolve()

    # ---------- 可用性 ----------
    def available(self) -> tuple:
        return self.config.available()

    # ---------- 主入口 ----------
    def refine(self, candidates, log=None) -> tuple:
        """对候选条款做辅助标注。

        :param candidates: :class:`~core.compliance.models.ClauseHit` 列表
            （由 ``classifier.select_candidates`` 挑出，同一条款只出现一次）
        :return: (``{uid: Refinement}``, 状态说明)
        """
        logger = log or (lambda _msg: None)
        ok, reason = self.available()
        if not ok:
            logger(f"LLM 辅助标注未启用：{reason}")
            return {}, reason
        if not candidates:
            return {}, "没有需要复核的候选条款，未调用 LLM"

        budget_limited = list(candidates)[:self.config.max_clauses] if \
            self.config.max_clauses > 0 else list(candidates)
        refinements: dict = {}
        chunks = _chunk_candidates(budget_limited, self.config.max_chars_per_clause)
        logger(f"LLM 辅助标注：发送 {len(budget_limited)} 条候选条款（分 {len(chunks)} 批）")
        for order, chunk in enumerate(chunks, 1):
            items, error = self._request_chunk(chunk, logger, order, len(chunks))
            if error:
                return refinements, f"LLM 辅助标注未完成（{error}），已保留本地检测结果"
            for item in items:
                refinement = self._to_refinement(item)
                if refinement is not None:
                    refinements[refinement.uid] = refinement

        status = (f"LLM 辅助标注已应用：复核 {len(budget_limited)} 条候选条款，"
                  f"生效 {len(refinements)} 条（模型 {self.config.model}，"
                  f"Key 来源：{self.config.key_source}；只发送了候选条款片段）")
        return refinements, status

    # ---------- 单批请求 ----------
    def _request_chunk(self, chunk, logger, order: int, total: int) -> tuple:
        payload_items = [
            {"uid": hit.uid, "text": _clip(hit.quote, self.config.max_chars_per_clause),
             "local_categories": [hit.category]}
            for hit in chunk
        ]
        payload = {
            "model": self.config.model,
            "temperature": self.config.temperature,
            "messages": [
                {"role": "system", "content": TRIAGE_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(
                    {"items": payload_items}, ensure_ascii=False)},
            ],
            "response_format": {"type": "json_object"},
        }
        text, error = self._post(payload, logger, order, total)
        if error == "response_format_unsupported":
            payload.pop("response_format", None)
            text, error = self._post(payload, logger, order, total)
        if error:
            return [], error
        items, parse_error = _parse_items(text)
        if parse_error:
            return [], parse_error
        return items, ""

    def _post(self, payload: dict, logger, order: int, total: int) -> tuple:
        """实际发起请求，带回退重试；返回 (响应文本, 错误说明)。"""
        url = f"{self.config.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        attempts = max(1, self.config.max_retries + 1)
        last_error = ""
        for attempt in range(1, attempts + 1):
            try:
                response = requests.post(url, headers=headers, json=payload,
                                         timeout=self.config.timeout)
            except Exception as exc:
                last_error = f"第 {order}/{total} 批网络异常：{type(exc).__name__}"
                logger(f"{last_error}（第 {attempt} 次尝试）")
                time.sleep(min(2.0 * attempt, 5.0))
                continue

            if response.status_code == 200:
                try:
                    data = response.json()
                    content = data["choices"][0]["message"]["content"]
                except Exception:
                    last_error = f"第 {order}/{total} 批响应格式异常"
                    logger(last_error)
                    continue
                return content or "", ""

            # 鉴权/额度问题不值得重试，直接降级
            if response.status_code in (401, 403):
                return "", f"鉴权失败（HTTP {response.status_code}），请检查 API Key"
            if response.status_code == 429:
                last_error = f"第 {order}/{total} 批触发限流（HTTP 429）"
                logger(f"{last_error}（第 {attempt} 次尝试）")
                time.sleep(min(2.0 * attempt, 5.0))
                continue
            if response.status_code == 400 and "response_format" in payload:
                return "", "response_format_unsupported"
            last_error = f"第 {order}/{total} 批请求失败（HTTP {response.status_code}）"
            logger(last_error)

        return "", last_error or "未知错误"

    # ---------- 结果清洗 ----------
    def _to_refinement(self, item: dict) -> Refinement | None:
        """把模型的单条输出转成 :class:`Refinement`（含红线与引文校验）。"""
        if not isinstance(item, dict):
            return None
        uid = str(item.get("uid") or "").strip()
        if not uid:
            return None

        category = str(item.get("category") or "").strip().lower()
        if category not in CATEGORY_ORDER:
            category = ""

        # 说明文字一律过红线：模型若写了结论，会被替换成「需人工确认」
        label, label_hits = guard.sanitize_verdict(str(item.get("label") or ""))
        note, note_hits = guard.sanitize_verdict(str(item.get("note") or ""))

        # 引文只用于校验，绝不采纳为证据（防幻觉）：真正的回溯比对在
        # apply_refinements 中进行，那里同时掌握源文本
        return Refinement(
            uid=uid, category=category, label=label.strip()[:40],
            note=note.strip()[:200], relevant=bool(item.get("relevant", True)),
            extra={"intercepted": label_hits + note_hits,
                   "model_quote": str(item.get("quote") or "").strip()})


def _clip(text: str, limit: int) -> str:
    """按上限截断条款文本（既省 token，也减少不必要的数据外发）。"""
    value = str(text or "")
    if limit > 0 and len(value) > limit:
        return value[:limit] + "…"
    return value


def _chunk_candidates(candidates, max_chars_per_clause: int) -> list:
    """按字符预算把候选条款分片，避免单次请求过大。"""
    chunks: list = []
    current: list = []
    used = 0
    for hit in candidates:
        size = len(_clip(hit.quote, max_chars_per_clause)) + 80
        if current and used + size > REQUEST_CHAR_BUDGET:
            chunks.append(current)
            current, used = [], 0
        current.append(hit)
        used += size
    if current:
        chunks.append(current)
    return chunks


def _parse_items(text: str) -> tuple:
    """从模型输出中稳健地解析出 items 数组。"""
    raw = (text or "").strip()
    if not raw:
        return [], "模型返回空内容"
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    data = None
    try:
        data = json.loads(raw)
    except Exception:
        start, end = raw.find("{"), raw.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(raw[start:end + 1])
            except Exception:
                data = None
    if data is None:
        return [], "模型输出不是合法 JSON"
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)], ""
    if isinstance(data, dict):
        items = data.get("items")
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)], ""
        # 兼容 {"uid": {...}} 形式
        if all(isinstance(value, dict) for value in data.values()):
            merged = []
            for uid, value in data.items():
                merged.append({**value, "uid": uid})
            return merged, ""
    return [], "模型输出缺少 items 数组"


# ---------------------------------------------------------------------------
# 结果应用
# ---------------------------------------------------------------------------
def apply_refinements(hits, refinements: dict, guard_stats, logger=None) -> int:
    """把 LLM 标注应用到本地命中上。

    规则：
    - 只**补充** ``llm_label`` / ``llm_note``，不改写本地分类与置信度依据
    - 模型返回的引文必须能在源文本中逐字匹配，否则丢弃并计入 ``dropped_quotes``
    - 模型认为"不相关"时只降权并标注，**不删除条目**（由人决定）
    - 模型给出的新分类若本地未命中，则补一条明确标注"无关键词证据"的低置信度条目

    :return: 被标注的条目数
    """
    log = logger or (lambda _msg: None)
    if not refinements:
        return 0
    applied = 0
    sources = {hit.uid: hit.quote for hit in hits}
    extra_hits: list = []
    # 同一个 uid 可能有多条分类命中（同一条款命中多个类别），
    # 用集合去重，避免模型建议的新分类被重复补入、拦截计数被重复累加
    existing = {(hit.uid, hit.category) for hit in hits}
    counted: set = set()
    for hit in hits:
        refinement = refinements.get(hit.uid)
        if refinement is None:
            continue

        # 引文防幻觉：模型引文必须能在源文本中逐字找到
        model_quote = (refinement.extra or {}).get("model_quote") or ""
        if model_quote and not guard.quote_in_source(model_quote, sources.get(hit.uid, "")):
            guard_stats.dropped_quotes += 1
            guard_stats.notes.append(f"丢弃无法回溯的模型引文（{hit.uid}）")
            log(f"丢弃无法回溯的模型引文：{hit.uid}")

        # 同一 uid 可能对应多条分类命中，拦截计数只按条款累加一次
        if hit.uid not in counted:
            counted.add(hit.uid)
            guard_stats.intercepted += int((refinement.extra or {}).get("intercepted") or 0)

        hit.llm_applied = True
        hit.llm_label = refinement.label or hit.llm_label
        hit.llm_note = refinement.note or hit.llm_note
        if not refinement.relevant:
            hit.llm_label = f"模型标注可能不相关（需人工确认）：{hit.llm_label}"
            hit.confidence = round(hit.confidence * 0.9, 3)
        applied += 1

        # 模型给出的新分类：补一条"无关键词证据"的低置信度线索
        key = (hit.uid, refinement.category)
        if (refinement.relevant and refinement.category
                and refinement.category != hit.category and key not in existing):
            existing.add(key)
            extra_hits.append(ClauseHit(
                category=refinement.category, quote=hit.quote,
                matched_terms=["LLM 标注（无关键词证据）"],
                source_url=hit.source_url, page_title=hit.page_title,
                heading=hit.heading, uid=hit.uid, clause_index=hit.clause_index,
                char_offset=hit.char_offset, confidence=0.4,
                context_note="该分类来自 LLM 辅助标注，本地关键词未命中，请重点人工核对。",
                llm_label=refinement.label, llm_note=refinement.note,
                llm_applied=True, needs_human_review=True,
                review_hint=hit.review_hint))
    hits.extend(extra_hits)
    if extra_hits:
        hits.sort(key=lambda item: (-item.confidence, item.source_url, item.clause_index))
        log(f"LLM 补充了 {len(extra_hits)} 条本地关键词未命中的分类线索")
    return applied


def enrich(hits, *, llm_config: LLMConfig | None = None, guard_stats=None,
           log=None, select_limit: int | None = None) -> tuple:
    """一站式辅助标注：挑候选 → 调用 LLM → 应用结果。

    :return: (被标注条数, 引擎标记, 状态说明, guard_stats)
    """
    from core.compliance.classifier import select_candidates
    from core.compliance.models import ENGINE_LOCAL, ENGINE_LOCAL_LLM, GuardStats

    stats = guard_stats if guard_stats is not None else GuardStats()
    limit = config.LLM_MAX_CLAUSES if select_limit is None else int(select_limit)
    client = LLMClient(llm_config)
    ok, reason = client.available()
    if not ok:
        return 0, ENGINE_LOCAL, reason, stats

    candidates = select_candidates(hits, limit=limit)
    if not candidates:
        return 0, ENGINE_LOCAL, "没有需要复核的候选条款，未调用 LLM", stats

    refinements, status = client.refine(candidates, log=log)
    applied = apply_refinements(hits, refinements, stats, logger=log)
    if not refinements:
        return 0, ENGINE_LOCAL, status, stats
    return applied, ENGINE_LOCAL_LLM, status, stats


__all__ = [
    "TRIAGE_SYSTEM_PROMPT", "REQUEST_CHAR_BUDGET", "LLMConfig", "Refinement",
    "LLMClient", "apply_refinements", "enrich",
]
