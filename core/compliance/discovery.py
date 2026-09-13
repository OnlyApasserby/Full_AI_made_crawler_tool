"""公开法律/政策页面的发现。

只找"站点自己公开摆出来的"页面，三条来源合并后按可靠性排序：

1. **页脚/首页同站链接**（锚文本含"服务条款/隐私政策/legal/terms"等）
   ——最可靠：站点亲自把入口放在页脚，说明这就是它想让用户看到的条款页。
2. **sitemap.xml**（含 sitemap 索引）——覆盖面广，但需要按 URL 特征筛。
3. **内置路径词典**——常见固定路径（``/terms``、``/privacy`` …），
   数量有限、逐条礼貌请求，用于兜底。

礼貌与安全约束（都在这里落实）：
- 仅同 host（可关闭）；不跟随站外跳转
- 遵守 robots.txt（``COMPLIANCE_RESPECT_ROBOTS``），被禁路径直接跳过并记录原因
- 请求间隔由抓取器统一控制（``request_delay`` + 抖动）
- 只做有限探测，不做目录爆破——路径词典是固定小集合，不是字典攻击
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse, urlunparse

# ---------------------------------------------------------------------------
# 内置路径词典（固定小集合，用于兜底；不是目录爆破字典）
# ---------------------------------------------------------------------------
LEGAL_PATH_CANDIDATES = (
    "/terms", "/terms-of-service", "/terms-of-use", "/terms.html",
    "/term", "/tos", "/service-terms", "/user-agreement", "/agreement",
    "/privacy", "/privacy-policy", "/privacy-statement", "/privacy.html",
    "/policy", "/policies", "/legal", "/legal-notice", "/legal/terms",
    "/legal/privacy", "/disclaimer", "/copyright", "/cookie-policy",
    "/cookies", "/about/terms", "/about/privacy", "/help/terms",
    "/api-terms", "/developer-terms", "/dpa", "/eula",
)

#: 锚文本线索（中文）
ANCHOR_HINTS_ZH = (
    "服务条款", "使用条款", "用户协议", "用户服务协议", "服务协议", "服务规则",
    "隐私政策", "隐私权政策", "隐私声明", "隐私保护", "个人信息保护",
    "法律声明", "法律信息", "免责声明", "版权声明", "版权政策", "知识产权",
    "条款与条件", "条款和条件", "使用规则", "社区规范", "用户须知",
    "开发者协议", "开放平台协议", "数据使用", "机器人协议", "网站声明",
)
#: 锚文本线索（英文）
ANCHOR_HINTS_EN = (
    "terms", "terms of service", "terms of use", "terms & conditions",
    "user agreement", "eula", "privacy", "privacy policy",
    "privacy statement", "legal", "legal notice", "disclaimer", "copyright",
    "cookie policy", "acceptable use", "fair use", "data policy",
    "developer terms", "api terms", "robots", "dpa",
)

#: URL 路径线索（大小写不敏感）
URL_HINTS = (
    "term", "privacy", "legal", "policy", "policies", "agreement",
    "disclaimer", "copyright", "cookie", "eula", "tos", "dpa",
    "条款", "协议", "隐私", "声明", "政策", "规范", "须知", "免责",
)

#: 发现来源标记（同时用作排序权重）
SOURCE_FOOTER = "页脚链接"
SOURCE_SITEMAP = "sitemap"
SOURCE_PATH = "路径词典"
SOURCE_HINT = "URL 特征"

_SOURCE_RANK = {
    SOURCE_FOOTER: 3,
    SOURCE_SITEMAP: 2,
    SOURCE_PATH: 1,
    SOURCE_HINT: 1,
}

#: 非 HTML 页面（无法用文本提取解析，只登记原因，不强行读二进制）
NON_HTML_EXT = (
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".zip",
    ".rar", ".7z", ".tar", ".gz", ".png", ".jpg", ".jpeg", ".gif", ".svg",
    ".webp", ".mp4", ".mp3", ".css", ".js", ".json", ".xml", ".csv",
)

#: sitemap 解析时的 URL 数量上限（避免超大 sitemap 拖慢扫描）
SITEMAP_MAX_URLS = 3000
#: sitemap 索引递归层数上限
SITEMAP_MAX_DEPTH = 1
#: 跳过清单的展示上限（超出部分只计数，避免报告被海量 robots 拒绝项淹没）
MAX_SKIPPED = 50

_ANCHOR_RE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.IGNORECASE | re.DOTALL)
_HREF_RE = re.compile(r"""href\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.IGNORECASE)
_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class LegalSeed:
    """一个候选法律页面（尚未抓取正文）。"""

    url: str
    source: str = SOURCE_HINT
    anchor: str = ""

    @property
    def rank(self) -> int:
        return _SOURCE_RANK.get(self.source, 0)

    def to_dict(self) -> dict:
        return {"url": self.url, "source": self.source, "anchor": self.anchor}


