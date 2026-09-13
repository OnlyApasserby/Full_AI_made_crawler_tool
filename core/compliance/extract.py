"""正文提取与条款切分（报告中引文的**唯一**来源）。

为什么不复用 :func:`core.parser.extract_text`
--------------------------------------------
那个函数服务于"整页纯文字存档"，会把所有标签换成换行、也不区分导航/页脚，
用于条款识别会有两类问题：导航菜单被当成条款、段落边界丢失导致一条命中
跨越无关内容。本模块在它的基础上做了三件事：

1. **去噪**：剥离 script/style/noscript/nav/header/footer/aside/form 等，
   并尽量收敛到 main/article 正文区；
2. **保留结构**：段落之间保留 ``\\n``，并识别"第X条 / Article N"这类标题，
   作为后续条款的小节上下文；
3. **偏移可验证**：所有 :class:`~core.compliance.models.Clause` 的
   ``start/end`` 都是**对归一化正文**的真实下标，即
   ``text[clause.start:clause.end] == clause.text`` 恒成立——
   报告里的引文因此可以被逐字回溯核对。

归一化在切分之前一次完成，之后不再改动文本，这是偏移可信的前提。
"""

from __future__ import annotations

import html as html_lib
import re

from core.compliance.keywords import MAX_CLAUSE_CHARS, MIN_CLAUSE_CHARS
from core.compliance.models import Clause

#: 需要整块丢弃的噪音标签
NOISE_TAGS = ("script", "style", "noscript", "nav", "header", "footer",
              "aside", "form", "iframe", "svg", "button", "template")
#: 视为块级边界、需要补换行的标签
BLOCK_TAGS = ("p", "div", "br", "li", "tr", "td", "th", "section", "article",
              "blockquote", "dd", "dt", "pre", "table", "ul", "ol",
              "h1", "h2", "h3", "h4", "h5", "h6", "hr")

#: 条款累积到这个长度就切一条（兼顾上下文完整与检索精度）
CLAUSE_TARGET_CHARS = 80

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_BODY_RE = re.compile(r"<body[^>]*>(.*?)</body>", re.IGNORECASE | re.DOTALL)
_ANY_TAG_RE = re.compile(r"<[^>]+>")
_NOISE_BLOCK_RE = re.compile(
    r"<(?:%s)\b[^>]*>.*?</(?:%s)\s*>" % ("|".join(NOISE_TAGS), "|".join(NOISE_TAGS)),
    re.IGNORECASE | re.DOTALL)
_BLOCK_TAG_RE = re.compile(r"</?(?:%s)\b[^>]*>" % "|".join(BLOCK_TAGS), re.IGNORECASE)

#: 句末标点：中文句号问号叹号，以及英文句点（要求后面是空白或行尾）
_SENT_END_RE = re.compile(r"[。！？!?]+|(?<=[A-Za-z0-9%\)\"'’”])\.(?=\s|$)", re.IGNORECASE)

#: 标题特征："第X条" / "一、" / "1.2 " / "Article 3" / "(3)"
_HEADING_PATTERNS = (
    re.compile(r"^第\s*[一二三四五六七八九十百零两\d]+\s*[条章节款项]"),
    re.compile(r"^[一二三四五六七八九十]+\s*[、\.]"),
    re.compile(r"^\d+(?:\.\d+)*\s*[、\.]?\s"),
    re.compile(r"^(?:article|section|clause|appendix|schedule)\s+\d+", re.IGNORECASE),
    re.compile(r"^[（(]\s*[一二三四五六七八九十\d]+\s*[)）]"),
)
#: 标题行长度上限（超过则视为普通段落）
HEADING_MAX_CHARS = 48
#: 以这些标点收尾的一定是句子而非标题（如"第二条 禁止行为：……。"整条属于条款正文）
_SENTENCE_TAIL = ("。", "！", "？", ".", "!", "?")
#: 以这些标点收尾的短行不是标题（逗号/分号/顿号说明话没说完）
_FRAGMENT_TAIL = ("，", ",", "、", "；", ";")
#: 非编号短行的长度上限
SHORT_HEADING_MAX_CHARS = 24
#: 编号前缀作为小节上下文时的长度上限
HEADING_PREFIX_MAX_CHARS = 30
#: 编号与标题之间的分隔符
_PREFIX_SEPARATORS = ("：", ":", "—", "-", "－")


