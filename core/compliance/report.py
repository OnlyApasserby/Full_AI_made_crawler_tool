"""结构化报告的构建与渲染（JSON / Markdown / HTML）。

报告的写法本身就是红线的一部分：
- 标题固定为"合规辅助筛查报告（非法律意见）"
- 顶部固定免责声明，任何导出格式都不省略
- 每条命中的措辞统一为"需人工确认的线索"，**不出现"可以抓取/禁止抓取"的结论**
- 未命中的分类也会列出（0 条），避免读者把"没扫到"误读成"没问题"
- 附"已拦截结论式表述 N 处"，让红线机制的执行情况可见
"""

from __future__ import annotations

import html as html_lib
import os
import re
import time
from urllib.parse import urlparse

import config
from core.compliance import guard
from core.compliance.models import (
    CATEGORY_ORDER, ComplianceReport, GuardStats,
)

REPORT_TITLE = "合规辅助筛查报告（非法律意见）"

#: 报告结尾的人工后续动作（同样不含任何结论性判断）
NEXT_STEPS = (
    "请由法务或律师复核本报告列出的条款原文，确认其对本次访问方式的适用范围与前提条件。",
    "请同时核对目标站点的 robots.txt 约定，并确认是否存在官方 API、开放平台或书面授权渠道。",
    "如需继续采集，请按条款要求的数据授权范围、请求频率与接口方式调整实现，并保留沟通记录。",
    "本报告不构成法律意见：其中的分类与置信度只是检索排序结果，不能作为行动依据。",
)

_ILLEGAL_FILENAME_RE = re.compile(r"[^0-9A-Za-z._-]+")


# ---------------------------------------------------------------------------
# 构建
# ---------------------------------------------------------------------------
def site_name(start_url: str) -> str:
    """从 URL 取出站点名（host），用于报告标题与文件名。"""
    return (urlparse(start_url or "").netloc or start_url or "").strip()


def report_filename(site: str, when: str = "") -> str:
    """报告文件名：``<host>_<时间戳>.json``（非法字符替换为下划线）。"""
    host = _ILLEGAL_FILENAME_RE.sub("_", site_name(site) or "site").strip("_")
    stamp = when or time.strftime("%Y%m%d_%H%M%S")
    return f"{host or 'site'}_{stamp}.json"


def default_report_path(site: str) -> str:
    """报告默认落盘路径：``resources/compliance/<host>_<时间戳>.json``。"""
    return os.path.join(config.ensure_compliance_dir(), report_filename(site))


def build_report(*, start_url: str = "", pages=None, skipped=None, findings=None,
                 robots_summary: dict | None = None,
                 engine: str = "", llm_status: str = "",
                 guard_stats=None, scan_time: str = "") -> ComplianceReport:
    """组装报告对象（并强制补齐免责声明与人工复核标记）。"""
    from core.compliance.models import ENGINE_LOCAL

    report = ComplianceReport(
        start_url=start_url,
        site=site_name(start_url),
        scan_time=scan_time or time.strftime("%Y-%m-%d %H:%M:%S"),
        engine=engine or ENGINE_LOCAL,
        llm_status=llm_status or "",
        pages=list(pages or []),
        skipped=list(skipped or []),
        findings=list(findings or []),
        robots_summary=dict(robots_summary or {}),
        next_steps=list(NEXT_STEPS),
        guard_stats=guard_stats if isinstance(guard_stats, GuardStats) else GuardStats(),
    )
    report.stats = report.stat_rows()
    guard.ensure_disclaimer(report)
    return report


# ---------------------------------------------------------------------------
# 渲染：Markdown
# ---------------------------------------------------------------------------
def _md_quote(text: str) -> str:
    """把原文转成 Markdown 引用块（逐行加 > ）。"""
    lines = (text or "").splitlines() or [""]
    return "\n".join(f"> {line}" for line in lines)