# ---------------------------------------------------------------------------
# URL 工具
# ---------------------------------------------------------------------------
def normalize_url(url: str) -> str:
    """归一化 URL 用于去重：去 fragment、统一 host 小写、去末尾斜杠。"""
    if not url:
        return ""
    parsed = urlparse(url.strip())
    host = (parsed.netloc or "").lower()
    path = parsed.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return urlunparse((parsed.scheme.lower(), host, path, "", parsed.query, ""))


def same_host(url_a: str, url_b: str) -> bool:
    """两个 URL 是否同 host（忽略大小写与 www. 前缀差异）。"""
    def _host(value: str) -> str:
        host = (urlparse(value).netloc or "").lower().split(":")[0]
        return host[4:] if host.startswith("www.") else host
    host_a, host_b = _host(url_a), _host(url_b)
    return bool(host_a) and host_a == host_b


def looks_like_legal_url(url: str) -> str:
    """按 URL 特征判断是否像法律/政策页，返回命中的线索词（否则空串）。"""
    path = (urlparse(url).path or "").lower()
    if not path or path == "/":
        return ""
    for hint in URL_HINTS:
        if hint in path:
            return hint
    return ""


def is_non_html(url: str) -> bool:
    """URL 是否指向无法做文本提取的非 HTML 资源。"""
    path = (urlparse(url).path or "").lower()
    return path.endswith(NON_HTML_EXT)


# ---------------------------------------------------------------------------
# 锚文本与链接提取（bs4 优先，缺失时用正则兜底）
# ---------------------------------------------------------------------------
def _iter_anchors_regex(html_text: str) -> list:
    results = []
    for match in _ANCHOR_RE.finditer(html_text or ""):
        attrs, inner = match.group(1), match.group(2)
        href_match = _HREF_RE.search(attrs)
        if not href_match:
            continue
        href = next((g for g in href_match.groups() if g), "")
        text = html.unescape(_TAG_RE.sub(" ", inner or ""))
        text = re.sub(r"\s+", " ", text).strip()
        if href:
            results.append((href, text))
    return results


def _iter_anchors_bs4(html_text: str) -> list:
    try:
        from bs4 import BeautifulSoup  # 可选依赖
    except Exception:
        return []
    try:
        soup = BeautifulSoup(html_text or "", "lxml")
    except Exception:
        try:
            soup = BeautifulSoup(html_text or "", "html.parser")
        except Exception:
            return []
    results = []
    for anchor in soup.find_all("a", href=True):
        text = anchor.get_text(" ", strip=True)
        results.append((str(anchor.get("href") or ""), text))
    return results


def iter_anchors(html_text: str) -> list:
    """提取页面全部 (href, 锚文本) 对；优先使用 bs4，缺失时正则兜底。"""
    anchors = _iter_anchors_bs4(html_text)
    return anchors if anchors else _iter_anchors_regex(html_text)


def anchor_hint(text: str) -> str:
    """锚文本命中的线索词（中文优先），未命中返回空串。"""
    lowered = (text or "").strip().lower()
    if not lowered:
        return ""
    for hint in ANCHOR_HINTS_ZH:
        if hint in lowered:
            return hint
    for hint in ANCHOR_HINTS_EN:
        if hint in lowered:
            return hint
    return ""


