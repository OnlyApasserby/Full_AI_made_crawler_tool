"""电子书正文抓取：从阅读页起始地址出发，自动翻页并把整本书落盘为纯文本。

设计要点（与现有模块保持一致，不引入新框架）
--------------------------------------------
- **抓取层复用** :mod:`core.fetcher`：``RequestsFetcher``（静态，快）与
  ``PlaywrightFetcher``（动态渲染，适配 JS 站点）暴露同一个 ``fetch()`` 契约，
  因此本模块只负责"取正文 / 找下一页 / 清洗 / 拼装"，不再各写一份请求逻辑。
  两者内部都已统一走 ``requests.Session`` 连接池、默认 UA、代理与编码推断。
- **正文与翻页定位**：CSS 选择器优先（``beautifulsoup4`` + ``lxml``），
  依赖缺失时自动退回内置正则实现，与 :mod:`core.extractor.rule_extractor`
  的降级策略一致——降级只影响复杂选择器，不影响主流程。
- **翻页终止**：找不到"下一页" / URL 命中已访问集合（防死循环） / 达到
  ``max_pages`` 上限，三者任一即停；页间随机延时，单页失败指数退避重试。
- **落盘**：``output_path``（UTF-8 纯文本）+ ``*.meta.json`` 元数据
  （书名 / 章节列表 / 页数 / 抓取时间 / 逐页明细）。

典型用法::

    from core.ebook_fetcher import fetch_book

    txt_path = fetch_book(
        "https://example.com/book/12345/1.html",
        "output/某本书.txt",
        dynamic=False,          # JS 渲染站点改 True（需 playwright）
        delay=(0.5, 1.5),
        max_pages=1000,
    )

免责声明：本模块只提供技术抓取能力，**不判断目标站点是否允许抓取**。
使用前请自行确认站点服务条款与著作权要求；``respect_robots=True``（默认）
只保证按 robots.txt 的约定跳过被禁止的路径。
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass
from html import unescape
from typing import Sequence
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

from config import CRAWLER_USER_AGENT
from core.fetcher import FetchContext, FetcherUnavailable, create_fetcher
from core.fetcher.base import interruptible_sleep
from utils.user_agents import UserAgentPool

try:  # 可选依赖：完整 CSS 选择器支持（缺失时退回内置正则，与规则提取器一致）
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:  # pragma: no cover - 取决于运行环境
    BeautifulSoup = None
    HAS_BS4 = False


LOGGER_NAME = "core.ebook_fetcher"
logger = logging.getLogger(LOGGER_NAME)
if not logger.handlers:  # 库被直接调用（未配置 logging）时保证日志可见
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

#: 默认 UA：显式固定 UA 池中的第一个，保证请求头与 robots.txt 校验身份一致
DEFAULT_USER_AGENT = UserAgentPool().all()[0]

# ---------------------------------------------------------------------------
# 默认选择器（按优先级顺序尝试，命中即用）
# ---------------------------------------------------------------------------
#: 正文容器候选：从站点惯用 id 到语义标签，逐级放宽
DEFAULT_CONTENT_SELECTORS: tuple[str, ...] = (
    "#content", "#chaptercontent", "#booktext", "#htmlContent", "#BookText",
    "#nr1", "#txtContent", "#chapterContent", "#read-content", "#book-content",
    ".content", ".chapter-content", ".read-content", ".reader-content",
    ".txtnav", ".book-content", ".article-content", ".entry-content",
    "div[data-role='content']", "article", "main",
)

#: 噪声节点：导航 / 广告 / 页脚 / 侧栏 / 脚本样式等，取正文前先整块删除
DEFAULT_EXCLUDE_SELECTORS: tuple[str, ...] = (
    "script", "style", "noscript", "iframe", "ins",
    "nav", "header", "footer", "aside",
    ".nav", ".navbar", ".navigation", ".menu", ".breadcrumb", ".breadcrumbs",
    ".ad", ".ads", ".advert", ".advertisement", ".gg", ".google-ad",
    ".footer", ".header", ".sidebar", ".side", ".toolbar", ".tools",
    ".comment", ".comments", ".share", ".social", ".recommend", ".related",
    ".bottem", ".booklist", ".chapterlist", ".listmain",
    ".notice", ".tips", ".banner", ".copyright",
)

#: "下一页"选择器候选（``:contains()`` 为 jQuery 风格，本模块做了近似实现）
DEFAULT_NEXT_SELECTORS: tuple[str, ...] = (
    "a#next", "a#nextPage", "a.next", "a.nextpage", "a.next-page",
    "#next", "#nextPage", "#pb_next", "a[rel='next']",
    "a:contains('下一页')", "a:contains('下一章')", "a:contains('下一节')",
    "a:contains('下页')", "a:contains('Next')",
)

#: 章节标题选择器候选
DEFAULT_TITLE_SELECTORS: tuple[str, ...] = (
    "h1", ".chapter-title", ".chapterTitle", "#chapter-title", "#chapterTitle",
    ".bookname h1", ".title h1", "h2.chapter", "h2", "h3",
)

#: 翻页链接的文本特征（选择器未命中时的兜底，参考 core.paginator）
_NEXT_TEXT_RE = re.compile(
    r"^\s*(下一页|下页|下一章|下一节|下一张|后一页|next(\s*page)?|›|»|→|>>)\s*$",
    re.IGNORECASE)
#: 章节标题文本特征（从 ``<title>`` 里剥离站点后缀时使用）
_CHAPTER_TEXT_RE = re.compile(
    r"^\s*(?:正文\s*)?(第[0-9零一二三四五六七八九十百千万两]+[章节回卷篇部集]"
    r"[^\n]{0,40}|chapter\s*\d+[^\n]{0,40})\s*$", re.IGNORECASE)
#: 不可见字符（零宽字符 / 控制符 / 不换行空格）
_INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\ufeff\xa0]")
#: 连续空行压缩
_BLANK_LINES_RE = re.compile(r"\n{3,}")
#: 行内多余空白
_INLINE_SPACE_RE = re.compile(r"[ \t\u3000]{2,}")


class EbookFetchError(RuntimeError):
    """电子书抓取失败（参数非法、起始页不可用、动态渲染不可用、落盘失败）。

    与 :class:`core.fetcher.base.FetchError` 的分工：单页网络失败**不**抛本异常
    （按需求记 ERROR 日志后继续），只有"整体无法继续"时才抛。
    """


@dataclass
class PageResult:
    """单页抓取结果（成功与失败都记录，便于元数据里逐页追溯）。"""

    index: int
    url: str
    title: str = ""
    chars: int = 0
    error: str = ""
    next_url: str = ""
    attempts: int = 0
    elapsed: float = 0.0
    html: str = ""

    @property
    def fetched(self) -> bool:
        """该页是否成功取到页面内容（不保证正文非空）。"""
        return not self.error and bool(self.html)

    @property
    def ok(self) -> bool:
        """该页是否成功取到正文。"""
        return self.fetched and bool(self.chars)

    def to_dict(self) -> dict:
        """转元数据条目（**不包含** HTML，避免元数据体积失控）。"""
        return {
            "index": self.index,
            "url": self.url,
            "title": self.title,
            "chars": self.chars,
            "error": self.error,
            "next_url": self.next_url,
            "attempts": self.attempts,
            "elapsed": round(self.elapsed, 3),
        }


# ---------------------------------------------------------------------------
# 参数与文本工具
# ---------------------------------------------------------------------------
def normalize_delay(delay) -> tuple[float, float]:
    """把 ``delay`` 规整为 ``(最小秒, 最大秒)`` 区间。

    支持 ``(0.5, 1.5)`` 元组/列表、单个数字（等价于固定延时）与 ``None``
    （不等待）；两端顺序颠倒时自动交换。

    >>> normalize_delay((1.5, 0.5))
    (0.5, 1.5)
    >>> normalize_delay(0.8)
    (0.8, 0.8)
    """
    if delay is None:
        return 0.0, 0.0
    if isinstance(delay, (int, float)):
        value = max(0.0, float(delay))
        return value, value
    try:
        values = [max(0.0, float(v)) for v in delay]
    except (TypeError, ValueError):
        return 0.0, 0.0
    if not values:
        return 0.0, 0.0
    return min(values), max(values)


def clean_text(text: str, collapse_blank: bool = True) -> str:
    """统一换行、去除多余空白与不可见字符。

    :param text: 原始文本（可含 HTML 实体残留）
    :param collapse_blank: 是否把 3 行以上空行压缩为 2 行
    """
    content = unescape(text or "")
    content = _INVISIBLE_RE.sub(" ", content)
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    lines = [_INLINE_SPACE_RE.sub(" ", line).strip()
             for line in content.split("\n")]
    output = "\n".join(lines).strip()
    if collapse_blank:
        output = _BLANK_LINES_RE.sub("\n\n", output)
    return output


# ---------------------------------------------------------------------------
# HTML 解析（BeautifulSoup 优先，缺失时退化为正则）
# ---------------------------------------------------------------------------
def _make_soup(html: str):
    """构造 BeautifulSoup；``bs4`` 缺失或解析失败时返回 None（调用方走正则分支）。"""
    if not HAS_BS4 or not html:
        return None
    for parser in ("lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _strip_noise(soup, exclude_selectors: Sequence[str]) -> None:
    """整块删除噪声节点（导航/广告/页脚等），就地修改 soup。"""
    if soup is None:
        return
    for selector in exclude_selectors or ():
        if not selector:
            continue
        try:
            for node in soup.select(selector):
                node.decompose()
        except Exception:
            continue


def _strip_noise_regex(html: str, exclude_selectors: Sequence[str]) -> str:
    """正则兜底删除噪声节点（仅支持标签名与 ``.class`` / ``#id`` 简单选择器）。"""
    content = html or ""
    for selector in exclude_selectors or ():
        token = (selector or "").strip()
        if not token:
            continue
        if token.startswith("."):
            pattern = (rf'<(\w+)\b[^>]*\bclass\s*=\s*["\'][^"\']*\b'
                       rf'{re.escape(token[1:])}\b[^"\']*["\'][^>]*>.*?</\1\s*>')
        elif token.startswith("#"):
            pattern = (rf'<(\w+)\b[^>]*\bid\s*=\s*["\']{re.escape(token[1:])}'
                       rf'["\'][^>]*>.*?</\1\s*>')
        else:
            tag = re.escape(token)
            pattern = rf"<{tag}\b[^>]*>.*?</{tag}\s*>"
        try:
            content = re.sub(pattern, " ", content, flags=re.IGNORECASE | re.DOTALL)
        except re.error:
            continue
    return content


