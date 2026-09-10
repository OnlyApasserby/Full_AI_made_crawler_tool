"""规则驱动的提取器：不改代码适配新站点。

规则文件放在 ``resources/rules/*.json``（热加载，按文件修改时间自动重载），
一条规则描述"什么 URL 用哪些选择器/正则取媒体、怎么翻页"。
以下划线开头的文件（如 ``_template.json``）视为模板/文档，不参与匹配。

规则字段
--------
``match``
    ``domain``（支持 ``*.example.com`` 通配）、``url_regex``、``exclude_url_regex``
``headers``
    下载媒体时携带的请求头（常用来带 Referer 过防盗链）
``pagination``
    交给 :class:`core.paginator.PageRule`：``strategy`` / ``param`` / ``template``
    / ``start`` / ``step``
``media``
    媒体取值规格列表，每项：``selector`` + ``attr``（``text`` 表示取元素文本）、
    ``kind``（image/video/audio，留空按扩展名推断）、``title_attr``、``require``
    （URL 必须匹配的正则）、``upgrade``（缩略图 → 原图的字符串替换对）
``regex``
    兜底正则列表：``pattern`` + ``group`` + ``kind``
``detail_links``
    列表页 → 详情页 的链接选择器（``selector`` + ``attr``），供编排线程跟进
``album``
    合集名来源：``{"selector": "...", "attr": "text"}``，缺省用页面标题

选择器引擎
----------
优先使用 ``beautifulsoup4``（+ ``lxml``）以获得完整 CSS 支持；
两者缺失时退回内置的**简化选择器**（``tag`` / ``.class`` / ``#id`` /
``tag.class`` / ``[attr]`` / ``[attr=value]``，后代用空格分隔，末级精确匹配、
祖先部分近似匹配）。降级只影响复杂选择器，不影响主流程。
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from config import BUNDLED_RULES_DIR as _BUNDLED_RULES_DIR, ensure_rules_dir
from core.extractor.base import BaseExtractor
from core.extractor.generic_html import (
    best_srcset, extract_page_album, parse_attrs,
)
from core.media.detector import MEDIA_TYPES, kind_of
from core.paginator import PageRule

try:  # 可选依赖：完整 CSS 选择器支持
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    BeautifulSoup = None
    HAS_BS4 = False

#: 无需闭合标签的空元素（取文本时跳过）
_VOID_TAGS = frozenset((
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr"))
_TAG_RE = re.compile(r"<([a-zA-Z][\w\-:]*)((?:\s+[^<>]*)?)>")
_SIMPLE_RE = re.compile(
    r"^(?P<tag>[a-zA-Z][\w\-]*)?"
    r"(?P<id>#[\w\-]+)?"
    r"(?P<classes>(?:\.[\w\-]+)*)"
    r"(?P<attrs>(?:\[[^\]]*\])*)$")
_ATTR_PART_RE = re.compile(r"\[([\w\-:]+)(?:\s*([~^$*|]?=)\s*\"?([^\]]*?)\"?)?\]")

_rules_lock = threading.Lock()
_rules_cache: list = []
_rules_mtime: float = -1.0


# ---------------------------------------------------------------------------
# 选择器引擎
# ---------------------------------------------------------------------------
def _split_simple(part: str) -> dict:
    """把单级选择器拆成 tag / id / classes / attrs。"""
    match = _SIMPLE_RE.match(part.strip())
    if not match:
        return {}
    data = match.groupdict()
    classes = [name for name in (data.get("classes") or "").split(".") if name]
    attr_parts = []
    for key, operator, value in _ATTR_PART_RE.findall(data.get("attrs") or ""):
        attr_parts.append((key.lower(), operator, value))
    return {
        "tag": (data.get("tag") or "").lower(),
        "id": (data.get("id") or "").lstrip("#"),
        "classes": classes,
        "attrs": attr_parts,
    }


def _match_simple(spec: dict, tag: str, attrs: dict) -> bool:
    """单级选择器是否命中某个标签。"""
    if not spec:
        return False
    if spec["tag"] and spec["tag"] != tag:
        return False
    if spec["id"] and attrs.get("id") != spec["id"]:
        return False
    if spec["classes"]:
        present = set((attrs.get("class") or "").split())
        if not set(spec["classes"]).issubset(present):
            return False
    for key, operator, value in spec["attrs"]:
        current = attrs.get(key, "")
        if operator is None:
            if key not in attrs:
                return False
        elif operator == "=":
            if current != value:
                return False
        elif operator == "*=":
            if value not in current:
                return False
        elif operator == "^=":
            if not current.startswith(value):
                return False
        elif operator == "$=":
            if not current.endswith(value):
                return False
        else:  # ~= / |= 等低频运算符统一按"包含"处理
            if value not in current:
                return False
    return True


def _iter_tags(html_text: str):
    """遍历 HTML 中的标签，产出 (标签名, 属性字典, 起始位置, 结束位置)。"""
    for match in _TAG_RE.finditer(html_text or ""):
        tag = match.group(1).lower()
        yield tag, parse_attrs(match.group(0)), match.start(), match.end()


def _inner_text(html_text: str, tag: str, end: int) -> str:
    """粗略取元素的内部文本（到最近一个同名闭合标签或下一个标签为止）。"""
    if tag in _VOID_TAGS:
        return ""
    close = html_text.lower().find(f"</{tag}", end)
    segment = html_text[end:close if close > 0 else end + 2000]
    return re.sub(r"<[^>]+>", " ", segment)


def _select_fallback(html_text: str, selector: str) -> list:
    """内置简化选择器：返回 [(标签名, 属性字典, 文本)]。

    仅最后一级做精确匹配；祖先部分用"出现在匹配点之前的最后一个同名模式"
    近似判断，覆盖 ``div.list a.item`` 这类常见写法。
    """
    parts = [part for part in (selector or "").split() if part]
    if not parts:
        return []
    specs = [_split_simple(part) for part in parts]
    if any(not spec for spec in specs):
        return []
    target, ancestors = specs[-1], specs[:-1]
    results: list = []
    for tag, attrs, start, end in _iter_tags(html_text):
        if not _match_simple(target, tag, attrs):
            continue
        if ancestors:
            prefix = html_text[:start]
            ok = True
            for spec in ancestors:
                found = False
                for atag, aattrs, _s, _e in _iter_tags(prefix):
                    if _match_simple(spec, atag, aattrs):
                        found = True
                        break
                if not found:
                    ok = False
                    break
            if not ok:
                continue
        results.append((tag, attrs, _inner_text(html_text, tag, end)))
    return results


def _select_elements(html_text: str, selector: str) -> list:
    """返回匹配元素的 (标签名, 属性字典) 列表。"""
    selector = (selector or "").strip()
    if not selector or not html_text:
        return []
    if HAS_BS4:
        try:
            soup = BeautifulSoup(html_text, "lxml")
        except Exception:
            try:
                soup = BeautifulSoup(html_text, "html.parser")
            except Exception:
                soup = None
        if soup is not None:
            found: list = []
            for element in soup.select(selector):
                attrs = {str(key).lower(): (" ".join(value) if isinstance(value, list)
                                            else str(value))
                         for key, value in (element.attrs or {}).items()}
                found.append((element.name or "", attrs))
            return found
    return [(tag, attrs) for tag, attrs, _text in _select_fallback(html_text, selector)]


def select_values(html_text: str, selector: str, attr: str = "src") -> list:
    """按选择器取出指定属性（``attr="text"`` 取元素文本），保持文档顺序。"""
    attr = (attr or "src").strip().lower()
    if attr == "text":
        if HAS_BS4 and (selector or "").strip():
            try:
                soup = BeautifulSoup(html_text, "lxml")
                return [element.get_text(" ", strip=True) for element in
                        soup.select(selector)]
            except Exception:
                pass
        return [text.strip() for _tag, _attrs, text in
                _select_fallback(html_text, selector)]
    values: list = []
    for _tag, attrs in _select_elements(html_text, selector):
        if attr == "srcset":
            value = best_srcset(attrs.get("srcset", "") or
                                attrs.get("data-srcset", ""))
        else:
            value = attrs.get(attr, "")
        if value:
            values.append(value)
    return values


# ---------------------------------------------------------------------------
# 规则模型
# ---------------------------------------------------------------------------
@dataclass
class SiteRule:
    """单条站点规则。"""

    id: str = ""
    name: str = ""
    enabled: bool = True
    priority: int = 50
    domains: list = field(default_factory=list)
    url_regex: str = ""
    exclude_url_regex: str = ""
    headers: dict = field(default_factory=dict)
    pagination: dict = field(default_factory=dict)
    media: list = field(default_factory=list)
    regex: list = field(default_factory=list)
    detail_links: dict = field(default_factory=dict)
    album: dict = field(default_factory=dict)
    note: str = ""

    # ---------- 匹配 ----------
    def score(self, url: str) -> float:
        """本规则对 URL 的匹配置信度（0 表示不匹配）。"""
        if not self.enabled or not url:
            return 0.0
        try:
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower()
        except ValueError:
            return 0.0
        if self.exclude_url_regex and _safe_search(self.exclude_url_regex, url):
            return 0.0
        domain_hit = False
        if self.domains:
            for pattern in self.domains:
                text = str(pattern or "").strip().lower().lstrip(".")
                if not text:
                    continue
                if text.startswith("*."):
                    if host.endswith("." + text[2:]):
                        domain_hit = True
                        break
                elif host == text or host.endswith("." + text):
                    domain_hit = True
                    break
            if not domain_hit:
                return 0.0
        regex_hit = bool(self.url_regex) and _safe_search(self.url_regex, url)
        if self.url_regex and not regex_hit:
            return 0.0
        if domain_hit and regex_hit:
            return 1.0
        if domain_hit:
            return 0.9
        if regex_hit:
            return 0.85
        return 0.0

    # ---------- 序列化 ----------
    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "enabled": self.enabled,
            "priority": self.priority, "match": {
                "domain": list(self.domains), "url_regex": self.url_regex,
                "exclude_url_regex": self.exclude_url_regex},
            "headers": dict(self.headers), "pagination": dict(self.pagination),
            "media": [dict(spec) for spec in self.media],
            "regex": [dict(spec) for spec in self.regex],
            "detail_links": dict(self.detail_links), "album": dict(self.album),
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SiteRule":
        data = data or {}
        match = data.get("match") or {}
        domains = match.get("domain") or data.get("domains") or []
        if isinstance(domains, str):
            domains = [domains]
        return cls(
            id=str(data.get("id") or ""),
            name=str(data.get("name") or data.get("id") or ""),
            enabled=bool(data.get("enabled", True)),
            priority=int(data.get("priority", 50) or 50),
            domains=[str(item) for item in domains],
            url_regex=str(match.get("url_regex") or data.get("url_regex") or ""),
            exclude_url_regex=str(match.get("exclude_url_regex") or ""),
            headers=dict(data.get("headers") or {}),
            pagination=dict(data.get("pagination") or {}),
            media=[dict(spec) for spec in (data.get("media") or [])
                   if isinstance(spec, dict)],
            regex=[dict(spec) for spec in (data.get("regex") or [])
                   if isinstance(spec, dict)],
            detail_links=dict(data.get("detail_links") or {}),
            album=dict(data.get("album") or {}),
            note=str(data.get("_note") or data.get("note") or ""),
        )

    def page_rule(self) -> PageRule | None:
        """把 ``pagination`` 配置转成 :class:`PageRule`（未配置返回 None）。"""
        if not self.pagination:
            return None
        data = self.pagination
        return PageRule(
            strategy=str(data.get("strategy", "auto") or "auto"),
            template=str(data.get("template", "") or ""),
            param=str(data.get("param", "") or ""),
            start=int(data.get("start", 1) or 1),
            step=int(data.get("step", 1) or 1),
            max_pages=int(data.get("max_pages", 0) or 0))


def _safe_search(pattern: str, text: str) -> bool:
    """正则安全匹配（非法正则返回 False，不影响其它规则）。"""
    try:
        return bool(re.search(pattern, text, re.IGNORECASE))
    except re.error:
        return False


# ---------------------------------------------------------------------------
# 规则加载（按目录修改时间热重载）
# ---------------------------------------------------------------------------
def rules_dir() -> str:
    """规则目录路径（不存在时自动创建）。"""
    return ensure_rules_dir()


def _rule_directories() -> list:
    """全部规则目录：可写目录优先，其次打包内置的只读目录。

    打包（PyInstaller）后随包发布的规则位于只读的 ``_MEIPASS/resources/rules``，
    用户自建/修改的规则位于 exe 同级的可写目录；后者的同名 id 优先生效。
    """
    directories: list = []
    for directory in (rules_dir(), _BUNDLED_RULES_DIR):
        if directory and os.path.isdir(directory) and directory not in directories:
            directories.append(directory)
    return directories


def load_rules(force: bool = False) -> list:
    """加载全部规则（按目录内容修改时间自动重载）。

    :return: 按 priority 升序的 :class:`SiteRule` 列表
    """
    global _rules_cache, _rules_mtime
    with _rules_lock:
        directories = _rule_directories()
        stamp = 0.0
        files: list = []
        for directory in directories:
            try:
                names = sorted(name for name in os.listdir(directory)
                               if name.endswith(".json") and not name.startswith("_"))
            except OSError:
                continue
            for name in names:
                path = os.path.join(directory, name)
                try:
                    stamp = max(stamp, os.path.getmtime(path))
                except OSError:
                    continue
                files.append((name, path))
        if not force and stamp == _rules_mtime and _rules_cache:
            return list(_rules_cache)
        rules: list = []
        seen_ids: set = set()
        for name, path in files:
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
            except (OSError, ValueError):
                continue
            entries = data if isinstance(data, list) else [data]
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                rule = SiteRule.from_dict(entry)
                if not rule.id:
                    rule.id = os.path.splitext(name)[0]
                # 至少要有"能起作用"的配置：取值规格 / 翻页 / 详情页链接
                if not (rule.media or rule.regex or rule.detail_links
                        or rule.pagination):
                    continue
                if rule.id in seen_ids:
                    continue      # 可写目录中的同名规则优先
                seen_ids.add(rule.id)
                rules.append(rule)
        rules.sort(key=lambda item: item.priority)
        _rules_cache = rules
        _rules_mtime = stamp
        return list(rules)


def reload_rules() -> list:
    """强制重新加载规则。"""
    return load_rules(force=True)


def get_rule(rule_id: str) -> SiteRule | None:
    """按 id 取规则。"""
    for rule in load_rules():
        if rule.id == rule_id:
            return rule
    return None


def save_rule(rule: SiteRule, filename: str = "") -> str:
    """保存规则到 JSON 文件并立即重载，返回文件路径。

    :raises ValueError: 规则未设置 id
    :raises OSError: 写入失败
    """
    if not rule.id:
        raise ValueError("规则缺少 id")
    name = filename or f"{rule.id}.json"
    if not name.endswith(".json"):
        name += ".json"
    path = os.path.join(rules_dir(), name)
    payload = rule.to_dict()
    if rule.note:
        payload["_note"] = rule.note
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    load_rules(force=True)
    return path


def delete_rule(rule_id: str) -> bool:
    """删除规则文件（按 id 匹配文件名或文件内容）。"""
    directory = rules_dir()
    removed = False
    try:
        names = [name for name in os.listdir(directory) if name.endswith(".json")]
    except OSError:
        return False
    for name in names:
        path = os.path.join(directory, name)
        stem = os.path.splitext(name)[0]
        if stem == rule_id:
            try:
                os.remove(path)
                removed = True
            except OSError:
                pass
            continue
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        entries = data if isinstance(data, list) else [data]
        if any(isinstance(entry, dict) and entry.get("id") == rule_id
               for entry in entries):
            try:
                os.remove(path)
                removed = True
            except OSError:
                pass
    if removed:
        load_rules(force=True)
    return removed


def validate_rule(data: dict) -> list:
    """校验规则字典，返回问题列表（空列表表示可用）。"""
    problems: list = []
    if not isinstance(data, dict):
        return ["规则必须是 JSON 对象"]
    if not str(data.get("id") or "").strip():
        problems.append("缺少 id")
    match = data.get("match") or {}
    if not match.get("domain") and not match.get("url_regex"):
        problems.append("match 至少需要 domain 或 url_regex 之一")
    for key, pattern in (("url_regex", match.get("url_regex")),
                         ("exclude_url_regex", match.get("exclude_url_regex"))):
        if pattern:
            try:
                re.compile(pattern)
            except re.error as exc:
                problems.append(f"{key} 正则非法：{exc}")
    media = data.get("media") or []
    regex = data.get("regex") or []
    pagination = data.get("pagination") or {}
    if not (media or regex or data.get("detail_links") or pagination):
        problems.append("至少需要 media / regex / detail_links / pagination 之一")
    for index, spec in enumerate(media):
        if not isinstance(spec, dict) or not spec.get("selector"):
            problems.append(f"media[{index}] 缺少 selector")
    for index, spec in enumerate(regex):
        if not isinstance(spec, dict) or not spec.get("pattern"):
            problems.append(f"regex[{index}] 缺少 pattern")
            continue
        try:
            re.compile(spec["pattern"])
        except re.error as exc:
            problems.append(f"regex[{index}] 正则非法：{exc}")
    strategy = str(pagination.get("strategy") or "auto")
    if strategy not in ("auto", "next_link", "page_param", "api_offset"):
        problems.append(f"pagination.strategy 非法：{strategy}")
    if strategy == "api_offset" and not pagination.get("template"):
        problems.append("pagination.strategy=api_offset 需要 template")
    return problems


# ---------------------------------------------------------------------------
# 供编排线程使用的查询
# ---------------------------------------------------------------------------
def matching_rules(url: str) -> list:
    """返回匹配该 URL 的规则（按置信度降序）。"""
    scored = [(rule.score(url), rule) for rule in load_rules()]
    return [rule for score, rule in
            sorted((item for item in scored if item[0] > 0),
                   key=lambda item: (-item[0], item[1].priority))]


def page_rule_for(url: str) -> PageRule | None:
    """取该 URL 命中的第一条规则的翻页配置（未命中返回 None）。"""
    for rule in matching_rules(url):
        rule_page = rule.page_rule()
        if rule_page is not None:
            return rule_page
    return None


def detail_links_for(url: str, html_text: str) -> list:
    """按规则从列表页提取详情页链接（未配置或未命中返回空列表）。"""
    for rule in matching_rules(url):
        spec = rule.detail_links or {}
        selector = str(spec.get("selector") or "")
        if not selector:
            continue
        attr = str(spec.get("attr") or "href")
        pattern = str(spec.get("pattern") or "")
        base = url
        links: list = []
        for value in select_values(html_text, selector, attr):
            text = (value or "").strip()
            if not text or text.lower().startswith(("javascript:", "#", "mailto:")):
                continue
            try:
                absolute = urljoin(base, text).split("#", 1)[0]
            except ValueError:
                continue
            if not absolute.startswith(("http://", "https://")):
                continue
            if pattern and pattern not in absolute:
                continue
            if absolute not in links:
                links.append(absolute)
        return links
    return []


# ---------------------------------------------------------------------------
# 提取器
# ---------------------------------------------------------------------------
class RuleExtractor(BaseExtractor):
    """按站点规则提取媒体（优先级高于通用提取器）。"""

    name = "rule"
    priority = 50

    def can_handle(self, url: str, ctx) -> float:
        if not getattr(ctx, "html", ""):
            return 0.0
        return 1.0 if matching_rules(ctx.base_url or url) else 0.0

    def extract(self, ctx):
        html_text = ctx.html or ""
        for rule in matching_rules(ctx.base_url or ctx.url):
            album = self._album_of(rule, html_text, ctx)
            seen: set = set()
            index = 0
            for url, kind, title, extra in self._candidates(rule, ctx):
                item = self.make_item(url, ctx, kind=kind, title=title, album=album,
                                      index=index + 1,
                                      headers=dict(rule.headers or {}),
                                      origin=f"rule:{rule.id}",
                                      extra=extra)
                if item is None or item.kind not in MEDIA_TYPES:
                    continue
                if item.url in seen:
                    continue
                seen.add(item.url)
                index += 1
                yield item

    # ---------- 取值 ----------
    def _candidates(self, rule: SiteRule, ctx):
        """产出 (url, kind, title, extra) 候选。"""
        html_text = ctx.html or ""
        for spec in rule.media:
            selector = str(spec.get("selector") or "")
            if not selector:
                continue
            attr = str(spec.get("attr") or "src")
            kind_hint = str(spec.get("kind") or "")
            title_attr = str(spec.get("title_attr") or "")
            require = str(spec.get("require") or "")
            upgrades = _normalize_upgrades(spec.get("upgrade"))
            for raw in select_values(html_text, selector, attr):
                url = self.absolute(raw, ctx.base_url)
                if not url:
                    continue
                if require and not _safe_search(require, url):
                    continue
                kind = kind_hint or kind_of(url, "")
                yield url, kind, "", {"rule_field": attr}
                for old, new in upgrades:
                    if old and old in url:
                        yield url.replace(old, new), kind, "", {"upgraded_from": url}
        for spec in rule.regex:
            pattern = str(spec.get("pattern") or "")
            if not pattern:
                continue
            try:
                group = int(spec.get("group", 1) or 1)
            except (TypeError, ValueError):
                group = 1
            kind_hint = str(spec.get("kind") or "")
            flags = re.IGNORECASE if spec.get("ignore_case", True) else 0
            try:
                matcher = re.compile(pattern, flags)
            except re.error:
                continue
            for match in matcher.finditer(html_text):
                try:
                    raw = match.group(group)
                except IndexError:
                    continue
                url = self.absolute(raw, ctx.base_url)
                if not url:
                    continue
                yield url, kind_hint or kind_of(url, ""), "", {"rule_regex": pattern[:40]}

    def _album_of(self, rule: SiteRule, html_text: str, ctx) -> str:
        """合集名：规则指定选择器优先，其次页面标题。"""
        selector = str((rule.album or {}).get("selector") or "")
        if selector:
            values = select_values(html_text, selector,
                                   str((rule.album or {}).get("attr") or "text"))
            if values:
                return values[0].strip()[:60]
        return extract_page_album(html_text)


def _normalize_upgrades(value) -> list:
    """规范化 upgrade 配置：``[["/thumb/","/original/"]]`` 或单个字符串对。"""
    if not value:
        return []
    pairs: list = []
    if isinstance(value, (list, tuple)) and len(value) == 2 and \
            all(isinstance(item, str) for item in value):
        return [(value[0], value[1])]
    for entry in value if isinstance(value, (list, tuple)) else []:
        if isinstance(entry, (list, tuple)) and len(entry) >= 2:
            pairs.append((str(entry[0]), str(entry[1])))
        elif isinstance(entry, dict) and entry.get("from"):
            pairs.append((str(entry["from"]), str(entry.get("to", ""))))
    return pairs


__all__ = [
    "SiteRule", "RuleExtractor", "HAS_BS4", "select_values", "rules_dir",
    "load_rules", "reload_rules", "get_rule", "save_rule", "delete_rule",
    "validate_rule", "matching_rules", "page_rule_for", "detail_links_for",
]
