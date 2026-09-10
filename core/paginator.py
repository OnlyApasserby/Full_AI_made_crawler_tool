"""翻页策略：图集 / 列表页的"下一页"发现。

为什么单独成模块
----------------
图集抓不全的头号原因是**翻页没跟上**：第一页只有 20 张，后面的图片地址
必须通过"下一页"按钮、``?page=2`` 参数或滚动加载接口才能拿到。
这里把翻页抽象成三种策略，统一产出"下一页候选 URL"：

===================  ==========================================  ==================
策略                  判定依据                                     适用
===================  ==========================================  ==================
``next_link``         页面里的"下一页/Next/›/加载更多"链接          常规分页导航
``page_param``        URL 中的 ``?page=2`` / ``/list_2.html``       数字页码、伪静态
``api_offset``        规则里显式给出的接口模板                       XHR 分页接口
===================  ==========================================  ==================

``auto``（默认）只做**有信号**的推断：先找"下一页"链接；没有链接时，
从页面内其它同类链接里推断页码规律（如同时出现 ``?page=2``、``?page=3``
则继续 ``?page=4``）。**绝不凭猜测拼接参数**，避免产生大量 404 噪声。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

STRATEGY_AUTO = "auto"
STRATEGY_NEXT = "next_link"
STRATEGY_PAGE_PARAM = "page_param"
STRATEGY_API_OFFSET = "api_offset"

#: "下一页"链接的文本特征
_NEXT_TEXT_RE = re.compile(
    r"(下一页|下页|下一张|下张|后一页|next\s*(?:page)?|more|load\s*more|"
    r"加载更多|查看更多|›|»|→|>>)", re.IGNORECASE)
#: 页码查询参数候选名（值必须是纯数字）
_PAGE_PARAMS = ("page", "p", "pg", "pn", "pageno", "page_no", "pageindex",
                "page_index", "pageid", "paged", "pagenum", "pagenumber", "pager")
#: 路径末段形如 list_2.html / photo-3 / page4 时的数字提取
_PATH_NUM_RE = re.compile(r"^(.*?)(\d+)([^\d]*)$")
#: 媒体直链扩展名（"下一页"若指向媒体文件，则不是翻页链接）
_MEDIA_LIKE_RE = re.compile(
    r"\.(?:jpg|jpeg|png|gif|webp|bmp|avif|svg|mp4|m3u8|mpd|mp3|m4a|ts|zip)(?:[?#]|$)",
    re.IGNORECASE)


@dataclass
class PageRule:
    """翻页规则（P4 的规则提取器可直接从 JSON 规则构造本对象）。"""

    strategy: str = STRATEGY_AUTO
    template: str = ""          # api_offset / page_param 显式模板，含 {n} 占位符
    param: str = ""             # page_param 策略的参数名（留空则自动推断）
    start: int = 1              # 起始页码（模板 {n} 的第一个取值）
    step: int = 1               # 步长
    max_pages: int = 0          # 翻页上限（0=使用 MediaConfig.max_pages）


def _host_key(url: str) -> str:
    """取用于"同站判定"的主机名（去掉 www. 前缀并转小写）。"""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _same_site(left: str, right: str) -> bool:
    """两个 URL 是否属于同一站点（忽略 www 前缀，允许子域到主域的差异）。"""
    host_left, host_right = _host_key(left), _host_key(right)
    if not host_left or not host_right:
        return False
    return (host_left == host_right
            or host_left.endswith("." + host_right)
            or host_right.endswith("." + host_left))


def _clean(url: str, base: str) -> str:
    """转绝对地址并去掉 fragment（同一页的锚点不应算作新页面）。"""
    text = (url or "").strip()
    if not text or text.lower().startswith(("javascript:", "mailto:", "#", "data:")):
        return ""
    try:
        absolute = urljoin(base, text)
    except ValueError:
        return ""
    if not absolute.startswith(("http://", "https://")):
        return ""
    return absolute.split("#", 1)[0]


class Paginator:
    """翻页候选生成器（一次媒体抓取任务一个实例）。"""

    def __init__(self, rule: PageRule | None = None, max_pages: int = 5):
        self.rule = rule or PageRule()
        self.max_pages = max(0, int(max_pages if max_pages is not None
                                    else (rule.max_pages if rule else 5)))

    # ---------- 对外入口 ----------
    def next_pages(self, ctx, visited=None, page_index: int = 1) -> list:
        """返回下一页候选 URL 列表（已去重、剔除已访问项，通常为 0~2 条）。"""
        visited = visited or set()
        base = ctx.base_url
        candidates: list = []
        strategy = self.rule.strategy or STRATEGY_AUTO
        if strategy in (STRATEGY_AUTO, STRATEGY_NEXT):
            candidates.extend(self._from_next_link(ctx, base))
        if strategy in (STRATEGY_AUTO, STRATEGY_PAGE_PARAM):
            candidates.extend(self._from_page_number(ctx, base, page_index))
        if strategy == STRATEGY_API_OFFSET and self.rule.template:
            candidates.append(self._from_template(page_index))

        ordered: list = []
        for url in candidates:
            url = (url or "").split("#", 1)[0]
            if not url or url in visited or url == base:
                continue
            if _MEDIA_LIKE_RE.search(url):     # 指向媒体文件，不是翻页
                continue
            if not _same_site(url, base):      # 跨站翻页风险高，不跟随
                continue
            if url not in ordered:
                ordered.append(url)
        return ordered[:2]

    def page_limit(self) -> int:
        """生效的翻页上限。"""
        return self.rule.max_pages or self.max_pages

    # ---------- 策略一：下一页链接 ----------
    def _from_next_link(self, ctx, base: str) -> list:
        html = getattr(ctx, "html", "") or ""
        if not html:
            return []
        found: list = []
        # rel="next" 是标准声明，优先级最高
        for match in re.finditer(r"""<a\b[^>]*\brel\s*=\s*["']next["'][^>]*>""",
                                 html, re.IGNORECASE):
            href = re.search(r"""href\s*=\s*["']([^"']+)["']""", match.group(0),
                             re.IGNORECASE)
            if href:
                found.append(_clean(href.group(1), base))
        # 文本含"下一页/Next/›"的链接
        for match in re.finditer(r"<a\b[^>]*>(.*?)</a>", html,
                                 re.IGNORECASE | re.DOTALL):
            text = re.sub(r"<[^>]+>", "", match.group(1)).strip()
            if not text or not _NEXT_TEXT_RE.search(text):
                continue
            href = re.search(r"""href\s*=\s*["']([^"']+)["']""", match.group(0),
                             re.IGNORECASE)
            if href:
                found.append(_clean(href.group(1), base))
        return [url for url in found if url]

    # ---------- 策略二：数字页码 ----------
    def _from_page_number(self, ctx, base: str, page_index: int) -> list:
        if self.rule.template:
            return [self._from_template(page_index)]
        param, current = self._current_page(base)
        if param and current:
            return [self._with_param(base, param, current + self.rule.step)]
        return self._infer_numbering(ctx, base)

    def _current_page(self, base: str) -> tuple:
        """当前 URL 是否已带页码参数：返回 (参数名, 当前值)。"""
        try:
            pairs = parse_qsl(urlparse(base).query, keep_blank_values=True)
        except ValueError:
            return "", 0
        preferred = [self.rule.param] if self.rule.param else []
        for name in preferred + list(_PAGE_PARAMS):
            if not name:
                continue
            for key, value in pairs:
                if key.lower() == name and value.isdigit():
                    return key, int(value)
        return "", 0

    def _infer_numbering(self, ctx, base: str) -> list:
        """从页面内同类链接推断页码规律（有信号才推断，不猜）。

        两种信号：
        - 同路径的 ``?page=N``（含其它可变参数一致）
        - 同一路径末段的数字递增（``/list_2.html``、``/photo/3``）

        取值规则：当前 URL **自身带页码**时续接 ``自身 + step``；
        自身无页码时取页面内出现的最小页码（通常就是第 2 页），
        避免用"最大页码 + 1"直接跳到最后。
        """
        html = getattr(ctx, "html", "") or ""
        if not html:
            return []
        base_path = urlparse(base).path
        base_hit = _split_trailing_number(base_path)
        candidates: list = []
        param_values: dict = {}
        path_numbers: list = []
        for match in re.finditer(r"""href\s*=\s*["']([^"']+)["']""", html,
                                 re.IGNORECASE):
            url = _clean(match.group(1), base)
            if not url or not _same_site(url, base):
                continue
            parsed = urlparse(url)
            # 信号A：同路径 + 数字型页码参数
            if parsed.path == base_path:
                try:
                    pairs = parse_qsl(parsed.query, keep_blank_values=True)
                except ValueError:
                    pairs = []
                for key, value in pairs:
                    if key.lower() in _PAGE_PARAMS and value.isdigit():
                        param_values.setdefault(key, set()).add(int(value))
            # 信号B：路径末段数字递增
            hit = _split_trailing_number(parsed.path)
            if hit and base_hit and hit[0] == base_hit[0] and hit[2] == base_hit[2]:
                path_numbers.append(hit[1])

        step = self.rule.step
        if param_values:
            current = _current_query_page(base)
            for param, values in param_values.items():
                candidates.append(self._with_param(
                    base, param, _pick_next(values, current, step)))
        if path_numbers:
            base_number = base_hit[1] if base_hit else 0
            next_number = _pick_next(set(path_numbers), base_number, step)
            prefix, _number, suffix = base_hit
            candidates.append(_replace_path(base, f"{prefix}{next_number}{suffix}"))
        return [url for url in candidates if url]

    @staticmethod
    def _with_param(url: str, param: str, value: int) -> str:
        """替换/追加查询参数，返回新 URL。"""
        parsed = urlparse(url)
        try:
            pairs = parse_qsl(parsed.query, keep_blank_values=True)
        except ValueError:
            pairs = []
        replaced = False
        output = []
        for key, old in pairs:
            if key.lower() == param.lower():
                output.append((key, str(value)))
                replaced = True
            else:
                output.append((key, old))
        if not replaced:
            output.append((param, str(value)))
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path,
                           parsed.params, urlencode(output), ""))

    def _from_template(self, page_index: int) -> str:
        """按规则模板生成下一页 URL（``{n}`` 为页码/偏移占位符）。

        ``{n}`` = ``rule.start + 当前页序号 × rule.step``：
        入口页序号为 1，因此 ``start=1, step=1`` 的页码模板给出 2，
        ``start=0, step=20`` 的偏移模板给出 20。
        """
        step = max(1, int(self.rule.step or 1))
        value = int(self.rule.start or 0) + max(1, int(page_index or 1)) * step
        try:
            return self.rule.template.replace("{n}", str(value))
        except (AttributeError, ValueError):
            return ""