# ---------------------------------------------------------------------------
# 正文提取
# ---------------------------------------------------------------------------
def extract_title(html_text: str) -> str:
    """提取 ``<title>`` 文本（去标签、压缩空白、限长）。"""
    match = _TITLE_RE.search(html_text or "")
    if not match:
        return ""
    title = html_lib.unescape(_ANY_TAG_RE.sub(" ", match.group(1)))
    return re.sub(r"\s+", " ", title).strip()[:200]


def normalize_text(text: str) -> str:
    """归一化文本：全角/不换行空格转普通空格、压缩行内空白、丢弃空行。

    归一化只做一次且不改变字符顺序，因此在此结果上计算的偏移始终有效。
    """
    if not text:
        return ""
    cleaned = str(text).replace("\u3000", " ").replace("\xa0", " ")
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = re.sub(r"[ \t\f\v]+", " ", cleaned)
    lines = [line.strip() for line in cleaned.split("\n")]
    return "\n".join(line for line in lines if line)


def _main_text_bs4(html_text: str) -> str:
    """用 bs4 提取正文（可选依赖缺失时返回空串）。"""
    try:
        from bs4 import BeautifulSoup  # 可选依赖
    except Exception:
        return ""
    try:
        try:
            soup = BeautifulSoup(html_text or "", "lxml")
        except Exception:
            soup = BeautifulSoup(html_text or "", "html.parser")
    except Exception:
        return ""
    for tag in soup(NOISE_TAGS):
        tag.decompose()
    scope = None
    for finder in (lambda: soup.find("main"),
                   lambda: soup.find("article"),
                   lambda: soup.find(attrs={"id": re.compile(
                       r"content|main|policy|terms|legal|agreement", re.I)}),
                   lambda: soup.find(attrs={"class": re.compile(
                       r"content|main|policy|terms|legal|agreement", re.I)})):
        try:
            scope = finder()
        except Exception:
            scope = None
        if scope is not None:
            break
    target = scope or soup.body or soup
    return target.get_text("\n")


def _main_text_regex(html_text: str) -> str:
    """正则兜底提取正文（无 bs4 时使用，精度略低但不依赖外部库）。"""
    text = _NOISE_BLOCK_RE.sub(" ", html_text or "")
    body = _BODY_RE.search(text)
    if body:
        text = body.group(1)
    # 块级标签补换行，保证段落边界不丢
    text = _BLOCK_TAG_RE.sub("\n", text)
    text = _ANY_TAG_RE.sub(" ", text)
    return html_lib.unescape(text)


def extract_main_text(html_text: str) -> str:
    """提取页面正文并归一化（返回值即为条款切分的基准文本）。"""
    if not html_text:
        return ""
    primary = _main_text_bs4(html_text)
    if not primary.strip():
        primary = _main_text_regex(html_text)
    if not primary.strip():
        primary = html_lib.unescape(_ANY_TAG_RE.sub("\n", html_text))
    return normalize_text(primary)


# ---------------------------------------------------------------------------
# 条款切分
# ---------------------------------------------------------------------------
def looks_like_heading(line: str) -> bool:
    """判断一行是否为小节标题。

    注意顺序：**先判句末标点**，再判编号特征。否则"第二条 禁止行为：……。"
    这种"编号开头的完整条款句"会被误判成标题，导致正文被当成目录丢弃。
    """
    text = (line or "").strip()
    if not text or len(text) > HEADING_MAX_CHARS:
        return False
    if text.endswith(_SENTENCE_TAIL):
        return False
    if any(pattern.match(text) for pattern in _HEADING_PATTERNS):
        return True
    if text.endswith(_FRAGMENT_TAIL):
        return False
    return len(text) <= SHORT_HEADING_MAX_CHARS


def heading_prefix(line: str) -> str:
    """取编号开头的条款句的"小节名"，用作后续条款的上下文。

    例：``第二条 禁止行为：未经……`` → ``第二条 禁止行为``；
    取不到的返回空串。
    """
    text = (line or "").strip()
    if not text or not any(pattern.match(text) for pattern in _HEADING_PATTERNS):
        return ""
    for separator in _PREFIX_SEPARATORS:
        index = text.find(separator)
        if 0 < index <= HEADING_PREFIX_MAX_CHARS:
            return text[:index].strip()
    return text[:HEADING_PREFIX_MAX_CHARS].strip()