def _select_text(soup, selector: str) -> str:
    """取选择器命中的第一个非空元素的纯文本（无命中返回空串）。"""
    try:
        nodes = soup.select(selector)
    except Exception:
        return ""
    for node in nodes:
        text = node.get_text("\n", strip=True)
        if text:
            return text
    return ""


def _select_text_regex(html: str, selector: str) -> str:
    """正则兜底取文本：支持 ``tag`` / ``.class`` / ``#id`` 简单选择器。"""
    token = (selector or "").strip()
    if not token:
        return ""
    if token.startswith("."):
        pattern = (rf'<(\w+)\b[^>]*\bclass\s*=\s*["\'][^"\']*\b'
                   rf'{re.escape(token[1:])}\b[^"\']*["\'][^>]*>(.*?)</\1\s*>')
    elif token.startswith("#"):
        pattern = (rf'<(\w+)\b[^>]*\bid\s*=\s*["\']{re.escape(token[1:])}'
                   rf'["\'][^>]*>(.*?)</\1\s*>')
    else:
        tag = re.escape(token)
        pattern = rf"<{tag}\b[^>]*>(.*?)</{tag}\s*>"
    try:
        match = re.search(pattern, html or "", re.IGNORECASE | re.DOTALL)
    except re.error:
        return ""
    if not match:
        return ""
    return _html_to_text(match.group(match.lastindex or 1))