# ---------------------------------------------------------------------------
# 路径末段数字处理
# ---------------------------------------------------------------------------
def _split_trailing_number(path: str) -> tuple:
    """把路径按"最后一个数字组"切成 (前缀, 数字, 后缀)。

    例如 ``/gallery/photo_2.html`` → ``("/gallery/photo_", 2, ".html")``；
    无数字时返回空 tuple。
    """
    match = _PATH_NUM_RE.match(path or "")
    if not match:
        return ()
    return match.group(1), int(match.group(2)), match.group(3)


def _replace_path(url: str, path: str) -> str:
    """替换 URL 的路径部分。"""
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, path, parsed.params,
                       parsed.query, ""))


def _current_query_page(base: str) -> int:
    """当前 URL 查询串中的页码值（无则返回 0）。"""
    try:
        pairs = parse_qsl(urlparse(base).query, keep_blank_values=True)
    except ValueError:
        return 0
    for key, value in pairs:
        if key.lower() in _PAGE_PARAMS and value.isdigit():
            return int(value)
    return 0


def _pick_next(numbers, current: int, step: int) -> int:
    """推断"下一页"页码。

    - 当前页号已知（URL 里带页码）→ 续接 ``当前 + step``
    - 否则取页面内出现的最小页码（通常是第 2 页），保证按顺序推进
    - 都没有时退回 ``最大值 + step``
    """
    step = max(1, int(step or 1))
    if current:
        return int(current) + step
    values = sorted(int(n) for n in numbers if int(n) > 1)
    if values:
        return values[0]
    return (max(int(n) for n in numbers) + step) if numbers else 1


__all__ = ["STRATEGY_AUTO", "STRATEGY_NEXT", "STRATEGY_PAGE_PARAM",
           "STRATEGY_API_OFFSET", "PageRule", "Paginator"]