def _sentence_spans(line: str, line_start: int) -> list:
    """把一行切成句子区间，并去掉两端的空白（保持下标与原文一致）。"""
    spans = []
    cursor = 0
    for match in _SENT_END_RE.finditer(line):
        spans.append((cursor, match.end()))
        cursor = match.end()
    if cursor < len(line):
        spans.append((cursor, len(line)))

    trimmed = []
    for start, end in spans:
        while start < end and line[start].isspace():
            start += 1
        while end > start and line[end - 1].isspace():
            end -= 1
        if end > start:
            trimmed.append((line_start + start, line_start + end))
    return trimmed


def _split_long_span(start: int, end: int, text: str) -> list:
    """把超长区间按上限硬切（优先在空格处断开）。"""
    chunks = []
    cursor = start
    while end - cursor > MAX_CLAUSE_CHARS:
        limit = cursor + MAX_CLAUSE_CHARS
        cut = text.rfind(" ", cursor + MAX_CLAUSE_CHARS // 2, limit)
        if cut <= cursor:
            cut = limit
        chunks.append((cursor, cut))
        cursor = cut
        while cursor < end and text[cursor].isspace():
            cursor += 1
    if end > cursor:
        chunks.append((cursor, end))
    return chunks


def _merge_spans(spans: list, text: str) -> list:
    """把句子合并成大小适中的条款区间（尾部过短并入上一条）。"""
    merged: list = []
    start = None
    end = None
    for span_start, span_end in spans:
        if start is None:
            start, end = span_start, span_end
        else:
            end = span_end
        if end - start >= CLAUSE_TARGET_CHARS:
            merged.append((start, end))
            start = None
    if start is not None:
        if merged and (end - start) < MIN_CLAUSE_CHARS:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))

    result: list = []
    for span_start, span_end in merged:
        if span_end - span_start > MAX_CLAUSE_CHARS:
            result.extend(_split_long_span(span_start, span_end, text))
        else:
            result.append((span_start, span_end))
    return result


def iter_clauses(text: str, page_index: int = 0, page_url: str = "",
                 start_index: int = 0) -> list:
    """把归一化正文切分为条款列表。

    :param text: :func:`extract_main_text` 的输出（必须是同一份文本，
        否则偏移将失去意义）
    :param page_index: 页面序号（用于生成 :attr:`Clause.uid`）
    :return: :class:`~core.compliance.models.Clause` 列表
    """
    clauses: list = []
    if not text:
        return clauses
    index = start_index
    offset = 0
    heading = ""
    for line in text.split("\n"):
        line_start = offset
        offset += len(line) + 1          # +1 为被 split 掉的换行
        if not line:
            continue
        if looks_like_heading(line):
            heading = line[:HEADING_MAX_CHARS]
            clauses.append(Clause(
                text=line, page_index=page_index, index=index,
                start=line_start, end=line_start + len(line),
                heading=heading, page_url=page_url))
            index += 1
            continue
        # 编号开头的完整条款句：用其编号前缀做小节上下文
        prefix = heading_prefix(line)
        if prefix:
            heading = prefix
        for span_start, span_end in _merge_spans(
                _sentence_spans(line, line_start), text):
            clauses.append(Clause(
                text=text[span_start:span_end], page_index=page_index,
                index=index, start=span_start, end=span_end,
                heading=heading, page_url=page_url))
            index += 1
    return clauses


def prepare_page(html_text: str, page_index: int = 0,
                 page_url: str = "") -> tuple:
    """一步完成"标题 + 正文 + 条款切分"。

    :return: (title, text, clauses)
    """
    text = extract_main_text(html_text)
    return extract_title(html_text), text, iter_clauses(
        text, page_index=page_index, page_url=page_url)


__all__ = [
    "NOISE_TAGS", "BLOCK_TAGS", "CLAUSE_TARGET_CHARS", "HEADING_MAX_CHARS",
    "SHORT_HEADING_MAX_CHARS", "extract_title", "normalize_text",
    "extract_main_text", "looks_like_heading", "heading_prefix",
    "iter_clauses", "prepare_page",
]