def _html_to_text(html: str) -> str:
    """正则版 HTML → 纯文本（去脚本样式、块级标签转换行、还原实体）。"""
    content = re.sub(r"<(script|style)\b[^>]*>.*?</\1\s*>", " ", html or "",
                     flags=re.IGNORECASE | re.DOTALL)
    content = re.sub(r"<br\s*/?>", "\n", content, flags=re.IGNORECASE)
    content = re.sub(r"</(p|div|li|tr|h[1-6])\s*>", "\n", content,
                     flags=re.IGNORECASE)
    content = re.sub(r"<[^>]+>", "", content)
    return clean_text(unescape(content))


def extract_content(html: str, selectors: Sequence[str] = (),
                    exclude_selectors: Sequence[str] = (),
                    min_chars: int = 1) -> str:
    """从页面 HTML 提取正文纯文本。

    策略：先整块删除噪声节点 → 按 ``selectors`` 顺序尝试（命中文本短于
    ``min_chars`` 则继续下一个）→ 全未命中时用 ``body`` 全文兜底。

    :param html: 页面 HTML
    :param selectors: 正文容器选择器候选（空则用默认）
    :param exclude_selectors: 噪声节点选择器（空则用默认）
    :param min_chars: 判定"命中"的最小字符数
    """
    selectors = tuple(selectors) or DEFAULT_CONTENT_SELECTORS
    excludes = tuple(exclude_selectors) if exclude_selectors \
        else DEFAULT_EXCLUDE_SELECTORS
    floor = max(1, int(min_chars or 1))
    soup = _make_soup(html)
    if soup is None:
        stripped = _strip_noise_regex(html, excludes)
        for selector in selectors:
            if ":contains(" in selector or "[" in selector:
                continue                      # 正则分支不支持伪类/属性选择器
            text = _select_text_regex(stripped, selector)
            if len(text) >= floor:
                return text
        return _html_to_text(stripped)

    _strip_noise(soup, excludes)
    for selector in selectors:
        if ":contains(" in selector:
            continue
        text = clean_text(_select_text(soup, selector))
        if len(text) >= floor:
            return text
    body = soup.body or soup
    return clean_text(body.get_text("\n", strip=True))


def extract_title(html: str, selectors: Sequence[str] = ()) -> str:
    """提取章节标题：选择器候选 → ``<title>`` → 空串。

    ``<title>`` 常带站点后缀（``第一章 xxx_书名_站名``），命中章节特征时
    按分隔符取首段，避免把站点名混入章节列表。
    """
    selectors = tuple(selectors) or DEFAULT_TITLE_SELECTORS
    doc_title = ""
    soup = _make_soup(html)
    if soup is not None:
        for selector in selectors:
            text = clean_text(_select_text(soup, selector))
            first = text.split("\n", 1)[0].strip() if text else ""
            if first and len(first) <= 60:
                return first
        head = soup.find("title")
        doc_title = clean_text(head.get_text()) if head is not None else ""
    else:
        match = re.search(r"<title[^>]*>(.*?)</title>", html or "",
                          re.IGNORECASE | re.DOTALL)
        if match:
            doc_title = _html_to_text(match.group(1))
    if doc_title:
        for separator in ("_", "|", " - ", "—"):
            head_part = doc_title.split(separator, 1)[0].strip()
            if head_part and _CHAPTER_TEXT_RE.match(head_part):
                return head_part
    return doc_title[:80]


def _match_contains(node, keyword: str) -> bool:
    """节点文本是否包含关键词（``:contains()`` 伪类的近似实现）。"""
    try:
        return keyword.lower() in node.get_text(" ", strip=True).lower()
    except Exception:
        return False


def extract_next_url(html: str, base_url: str,
                     selectors: Sequence[str] = ()) -> str:
    """提取"下一页"链接的绝对地址（找不到返回空串）。

    顺序：``<link rel="next">`` → 选择器候选（含 ``:contains()`` 近似实现）
    → 链接文本特征兜底。跨页死循环由调用方的 ``visited`` 集合兜底。
    """
    if not html:
        return ""
    selectors = tuple(selectors) if selectors else DEFAULT_NEXT_SELECTORS
    soup = _make_soup(html)
    found: list[str] = []

    def _push(raw: str) -> None:
        url = _absolute(raw, base_url)
        if url and url not in found:
            found.append(url)

    if soup is not None:
        rel_next = soup.find("link", attrs={"rel": "next"})
        if rel_next is None:
            for candidate in soup.find_all("link"):
                rel = candidate.get("rel") or []
                if isinstance(rel, str):
                    rel = [rel]
                if "next" in [str(item).lower() for item in rel]:
                    rel_next = candidate
                    break
        if rel_next is not None:
            _push(rel_next.get("href", ""))
        for selector in selectors:
            if ":contains(" in selector:
                keyword = selector.split(":contains(", 1)[1].rstrip(")'\"").strip("'\"")
                for anchor in soup.find_all("a"):
                    if _match_contains(anchor, keyword):
                        _push(anchor.get("href", ""))
                continue
            try:
                nodes = soup.select(selector)
            except Exception:
                continue
            for node in nodes:
                href = node.get("href") if hasattr(node, "get") else None
                if href:
                    _push(href)
        for anchor in soup.find_all("a"):       # 文本特征兜底
            text = anchor.get_text(" ", strip=True)
            if text and _NEXT_TEXT_RE.match(text):
                _push(anchor.get("href", ""))
    else:
        for pattern in (r'<link\b[^>]*\brel\s*=\s*["\']?next["\']?[^>]*>',
                        r'<a\b[^>]*\brel\s*=\s*["\']next["\'][^>]*>'):
            for match in re.finditer(pattern, html, re.IGNORECASE):
                href = re.search(r'href\s*=\s*["\']([^"\']+)["\']',
                                 match.group(0), re.IGNORECASE)
                if href:
                    _push(href.group(1))
        for match in re.finditer(r"<a\b[^>]*>(.*?)</a>", html,
                                 re.IGNORECASE | re.DOTALL):
            text = _html_to_text(match.group(1)).strip()
            if not text or not _NEXT_TEXT_RE.match(text):
                continue
            href = re.search(r'href\s*=\s*["\']([^"\']+)["\']',
                             match.group(0), re.IGNORECASE)
            if href:
                _push(href.group(1))
    return found[0] if found else ""