def render_markdown(report: ComplianceReport) -> str:
    """渲染 Markdown 报告（适合贴进工单/文档留档）。"""
    disclaimer = report.disclaimer or guard.disclaimer_text()
    out: list = [f"# {REPORT_TITLE}", "", "## 免责声明", "",
                 _md_quote(disclaimer), "",
                 "## 一、站点与扫描概况", "",
                 "| 项目 | 内容 |", "| --- | --- |",
                 f"| 起始网址 | {report.start_url} |",
                 f"| 站点 | {report.site} |",
                 f"| 扫描时间 | {report.scan_time} |",
                 f"| 检测引擎 | {_engine_text(report)} |",
                 f"| 成功扫描页面 | {report.pages_scanned} 个 |",
                 f"| 命中线索 | {report.total_hits} 条 |",
                 f"| 跳过页面 | {len(report.skipped or [])} 项 |", ""]

    if report.llm_status:
        out += ["## 二、检测引擎说明", "", f"- {report.llm_status}", ""]
        section = "三"
    else:
        section = "二"

    out += [f"## {section}、分类命中统计", "",
            "| 关注类别 | 命中线索数 |", "| --- | --- |"]
    for stat in (report.stats or report.stat_rows()):
        out.append(f"| {stat.label} | {stat.count} |")
    out += ["", "> 未命中的类别也一并列出：0 表示本次未在已扫描页面中发现相关表述，"
                "并不代表该站点没有此类约束。", ""]

    body_section = _next_section(section)
    out += [f"## {body_section}、命中明细", ""]
    if not report.findings:
        out += ["本次扫描未发现与关注类别相关的条款线索。",
                "这**不代表**可以抓取：条款可能位于未扫描到的页面（需登录、PDF、其他域名等），"
                "也可能以图片或动态脚本呈现而未被文本提取覆盖。", ""]
    else:
        grouped = {category: [] for category in CATEGORY_ORDER}
        for hit in report.findings:
            grouped.setdefault(hit.category, []).append(hit)
        for category in CATEGORY_ORDER:
            items = grouped.get(category) or []
            if not items:
                continue
            out += [f"### {items[0].category_label}（{len(items)} 条）", ""]
            for order, hit in enumerate(items, 1):
                out += [f"**{order}. 置信度：{hit.confidence_text}（{hit.confidence}）**", "",
                        f"- 来源页面：{hit.source_url}",
                        f"- 小节：{hit.heading or '（未识别）'}",
                        f"- 命中词条：{'、'.join(hit.matched_terms) or '（无）'}",
                        f"- 原文位置：第 {hit.clause_index + 1} 条（字符偏移 {hit.char_offset}）"]
                if hit.context_note:
                    out.append(f"- 语境提示：{hit.context_note}")
                if hit.llm_applied:
                    out.append(f"- LLM 辅助标注：{hit.llm_label or '（无）'}"
                               + (f"；{hit.llm_note}" if hit.llm_note else ""))
                out += ["", "条款原文：", "", _md_quote(hit.quote), "",
                        f"- **{guard.REVIEW_NEEDED}**：{hit.review_hint}", ""]

    tail_section = _next_section(body_section)
    out += [f"## {tail_section}、建议的人工后续动作", ""]
    for step in (report.next_steps or NEXT_STEPS):
        out.append(f"1. {step}")
    out += ["", "## 附录", "",
            f"- 检测引擎：{_engine_text(report)}",
            f"- 红线机制：已拦截结论式表述 {report.guard_stats.intercepted} 处；"
            f"丢弃无法回溯的引文 {report.guard_stats.dropped_quotes} 条",
            f"- 免责声明版本：{report.disclaimer_version}", ""]

    if report.pages:
        out += ["### 已扫描页面", "", "| 页面 | 状态 | 正文字符数 | 发现来源 |", "| --- | --- | --- | --- |"]
        for page in report.pages:
            state = page.error or (f"HTTP {page.status}" if page.status else "成功")
            out.append(f"| {page.url} | {state} | {page.chars} | {page.matched_by or '-'} |")
        out.append("")
        failed = [page for page in report.pages if page.error]
        if failed:
            out += [f"> 上表中有 {len(failed)} 个页面未能读取（403 / 404 / 超时等）："
                    "这些页面**未参与条款筛查**，本次线索均来自成功读取的页面，"
                    "该情况**不影响已获得的结果**。如需覆盖它们，请改用可访问的条款入口，"
                    "或适当增大抓取间隔后重试。", ""]
    if report.skipped:
        out += ["### 未扫描 / 被跳过的页面", "", "| 页面 | 原因 |", "| --- | --- |"]
        for item in report.skipped:
            out.append(f"| {item.get('url', '')} | {item.get('reason', '')} |")
        out.append("")
    return "\n".join(out)


def _next_section(current: str) -> str:
    """返回下一个中文小节序号（一 → 二 → 三 …）。"""
    order = ("一", "二", "三", "四", "五", "六", "七", "八", "九", "十")
    try:
        return order[order.index(current) + 1]
    except (ValueError, IndexError):
        return "附"