def parse_sitemap_locs(xml_text: str) -> list:
    """从 sitemap(索引) 中取出 ``<loc>`` 列表。"""
    return [html.unescape(m.group(1)) for m in _LOC_RE.finditer(xml_text or "")]


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
class LegalPageDiscovery:
    """发现目标站点的公开法律/政策页面。

    用法::

        discovery = LegalPageDiscovery(fetcher, max_pages=12, log=print)
        seeds, skipped = discovery.discover(start_url, robots_parser=parser,
                                           user_agent="crawler-tool")

    :param fetcher: :class:`core.fetcher.base.BaseFetcher` 实例（统一携带
        UA 池、代理、请求间隔与停止标志）
    """

    def __init__(self, fetcher, *, max_pages: int = 12, same_host_only: bool = True,
                 respect_robots: bool = True, log=None):
        self.fetcher = fetcher
        self.max_pages = max(1, int(max_pages or 0) or 12)
        self.same_host_only = bool(same_host_only)
        self.respect_robots = bool(respect_robots)
        self.log = log or (lambda _msg: None)
        self.skipped: list = []
        self.skipped_total = 0
        self._skipped_keys: set = set()
        self._robots_parser = None
        self._user_agent = ""

    # ---------- 对外 ----------
    def discover(self, start_url: str, *, robots_parser=None,
                 user_agent: str = "") -> tuple:
        """发现候选法律页面。

        :return: (seeds, skipped)；seeds 为 :class:`LegalSeed` 列表（已排序去重）
        """
        self._robots_parser = robots_parser
        self._user_agent = user_agent
        self.skipped = []
        self.skipped_total = 0
        self._skipped_keys = set()

        home = self._fetch_text(start_url, what="起始页")
        seeds: dict[str, LegalSeed] = {}
        if home is not None:
            self._collect_from_anchors(home, start_url, seeds)
        self._collect_from_sitemap(start_url, seeds)
        self._collect_from_path_dict(start_url, seeds)

        ordered = sorted(
            seeds.values(),
            key=lambda seed: (-seed.rank, len(seed.url), seed.url))
        picked = ordered[:self.max_pages]
        for extra in ordered[self.max_pages:]:
            self.skipped.append({"url": extra.url, "source": extra.source,
                                 "reason": f"超出页面数上限（{self.max_pages}）"})
        return picked, list(self.skipped)

    # ---------- 内部：抓取 ----------
    def _fetch_text(self, url: str, *, what: str = "") -> str | None:
        """礼貌抓取一个页面并返回 HTML 文本；失败返回 None（原因记入 skipped）。"""
        if not url:
            return None
        if not self._allowed(url):
            return None
        try:
            ctx = self.fetcher.fetch(url)
        except Exception as exc:                      # 抓取器内部已做降级，这里兜底
            self._skip(url, f"{what}抓取异常：{type(exc).__name__}")
            return None
        if ctx.error or not ctx.html:
            self._skip(url, f"{what}抓取失败：{ctx.error or '无内容'}")
            return None
        return ctx.html

    def _allowed(self, url: str) -> bool:
        """robots.txt 校验：被禁路径直接跳过（扫描自身也守规矩）。"""
        if self.respect_robots and self._robots_parser is not None:
            try:
                if self._robots_parser.is_disallowed(url, self._user_agent):
                    self._skip(url, "robots.txt 禁止访问该路径")
                    return False
            except Exception:
                pass
        return True

    def _skip(self, url: str, reason: str) -> None:
        key = (url, reason)
        if key in self._skipped_keys:
            return
        self._skipped_keys.add(key)
        self.skipped_total += 1
        if len(self.skipped) < MAX_SKIPPED:
            self.skipped.append({"url": url, "reason": reason})
        self.log(f"跳过 {url}：{reason}")

    # ---------- 内部：三条来源 ----------
    def _add(self, seeds: dict, url: str, source: str, anchor: str = "",
             base_url: str = "") -> None:
        absolute = urljoin(base_url or url, (url or "").strip())
        if not absolute.lower().startswith(("http://", "https://")):
            return
        # 站外条款入口（站点把条款放在别的域名）值得记录：说明"这里没扫到，是有原因的"
        if self.same_host_only and base_url and not same_host(absolute, base_url):
            self._skip(absolute, "站外链接（本次不扫描跨域页面）")
            return
        if is_non_html(absolute):
            self._skip(absolute, "非 HTML 资源（PDF/文档/媒体），无法做文本提取")
            return
        # 入队前先过 robots：被禁的路径连候选都不提，避免"先发现再否决"
        if not self._allowed(absolute):
            return
        key = normalize_url(absolute)
        if not key:
            return
        existing = seeds.get(key)
        if existing and existing.rank >= _SOURCE_RANK.get(source, 0):
            return
        seeds[key] = LegalSeed(url=key, source=source, anchor=anchor)

    def _collect_from_anchors(self, html_text: str, page_url: str,
                              seeds: dict) -> None:
        """从首页/页脚的同站链接中筛出条款入口。"""
        found = 0
        for href, anchor in iter_anchors(html_text):
            if not href or href.lower().startswith(
                    ("javascript:", "mailto:", "tel:", "#")):
                continue
            hint = anchor_hint(anchor)
            url_hint = looks_like_legal_url(href)
            if not hint and not url_hint:
                continue
            before = len(seeds)
            self._add(seeds, href, SOURCE_FOOTER if hint else SOURCE_HINT,
                      anchor=anchor[:80], base_url=page_url)
            if len(seeds) > before:
                found += 1
        if found:
            self.log(f"从页面链接中发现 {found} 个疑似条款入口")

    def _collect_from_sitemap(self, start_url: str, seeds: dict) -> None:
        """读取 sitemap.xml（含一层索引）并按 URL 特征筛条款页。"""
        parsed = urlparse(start_url)
        root = f"{parsed.scheme}://{parsed.netloc}"
        queue = [urljoin(root, "/sitemap.xml")]
        seen_maps: set[str] = set()
        total = 0
        depth = 0
        while queue and depth <= SITEMAP_MAX_DEPTH and total < SITEMAP_MAX_URLS:
            nxt: list = []
            for sitemap_url in queue:
                if sitemap_url in seen_maps or total >= SITEMAP_MAX_URLS:
                    continue
                seen_maps.add(sitemap_url)
                xml_text = self._fetch_text(sitemap_url, what="sitemap")
                if not xml_text:
                    continue
                for loc in parse_sitemap_locs(xml_text):
                    if total >= SITEMAP_MAX_URLS:
                        break
                    total += 1
                    if loc.lower().endswith((".xml", ".xml.gz")):
                        nxt.append(loc)
                        continue
                    if looks_like_legal_url(loc):
                        self._add(seeds, loc, SOURCE_SITEMAP, base_url=root)
            queue = nxt
            depth += 1
        if total:
            self.log(f"sitemap 中共检查 {total} 条 URL")

    def _collect_from_path_dict(self, start_url: str, seeds: dict) -> None:
        """按内置路径词典补足（仅登记候选，抓取由编排层统一进行）。"""
        for path in LEGAL_PATH_CANDIDATES:
            self._add(seeds, path, SOURCE_PATH, base_url=start_url)


def discover_legal_pages(start_url: str, fetcher, *, max_pages: int = 12,
                         same_host_only: bool = True, respect_robots: bool = True,
                         robots_parser=None, user_agent: str = "",
                         log=None) -> tuple:
    """便捷函数：等价于构造 :class:`LegalPageDiscovery` 后调用 ``discover``。"""
    discovery = LegalPageDiscovery(
        fetcher, max_pages=max_pages, same_host_only=same_host_only,
        respect_robots=respect_robots, log=log)
    return discovery.discover(start_url, robots_parser=robots_parser,
                              user_agent=user_agent)


__all__ = [
    "LegalSeed", "LegalPageDiscovery", "discover_legal_pages",
    "LEGAL_PATH_CANDIDATES", "ANCHOR_HINTS_ZH", "ANCHOR_HINTS_EN", "URL_HINTS",
    "SOURCE_FOOTER", "SOURCE_SITEMAP", "SOURCE_PATH", "SOURCE_HINT",
    "normalize_url", "same_host", "looks_like_legal_url", "is_non_html",
    "iter_anchors", "anchor_hint", "parse_sitemap_locs",
]