def _absolute(raw: str, base_url: str) -> str:
    """相对链接转绝对链接；过滤非翻页链接（空锚点/脚本/邮件）并去掉 fragment。"""
    text = (raw or "").strip()
    if not text or text.lower().startswith(
            ("javascript:", "mailto:", "data:", "about:", "#")):
        return ""
    try:
        absolute = urljoin(base_url, text)
    except ValueError:
        return ""
    if not absolute.startswith(("http://", "https://")):
        return ""
    return absolute.split("#", 1)[0]


# ---------------------------------------------------------------------------
# robots.txt（urllib.robotparser）
# ---------------------------------------------------------------------------
def load_robots_parser(fetcher, start_url: str) -> tuple:
    """获取并解析目标站点的 robots.txt。

    :return: ``(RobotFileParser | None, 说明文本)``；站点未提供 robots.txt
        （404/403）或获取异常时返回 ``(None, 原因)``，按"未声明即不限制"处理，
    与 :mod:`core.compliance.scanner` 的既有做法一致。
    """
    parsed = urlparse(start_url)
    robots_url = urljoin(f"{parsed.scheme}://{parsed.netloc}", "/robots.txt")
    parser = RobotFileParser()
    parser.set_url(robots_url)
    try:
        ctx = fetcher.fetch(robots_url)
    except Exception as exc:                      # 网络异常不应阻断抓取
        return None, (f"robots.txt 获取异常（{type(exc).__name__}），"
                      f"本次跳过校验：{robots_url}")
    if ctx.status == 404:
        return None, f"该站点未提供 robots.txt（HTTP 404），按不限制处理"
    if ctx.status == 403:
        return None, "站点拒绝访问 robots.txt（HTTP 403），本次跳过校验"
    if not ctx.ok or not ctx.html:
        return None, (f"未获取到 robots.txt（{_failure_reason(ctx)}），"
                      f"本次跳过校验：{robots_url}")
    try:
        parser.parse(ctx.html.splitlines())
    except Exception as exc:
        return None, f"robots.txt 解析失败（{type(exc).__name__}），本次跳过校验"
    return parser, f"已加载 robots.txt：{robots_url}"


def _robots_allows(parser: RobotFileParser | None, url: str,
                   user_agent: str) -> bool:
    """robots.txt 是否允许该 UA 访问该 URL（无解析器视为允许）。"""
    if parser is None:
        return True
    try:
        return bool(parser.can_fetch(user_agent or "*", url))
    except Exception:
        return True


def _failure_reason(ctx: FetchContext) -> str:
    """把抓取上下文转成可读的失败原因。"""
    if ctx.error:
        return ctx.error
    if ctx.status:
        return f"HTTP {ctx.status}"
    return "无内容"


# ---------------------------------------------------------------------------
# 单页抓取（含重试与指数退避）
# ---------------------------------------------------------------------------
def _fetch_page(fetcher, url: str, *, dynamic: bool, wait_selector: str,
                scroll: bool, max_scroll: int, timeout: float, retries: int,
                backoff: float, page_index: int, log: logging.Logger) -> PageResult:
    """抓取单页并按需重试（指数退避 ``backoff * 2**(n-1)``）。

    单页彻底失败**不抛异常**：记 ERROR 日志并把失败信息留在结果里，
    由主流程决定跳过还是终止，保证"失败页继续而非中断"。
    """
    result = PageResult(index=page_index, url=url)
    started = time.monotonic()
    last_error = ""
    for attempt in range(max(0, retries) + 1):
        result.attempts = attempt + 1
        ctx = None
        try:
            if dynamic:
                ctx = fetcher.fetch(url, wait_selector=wait_selector, scroll=scroll,
                                    max_scroll=max_scroll, timeout=timeout)
            else:
                ctx = fetcher.fetch(url, timeout=timeout)
        except Exception as exc:                  # 抓取器本身抛出（依赖缺失等）
            last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
        if ctx is not None and ctx.ok:
            result.html = ctx.html
            result.url = ctx.final_url or url    # 跟随重定向后的真实地址
            result.elapsed = time.monotonic() - started
            return result
        last_error = _failure_reason(ctx) if ctx is not None else last_error
        if attempt < retries:
            wait = max(0.1, float(backoff) * (2 ** attempt))
            log.warning("第 %s 页抓取失败（%s），%.1fs 后进行第 %s 次重试：%s",
                        page_index, last_error, wait, attempt + 2, url)
            time.sleep(wait)
    result.error = last_error or "抓取失败"
    result.elapsed = time.monotonic() - started
    log.error("第 %s 页最终失败（已重试 %s 次）：%s | %s",
              page_index, max(0, retries), url, result.error)
    return result


#: 路径末段形如 ``chapter_12.html`` / ``/12345/13`` 时的数字拆分
_PATH_TAIL_NUM_RE = re.compile(r"^(.*?)(\d+)([^\d]*)$")
#: 已带页码参数的查询参数名（与 core.paginator._PAGE_PARAMS 保持一致的常用子集）
_PAGE_PARAMS = ("page", "p", "pg", "pn", "pageno", "page_no", "pageindex",
                "page_index", "pagenum", "pagenumber", "pager")
#: 粗略提取页面里的 href（用于判断"章节 URL 规律"是否被页面链接印证）
_HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)