def _engine_text(report: ComplianceReport) -> str:
    """引擎的中文说明（让读者知道哪些内容是模型标注的）。"""
    if report.engine == "local+llm":
        return "本地关键词匹配 + LLM 辅助标注（仅发送候选条款片段）"
    return "本地关键词匹配（未使用 LLM）"


# ---------------------------------------------------------------------------
# 渲染：HTML
# ---------------------------------------------------------------------------
_HTML_STYLE = """
body{font-family:"Microsoft YaHei",-apple-system,Segoe UI,sans-serif;margin:0;
padding:24px 32px;color:#24292f;line-height:1.7;background:#fff}
h1{font-size:22px;margin:0 0 4px}
h2{font-size:17px;margin:28px 0 10px;border-bottom:1px solid #e5e7eb;padding-bottom:6px}
h3{font-size:15px;margin:20px 0 8px;color:#1f2937}
.disclaimer{background:#fff8e1;border:1px solid #f0c36d;border-left:5px solid #e6a23c;
padding:12px 16px;border-radius:6px;margin:14px 0 20px;white-space:pre-wrap;font-size:13px}
.meta{font-size:13px;color:#57606a}
table{border-collapse:collapse;width:100%;margin:8px 0 16px;font-size:13px}
th,td{border:1px solid #e5e7eb;padding:6px 8px;text-align:left;vertical-align:top}
th{background:#f6f8fa;font-weight:600}
.hit{border:1px solid #e5e7eb;border-radius:6px;padding:12px 14px;margin:10px 0}
.hit .head{font-weight:600;margin-bottom:6px}
.quote{background:#f6f8fa;border-left:4px solid #d0d7de;padding:8px 12px;margin:8px 0;
white-space:pre-wrap;word-break:break-word;font-size:13px}
.review{color:#b45309;font-weight:600}
.tag{display:inline-block;background:#eef2f7;border-radius:10px;padding:1px 8px;
font-size:12px;margin-right:6px;color:#374151}
.note{font-size:13px;color:#4b5563}
"""


def _esc(text) -> str:
    return html_lib.escape(str(text or ""), quote=True)