def _tail_pattern(url: str) -> tuple:
    """返回 URL 的"可递增尾巴"描述：``("path", url, 前缀, 数字, 后缀)``
    或 ``("query", url, 参数名, 当前值)``；无法递增时返回空 tuple。

    只认同一种情况：路径末段是数字（``/book/12.html``、``/book/12``），
    或查询串里存在数字型页码参数（``?page=3``）。
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return ()
    if parsed.query:
        try:
            pairs = parse_qsl(parsed.query, keep_blank_values=True)
        except ValueError:
            pairs = []
        for name, value in pairs:
            if name.lower() in _PAGE_PARAMS and value.isdigit():
                return ("query", url, name.lower(), int(value))
    match = _PATH_TAIL_NUM_RE.match(parsed.path)
    if not match:
        return ()
    prefix, number, suffix = match.groups()
    return ("path", url, prefix, int(number), suffix)


def _bump_tail(pattern: tuple) -> str:
    """按"尾巴"规律生成下一页 URL（无法生成时返回空串）。"""
    if not pattern:
        return ""
    kind, url = pattern[0], pattern[1]
    try:
        parsed = urlparse(url)
    except ValueError:
        return ""
    if kind == "query":
        name, current = pattern[2], pattern[3]
        try:
            pairs = parse_qsl(parsed.query, keep_blank_values=True)
        except ValueError:
            return ""
        updated = [(key, str(current + 1) if key.lower() == name and val.isdigit()
                    else val) for key, val in pairs]
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path,
                           parsed.params, urlencode(updated), ""))
    prefix, number, suffix = pattern[2], pattern[3], pattern[4]
    return urlunparse((parsed.scheme, parsed.netloc,
                       f"{prefix}{number + 1}{suffix}",
                       parsed.params, parsed.query, ""))


def _collect_site_links(html: str, base_url: str) -> set[str]:
    """粗略收集页面内的同站链接（含 fragment 剥离），用于印证 URL 规律。"""
    found: set[str] = set()
    base_host = (urlparse(base_url).hostname or "").lower()
    for raw in _HREF_RE.findall(html or ""):
        url = _absolute(raw, base_url)
        if not url:
            continue
        try:
            host = (urlparse(url).hostname or "").lower()
        except ValueError:
            continue
        if host and base_host and host != base_host:
            continue
        found.add(url)
    return found


def _compute_next_url(result: PageResult, next_selectors: Sequence[str],
                      known: set[str], log: logging.Logger) -> str:
    """定位下一页：页面链接优先，缺失时按 URL 页码规律兜底。

    页码规律**只在被印证时才推断**（推断出的 URL 必须已经出现在本次抓取
    已见过的链接里），与 :mod:`core.paginator` 的"绝不凭猜测拼接参数、
    避免产生大量 404 噪声"保持一致。
    """
    linked = extract_next_url(result.html, result.url, next_selectors)
    if linked:
        return linked
    guessed = _bump_tail(_tail_pattern(result.url))
    if guessed and guessed in known:
        log.warning("　未找到下一页链接，按已验证的 URL 页码规律推断：%s", guessed)
        return guessed
    return ""


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def fetch_book(start_url: str, output_path: str, **kwargs) -> str:
    """抓取电子书阅读页正文并自动翻页，把整本书落盘为纯文本。

    :param start_url: 电子书起始阅读页（通常是第一章/第一页）
    :param output_path: 纯文本输出路径（UTF-8）；父目录不存在会自动创建
    :param kwargs: 可选参数

    **页面解析**

    - ``content_selectors``：正文容器 CSS 选择器列表（默认
      :data:`DEFAULT_CONTENT_SELECTORS`）
    - ``exclude_selectors``：噪声节点选择器列表（默认
      :data:`DEFAULT_EXCLUDE_SELECTORS`）
    - ``next_selectors``：翻页链接选择器列表（默认
      :data:`DEFAULT_NEXT_SELECTORS`）
    - ``title_selectors``：章节标题选择器列表（默认
      :data:`DEFAULT_TITLE_SELECTORS`）
    - ``min_content_chars``：判定"正文命中"的最小字符数（默认 1）

    **翻页**

    - ``max_pages``：页数上限（默认 1000）
    - ``delay``：页间延时 ``(最小, 最大)`` 秒随机（默认 ``(0.5, 1.5)``）
    - ``retries``：单页最大重试次数（默认 3）
    - ``backoff``：退避基数秒，第 n 次重试等待 ``backoff * 2**(n-1)``（默认 1.0）
    - ``refetch_failed_pages``：收尾阶段对"整页抓取失败"的页做一次浏览器渲染
      二次尝试并补回正文（默认 ``True``；仅在真的出现整页失败时才惰性准备
      浏览器，浏览器不可用时自动跳过，不影响其余结果）

    **抓取方式**

    - ``dynamic``：``True`` 用 Playwright 渲染 JS 站点（默认 ``False``）
    - ``render_fallback``：静态模式下的兜底升级——某页正文为空/过短时，
      自动改用浏览器渲染该页再试一次（默认 ``False``；需 playwright 可用，
      不可用时自动跳过，与 :mod:`core.compliance.scanner` 的做法一致）
    - ``wait_selector``：动态模式下等待正文节点出现的选择器
    - ``scroll`` / ``max_scroll``：动态模式下是否滚动触发懒加载
    - ``timeout``：单页抓取超时秒数（默认 30）

    **网络**

    - ``headers`` / ``cookies`` / ``proxy`` / ``user_agent``
      （统一注入到同一个 session，见 :mod:`core.fetcher`）

    **合规与输出**

    - ``respect_robots``：是否遵守 robots.txt（默认 ``True``）
    - ``chapter_separator``：章节分隔串（默认 ``"\\n\\n"``）
    - ``include_title``：是否在文首写入书名（默认 ``True``）
    - ``book_title``：显式书名（默认取起始页标题）
    - ``meta_path``：元数据 JSON 路径（默认与 ``output_path`` 同名的
      ``*.meta.json``；传空串则不写元数据）
    - ``logger``：自定义 ``logging.Logger``

    :return: 实际写入的纯文本路径（即 ``output_path``）
    :raises EbookFetchError: 参数非法、零正文产出、动态渲染不可用或落盘失败
    """
    # ---------- 参数校验与解析 ----------
    start_url = (start_url or "").strip()
    output_path = (output_path or "").strip()
    if not start_url.startswith(("http://", "https://")):
        raise EbookFetchError(f"起始地址必须是 http(s) 链接：{start_url or '（空）'}")
    if not output_path:
        raise EbookFetchError("必须提供纯文本输出路径 output_path")

    content_selectors = tuple(kwargs.get("content_selectors")
                              or DEFAULT_CONTENT_SELECTORS)
    exclude_selectors = tuple(kwargs.get("exclude_selectors")
                              or DEFAULT_EXCLUDE_SELECTORS)
    next_selectors = tuple(kwargs.get("next_selectors") or DEFAULT_NEXT_SELECTORS)
    title_selectors = tuple(kwargs.get("title_selectors") or DEFAULT_TITLE_SELECTORS)

    max_pages = max(1, int(kwargs.get("max_pages") or 1000))
    delay_low, delay_high = normalize_delay(kwargs.get("delay", (0.5, 1.5)))
    retries = max(0, int(kwargs.get("retries") if kwargs.get("retries") is not None
                         else 3))
    backoff = max(0.1, float(kwargs.get("backoff") or 1.0))

    dynamic = bool(kwargs.get("dynamic", False))
    wait_selector = str(kwargs.get("wait_selector") or "")
    scroll = bool(kwargs.get("scroll", False))
    max_scroll = max(0, int(kwargs.get("max_scroll") or 8))
    timeout = max(1.0, float(kwargs.get("timeout") or 30.0))

    headers = dict(kwargs.get("headers") or {})
    cookies = dict(kwargs.get("cookies") or {})
    proxy = dict(kwargs.get("proxy") or {})
    user_agent = str(kwargs.get("user_agent") or "") or DEFAULT_USER_AGENT

    respect_robots = bool(kwargs.get("respect_robots", True))
    min_content_chars = max(1, int(kwargs.get("min_content_chars") or 1))
    chapter_separator = str(kwargs.get("chapter_separator", "\n\n"))
    include_title = bool(kwargs.get("include_title", True))
    explicit_title = str(kwargs.get("book_title") or "")
    meta_path = kwargs.get("meta_path")
    if meta_path is None:
        meta_path = os.path.splitext(output_path)[0] + ".meta.json"
    meta_path = str(meta_path or "")
    log = kwargs.get("logger")
    if not isinstance(log, logging.Logger):
        log = logger

    # ---------- 统一 session（UA / headers / cookies / proxy） ----------
    request_headers = {"User-Agent": user_agent,
                       "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    if cookies:
        request_headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    request_headers.update({k: v for k, v in headers.items() if v})

    try:
        fetcher = create_fetcher(
            "dynamic" if dynamic else "static",
            user_agent=user_agent, proxy=proxy, extra_headers=request_headers,
            max_retries=0,          # 重试与退避由本模块统一控制
        )
    except FetcherUnavailable as exc:
        raise EbookFetchError(f"动态渲染不可用：{exc}") from exc

    log.info("=== 电子书抓取启动 ===")
    log.info("　起始地址：%s", start_url)
    log.info("　输出文本：%s", output_path)
    log.info("　抓取方式：%s | 页数上限：%s | 页间延时：%.2f~%.2fs | 单页重试：%s 次",
             "浏览器渲染" if dynamic else "静态请求", max_pages,
             delay_low, delay_high, retries)
    if not HAS_BS4:
        log.warning("未安装 beautifulsoup4，已退化为正则解析（复杂 CSS 选择器不可用）")

    render_fallback = bool(kwargs.get("render_fallback", False)) and not dynamic
    # 失败页二次尝试：只在真的出现"整页抓取失败"时才惰性准备浏览器，
    # 浏览器不可用时自动跳过（不视为任务失败）
    refetch_failed = bool(kwargs.get("refetch_failed_pages", True))
    fallback_fetcher = None
    fallback_tried = False

    # ---------- 抓取状态 ----------
    visited: set[str] = set()          # 防死循环：已访问 URL 集合
    known: set[str] = set()            # 已见过的同站链接（印证 URL 页码规律）
    pages: list[PageResult] = []
    chapters: list[dict] = []
    collected: list[tuple[int, str, str]] = []      # (页序, 章节标题, 正文)
    book_title = explicit_title

    def _ensure_fallback():
        """惰性创建渲染兜底抓取器；不可用时返回 None（不视为任务失败）。"""
        nonlocal fallback_fetcher, fallback_tried
        if fallback_tried:
            return fallback_fetcher
        fallback_tried = True
        log.info("　正在准备浏览器内核（首次可能需要解压，请稍候）…")
        try:
            fallback_fetcher = create_fetcher(
                "dynamic", user_agent=user_agent, proxy=proxy,
                extra_headers=request_headers, max_retries=0)
        except FetcherUnavailable as exc:
            log.warning("　渲染兜底不可用（%s），继续使用静态结果", exc)
            fallback_fetcher = None
        return fallback_fetcher

    def _page_text_len(result: PageResult) -> int:
        """当前参数下该页可提取的正文长度（用于比较静态/渲染结果）。"""
        return len(extract_content(result.html, content_selectors,
                                   exclude_selectors, min_content_chars))

    def _accept(result: PageResult) -> bool:
        """从抓取结果解析标题/正文并登记；无正文返回 False。"""
        nonlocal book_title
        chapter_title = extract_title(result.html, title_selectors)
        body = extract_content(result.html, content_selectors, exclude_selectors,
                               min_content_chars)
        if not body.strip():
            result.error = "未提取到正文"
            log.error("　第 %s 页未提取到正文（可尝试 dynamic=True 或指定 "
                      "content_selectors）：%s", result.index, result.url)
            return False
        result.title = chapter_title
        result.chars = len(body)
        collected.append((result.index, chapter_title, body))
        chapters.append({"index": result.index, "title": chapter_title,
                         "url": result.url, "chars": result.chars})
        if not book_title and chapter_title:
            book_title = chapter_title
        log.info("　✓ %s | 正文 %s 字符 | 下一页：%s",
                 chapter_title or "（无标题）", result.chars,
                 result.next_url or "（无）")
        return True

    @dataclass
    class _Step:
        """一次"抓取 + 解析 + 登记"的结果（主循环与收尾二次尝试共用）。"""

        result: PageResult
        next_url: str = ""
        recovered: bool = False        # 是否成功登记了正文

    def _step(url: str, handle, use_dynamic: bool, index: int = 0) -> "_Step":
        """抓取并登记一页：解析标题/正文/下一页，返回下一步线索。"""
        page_index = index or (len(pages) + 1)
        result = _fetch_page(
            handle, url, dynamic=use_dynamic, wait_selector=wait_selector,
            scroll=scroll, max_scroll=max_scroll, timeout=timeout,
            retries=retries, backoff=backoff, page_index=page_index, log=log)
        if not result.fetched:
            return _Step(result=result)

        # 先收集本页同站链接（供"URL 页码规律"印证），再定位下一页
        known.update(_collect_site_links(result.html, result.url))
        result.next_url = _compute_next_url(result, next_selectors, known, log)

        # 静态正文过短（SPA / JS 注入站点）：可选升级为浏览器渲染再试一次
        if render_fallback and _page_text_len(result) < min_content_chars:
            fallback = _ensure_fallback()
            if fallback is not None:
                log.info("　静态正文过短，改用浏览器渲染重试：%s", url)
                rendered = _fetch_page(
                    fallback, url, dynamic=True, wait_selector=wait_selector,
                    scroll=scroll, max_scroll=max_scroll, timeout=timeout,
                    retries=retries, backoff=backoff, page_index=page_index, log=log)
                if rendered.fetched:
                    known.update(_collect_site_links(rendered.html, rendered.url))
                    if _page_text_len(rendered) > _page_text_len(result):
                        rendered.next_url = _compute_next_url(
                            rendered, next_selectors, known, log)
                        result = rendered
                        log.info("　渲染兜底取到更长正文（%s 字符）",
                                 _page_text_len(rendered))

        return _Step(result=result, next_url=result.next_url,
                     recovered=_accept(result))

    try:
        # ---------- robots.txt ----------
        robots_parser = None
        if respect_robots:
            robots_parser, note = load_robots_parser(fetcher, start_url)
            log.info("　%s", note)
        else:
            log.warning("　已关闭 robots.txt 校验（请自行确认站点服务条款与授权）")

        # ---------- 自动翻页主循环 ----------
        url = start_url
        stop_reason = ""
        while url:
            if url in visited:
                stop_reason = f"下一页 URL 已访问过（防死循环）：{url}"
                log.warning("　%s", stop_reason)
                break
            if len(pages) >= max_pages:
                stop_reason = f"已达页数上限 {max_pages}，停止翻页"
                log.warning("　%s", stop_reason)
                break
            visited.add(url)

            # robots 禁止：跳过该页（记 ERROR）并结束本轮，因为被禁页面无法提供下一页线索
            if respect_robots and not _robots_allows(robots_parser, url, user_agent):
                log.error("第 %s 页被 robots.txt 禁止访问，已跳过：%s",
                          len(pages) + 1, url)
                pages.append(PageResult(index=len(pages) + 1, url=url,
                                        error="robots.txt 禁止访问"))
                stop_reason = "当前页被 robots.txt 禁止访问"
                break

            # 页间随机延时（首页不等待）
            if pages and (delay_low or delay_high):
                wait = random.uniform(delay_low, delay_high)
                log.info("　等待 %.2fs 后继续…", wait)
                interruptible_sleep(wait)

            log.info("[第 %s 页] 正在抓取：%s", len(pages) + 1, url)
            step = _step(url, fetcher, dynamic)
            pages.append(step.result)

            if not step.result.fetched:
                # 失败页记 ERROR 后进入收尾阶段；无 HTML 即无翻页线索，无法继续本轮
                log.error("　第 %s 页抓取失败，未能取得页面内容：%s",
                          step.result.index, url)
                stop_reason = f"第 {step.result.index} 页抓取失败"
                break

            if not step.recovered:                 # 正文为空但拿到了下一页
                log.warning("　该页正文为空，继续跟随下一页")
            if not step.next_url:
                stop_reason = (f"第 {step.result.index} 页正文为空且无下一页"
                               if not step.recovered else "未找到下一页元素，已到末页")
                break
            url = step.next_url

        # ---------- 收尾：对"整页抓取失败"的页做渲染二次尝试，并续爬 ----------
        failed = [p for p in pages if not p.ok and "未提取到正文" not in p.error]
        resume_url = ""
        if refetch_failed and failed:
            fallback = _ensure_fallback()
            if fallback is not None:
                log.warning("　对 %s 个失败页做渲染二次尝试（共 %s 页）",
                            len(failed), len(pages))
                recovered = 0
                for old in failed:
                    if delay_low or delay_high:
                        interruptible_sleep(random.uniform(delay_low, delay_high))
                    step = _step(old.url, fallback, True, index=old.index)
                    if not step.result.fetched or not step.recovered:
                        continue
                    pages[pages.index(old)] = step.result
                    recovered += 1
                    if not resume_url and step.next_url:
                        # 最早补回的页天然衔接后续章节，从它继续把书抓完
                        resume_url = step.next_url
                        stop_reason = ""
                if recovered:
                    # 补回的页序与原始页序可能交错，落盘前按页序号重排
                    collected.sort(key=lambda item: item[0])
                    chapters.sort(key=lambda item: item["index"])
                    log.info("　二次尝试补回 %s 页正文", recovered)
                else:
                    log.warning("　二次尝试未能补回任何正文")

        # ---------- 续爬：从最早补回的页之后把剩余章节抓完 ----------
        while resume_url:
            if resume_url in visited:
                stop_reason = f"下一页 URL 已访问过（防死循环）：{resume_url}"
                log.warning("　%s", stop_reason)
                break
            if len(pages) >= max_pages:
                stop_reason = f"已达页数上限 {max_pages}，停止翻页"
                log.warning("　%s", stop_reason)
                break
            visited.add(resume_url)
            if respect_robots and not _robots_allows(robots_parser, resume_url,
                                                     user_agent):
                log.error("续爬页被 robots.txt 禁止访问，已停止：%s", resume_url)
                stop_reason = "续爬页被 robots.txt 禁止访问"
                break
            if delay_low or delay_high:
                interruptible_sleep(random.uniform(delay_low, delay_high))
            log.info("[续爬第 %s 页] %s", len(pages) + 1, resume_url)
            step = _step(resume_url, fetcher, dynamic)
            pages.append(step.result)
            if not step.result.fetched:
                log.error("　续爬页抓取失败：%s", resume_url)
                stop_reason = f"续爬页抓取失败：{resume_url}"
                break
            if not step.recovered:
                log.warning("　续爬页正文为空，继续跟随下一页")
            if not step.next_url:
                stop_reason = "未找到下一页元素，已到末页"
                break
            resume_url = step.next_url
    finally:
        for handle in (fetcher, fallback_fetcher):
            if handle is None:
                continue
            try:
                handle.close()
            except Exception:
                pass

    # ---------- 零正文产出：明确失败，不落盘空文件 ----------
    if not collected:
        causes = {p.error for p in pages if p.error}
        if any("robots" in cause for cause in causes):
            raise EbookFetchError(
                f"起始页被 robots.txt 禁止访问，未抓取任何内容：{start_url}"
                "（如确已获得站点授权，可传 respect_robots=False）")
        if causes:
            raise EbookFetchError(
                f"起始页抓取失败，未取得任何正文：{start_url}"
                f"（共尝试 {len(pages)} 页；原因：{'、'.join(sorted(causes))}）")
        raise EbookFetchError(
            "未从起始页提取到任何正文："
            f"{start_url}（可尝试 dynamic=True，或用 content_selectors 指定正文容器）")

    # ---------- 拼装与落盘 ----------
    text_body = _compose_book(book_title, collected, include_title=include_title,
                              separator=chapter_separator)
    try:
        _write_text(output_path, text_body)
    except OSError as exc:
        raise EbookFetchError(f"写入纯文本失败：{output_path}（{exc}）") from exc
    log.info("纯文本已写入：%s（%s 字符，%s 页）",
             output_path, len(text_body), len(collected))

    if meta_path:
        meta = {
            "book_title": book_title or "",
            "source_url": start_url,
            "output_path": os.path.abspath(output_path),
            "page_count": len(pages),
            "success_pages": len(collected),
            "failed_pages": len([p for p in pages if not p.ok]),
            "total_chars": len(text_body),
            "chapters": chapters,
            "pages": [p.to_dict() for p in pages],
            "fetch_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "fetch_mode": "dynamic" if dynamic else "static",
            "stop_reason": stop_reason,
            "respect_robots": respect_robots,
            "max_pages": max_pages,
            "delay_seconds": [delay_low, delay_high],
            "retries": retries,
            "crawler_user_agent": CRAWLER_USER_AGENT,
            "generator": LOGGER_NAME,
        }
        try:
            _write_json(meta_path, meta)
            log.info("元数据已写入：%s", meta_path)
        except OSError as exc:
            # 元数据写失败不影响正文成果
            log.error("元数据写入失败：%s（%s）", meta_path, exc)

    log.info("=== 抓取结束：成功 %s 页 / 共尝试 %s 页 | 停止原因：%s ===",
             len(collected), len(pages), stop_reason or "正常结束")
    return output_path


def _compose_book(book_title: str, collected: list[tuple[int, str, str]], *,
                  include_title: bool = True, separator: str = "\n\n") -> str:
    """把逐页正文按章节拼装为整本书文本（``collected`` 元素为 ``(页序, 标题, 正文)``）。"""
    blocks: list[str] = []
    if include_title and book_title:
        blocks.append(f"《{book_title.strip('《》').strip()}》")
    last_title = ""
    for _index, chapter_title, body in collected:
        heading = clean_text(chapter_title).split("\n", 1)[0].strip()
        # 标题变化才写入，避免同一章节名在页首逐页重复
        if heading and heading != last_title and not body.startswith(heading):
            blocks.append(heading)
            last_title = heading
        blocks.append(body)
    return separator.join(block for block in blocks if block).strip() + "\n"


def _write_text(path: str, content: str) -> None:
    """UTF-8 写入纯文本（自动创建父目录，统一 ``\\n`` 换行）。"""
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)


def _write_json(path: str, payload: dict) -> None:
    """UTF-8 写入 JSON 元数据（``ensure_ascii=False`` 保留中文）。"""
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


__all__ = [
    "EbookFetchError", "PageResult", "fetch_book", "extract_content",
    "extract_title", "extract_next_url", "clean_text", "normalize_delay",
    "load_robots_parser", "DEFAULT_CONTENT_SELECTORS",
    "DEFAULT_EXCLUDE_SELECTORS", "DEFAULT_NEXT_SELECTORS",
    "DEFAULT_TITLE_SELECTORS", "DEFAULT_USER_AGENT", "LOGGER_NAME",
]


if __name__ == "__main__":
    # 最小可运行示例（在项目根目录执行）：
    #   python -m core.ebook_fetcher https://example.com/book/1.html output/book.txt
    #   python -m core.ebook_fetcher <url> output/book.txt --dynamic   # JS 渲染站点
    #   python -m core.ebook_fetcher <url> output/book.txt \
    #       --content-selector "#content" --delay 0.5 1.5 --max-pages 500
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="抓取电子书正文并自动翻页落盘")
    parser.add_argument("start_url", help="起始阅读页 URL")
    parser.add_argument("output_path", help="纯文本输出路径")
    parser.add_argument("--dynamic", action="store_true",
                        help="使用 Playwright 渲染（适配 JS 站点）")
    parser.add_argument("--max-pages", type=int, default=1000, help="页数上限")
    parser.add_argument("--delay", type=float, nargs=2, default=(0.5, 1.5),
                        metavar=("MIN", "MAX"), help="页间随机延时区间（秒）")
    parser.add_argument("--content-selector", action="append", default=[],
                        help="正文容器选择器（可重复指定，覆盖默认）")
    parser.add_argument("--no-robots", action="store_true",
                        help="跳过 robots.txt 校验（请自行确认站点条款）")
    args = parser.parse_args()

    try:
        saved = fetch_book(
            args.start_url, args.output_path,
            dynamic=args.dynamic,
            max_pages=args.max_pages,
            delay=tuple(args.delay),
            content_selectors=tuple(args.content_selector) or None,
            respect_robots=not args.no_robots,
        )
    except EbookFetchError as exc:
        logger.error("抓取失败：%s", exc)
        sys.exit(1)
    print(f"已保存：{saved}")