def render_html(report: ComplianceReport) -> str:
    """渲染自包含的 HTML 报告（可直接在浏览器打开或另存归档）。"""
    disclaimer = report.disclaimer or guard.disclaimer_text()
    parts: list = [
        "<!DOCTYPE html>", '<html lang="zh-CN"><head><meta charset="utf-8">',
        f"<title>{_esc(REPORT_TITLE)} - {_esc(report.site)}</title>",
        f"<style>{_HTML_STYLE}</style></head><body>",
        f"<h1>{_esc(REPORT_TITLE)}</h1>",
        f'<p class="meta">站点：{_esc(report.site)} ｜ 起始网址：{_esc(report.start_url)}'
        f" ｜ 扫描时间：{_esc(report.scan_time)}</p>",
        f'<div class="disclaimer">{_esc(disclaimer)}</div>',
        "<h2>一、扫描概况</h2>", "<table><tr><th>项目</th><th>内容</th></tr>",
        f"<tr><td>检测引擎</td><td>{_esc(_engine_text(report))}</td></tr>",
        f"<tr><td>成功扫描页面</td><td>{report.pages_scanned} 个</td></tr>",
        f"<tr><td>命中线索</td><td>{report.total_hits} 条</td></tr>",
        f"<tr><td>跳过页面</td><td>{len(report.skipped or [])} 项</td></tr>",
        f"<tr><td>已拦截结论式表述</td><td>{report.guard_stats.intercepted} 处</td></tr>",
        f"<tr><td>丢弃无法回溯的引文</td><td>{report.guard_stats.dropped_quotes} 条</td></tr>",
        "</table>",
    ]

    parts += ["<h2>二、分类命中统计</h2>",
              "<table><tr><th>关注类别</th><th>命中线索数</th></tr>"]
    for stat in (report.stats or report.stat_rows()):
        parts.append(f"<tr><td>{_esc(stat.label)}</td><td>{stat.count}</td></tr>")
    parts.append("</table>")
    parts.append('<p class="note">未命中的类别也一并列出：0 表示本次未在已扫描页面中发现'
                 "相关表述，并不代表该站点没有此类约束。</p>")

    parts.append("<h2>三、命中明细</h2>")
    if not report.findings:
        parts.append("<p>本次扫描未发现与关注类别相关的条款线索。</p>"
                     '<p class="note">这<strong>不代表</strong>可以抓取：条款可能位于'
                     "未扫描到的页面（需登录、PDF、其他域名等），也可能以图片或动态脚本"
                     "呈现而未被文本提取覆盖。</p>")
    else:
        grouped = {category: [] for category in CATEGORY_ORDER}
        for hit in report.findings:
            grouped.setdefault(hit.category, []).append(hit)
        for category in CATEGORY_ORDER:
            items = grouped.get(category) or []
            if not items:
                continue
            parts.append(f"<h3>{_esc(items[0].category_label)}（{len(items)} 条）</h3>")
            for order, hit in enumerate(items, 1):
                parts.append('<div class="hit">')
                parts.append(f'<div class="head">{order}. 置信度：'
                             f"{_esc(hit.confidence_text)}（{hit.confidence}）</div>")
                parts.append(f'<div class="meta">来源页面：{_esc(hit.source_url)}</div>')
                parts.append(f'<div class="meta">小节：{_esc(hit.heading or "（未识别）")}'
                             f" ｜ 第 {hit.clause_index + 1} 条 ｜ 字符偏移 {hit.char_offset}</div>")
                for term in hit.matched_terms or []:
                    parts.append(f'<span class="tag">{_esc(term)}</span>')
                if hit.llm_applied:
                    parts.append(f'<div class="note">LLM 辅助标注：{_esc(hit.llm_label)}'
                                 f'{"；" + _esc(hit.llm_note) if hit.llm_note else ""}</div>')
                if hit.context_note:
                    parts.append(f'<div class="note">语境提示：{_esc(hit.context_note)}</div>')
                parts.append(f'<div class="quote">{_esc(hit.quote)}</div>')
                parts.append(f'<div class="note"><span class="review">'
                             f"{_esc(guard.REVIEW_NEEDED)}</span>：{_esc(hit.review_hint)}</div>")
                parts.append("</div>")

    parts.append("<h2>四、建议的人工后续动作</h2><ol>")
    for step in (report.next_steps or NEXT_STEPS):
        parts.append(f"<li>{_esc(step)}</li>")
    parts.append("</ol>")

    if report.pages:
        parts.append("<h2>附录 A：已扫描页面</h2>"
                     "<table><tr><th>页面</th><th>状态</th><th>正文字符数</th>"
                     "<th>发现来源</th></tr>")
        for page in report.pages:
            state = page.error or (f"HTTP {page.status}" if page.status else "成功")
            parts.append(f"<tr><td>{_esc(page.url)}</td><td>{_esc(state)}</td>"
                         f"<td>{page.chars}</td><td>{_esc(page.matched_by or '-')}</td></tr>")
        parts.append("</table>")
        failed = [page for page in report.pages if page.error]
        if failed:
            parts.append(f'<p class="note">上表中有 {len(failed)} 个页面未能读取'
                         "（403 / 404 / 超时等）：这些页面<strong>未参与条款筛查</strong>，"
                         "本次线索均来自成功读取的页面，<strong>不影响已获得的结果</strong>。"
                         "如需覆盖它们，请改用可访问的条款入口，或适当增大抓取间隔后重试。</p>")
    if report.skipped:
        parts.append("<h2>附录 B：未扫描 / 被跳过的页面</h2>"
                     "<table><tr><th>页面</th><th>原因</th></tr>")
        for item in report.skipped:
            parts.append(f"<tr><td>{_esc(item.get('url', ''))}</td>"
                         f"<td>{_esc(item.get('reason', ''))}</td></tr>")
        parts.append("</table>")

    parts.append(f'<p class="meta">免责声明版本：{_esc(report.disclaimer_version)}'
                 f" ｜ 报告生成时间：{_esc(report.scan_time)}</p>")
    parts.append("</body></html>")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------
def write_report(filepath: str, fmt: str, report: ComplianceReport) -> int:
    """把报告写入文件。

    :param fmt: ``json`` / ``markdown`` / ``html``
    :return: 写入的命中条数
    :raises OSError: 写入失败（由调用方提示用户）
    """
    fmt = (fmt or "json").lower()
    if fmt in ("md", "markdown"):
        content = render_markdown(report)
        encoding = "utf-8-sig"
    elif fmt == "html":
        content = render_html(report)
        encoding = "utf-8"
    else:
        content = report.to_json()
        encoding = "utf-8"
    directory = os.path.dirname(os.path.abspath(filepath))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(filepath, "w", encoding=encoding, newline="") as handle:
        handle.write(content)
    return report.total_hits


__all__ = [
    "REPORT_TITLE", "NEXT_STEPS", "site_name", "report_filename",
    "default_report_path", "build_report", "render_markdown", "render_html",
    "write_report",
]
