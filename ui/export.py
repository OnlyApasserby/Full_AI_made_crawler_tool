"""导出功能：把爬取结果（链接 + 页面文字）或媒体清单写为 CSV / JSON / Markdown。

数据序列化与主窗口解耦；文件选择对话框、日志与消息框仍留在主窗口处理。
"""

from __future__ import annotations

import csv
import json
import time
from urllib.parse import urlparse

# 导出格式 -> (默认文件名模板, 文件对话框过滤器)
FORMAT_SPECS = {
    "csv": ("crawl_results_{ts}.csv", "CSV 文件 (*.csv);;所有文件 (*)"),
    "json": ("crawl_results_{ts}.json", "JSON 文件 (*.json);;所有文件 (*)"),
    "markdown": ("crawl_results_{ts}.md", "Markdown 文件 (*.md);;所有文件 (*)"),
}

#: 媒体清单导出的默认文件名（与爬取结果区分）
MEDIA_NAME_SPECS = {
    "csv": "media_list_{ts}.csv",
    "json": "media_list_{ts}.json",
    "markdown": "media_list_{ts}.md",
}

#: 媒体清单字段：(键, 表头)；键与 MediaItem.to_dict() 对齐，额外支持 referer
MEDIA_FIELDS = (
    ("url", "资源URL"), ("kind", "类型"), ("album", "合集"),
    ("index", "序号"), ("width", "宽"), ("height", "高"),
    ("duration", "时长(秒)"), ("size", "大小(字节)"), ("origin", "来源"),
    ("source_page", "来源页"), ("referer", "Referer"),
)
MEDIA_FIELD_KEYS = tuple(key for key, _label in MEDIA_FIELDS)
MEDIA_FIELD_LABELS = {key: label for key, label in MEDIA_FIELDS}


def export_default_name(fmt: str, prefix: str = "crawl") -> str:
    """按导出格式返回带时间戳的默认文件名。

    :param prefix: ``crawl`` 为爬取结果，``media`` 为媒体清单
    """
    ts = time.strftime("%Y%m%d_%H%M%S")
    specs = MEDIA_NAME_SPECS if prefix == "media" else {
        key: value[0] for key, value in FORMAT_SPECS.items()}
    template = specs.get(fmt, specs.get("csv", "crawl_results_{ts}.csv"))
    return template.format(ts=ts)


def export_file_filter(fmt: str) -> str:
    """返回导出格式对应的文件对话框过滤器字符串。"""
    return FORMAT_SPECS.get(fmt, FORMAT_SPECS["csv"])[1]


def _row_data(link: str, fields: list[str], page_texts: dict) -> list:
    """按字段列表生成一行数据。"""
    row = []
    for f in fields:
        if f == "url":
            row.append(link)
        elif f == "domain":
            row.append(urlparse(link).netloc)
        elif f == "text":
            row.append(page_texts.get(link, ""))
    return row


def write_export(filepath: str, fmt: str, fields: list[str],
                 links: list, page_texts: dict) -> int:
    """把链接列表按指定格式写入文件。

    :param filepath: 目标文件路径
    :param fmt: csv / json / markdown
    :param fields: 需要导出的字段 key（url/domain/text，顺序即列顺序）
    :param links: 全部链接（有序）
    :param page_texts: {url: 页面文字}
    :return: 导出的记录数
    :raises OSError: 写入失败时抛出（由调用方提示用户）
    """
    if fmt == "json":
        records = []
        for link in links:
            row = _row_data(link, fields, page_texts)
            records.append(dict(zip(fields, row)))
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
        return len(records)

    if fmt == "markdown":
        lines = ["| " + " | ".join(fields) + " |",
                 "|" + "|".join(["---"] * len(fields)) + "|"]
        for link in links:
            row = _row_data(link, fields, page_texts)
            # 表格转义：竖线转义、换行转空格、文字截断防止表格过长
            escaped = []
            for cell in row:
                cell = str(cell)
                if len(cell) > 200:
                    cell = cell[:200] + "…"
                escaped.append(cell.replace("|", "\\|").replace("\n", " "))
            lines.append("| " + " | ".join(escaped) + " |")
        with open(filepath, "w", encoding="utf-8-sig") as f:
            f.write("\n".join(lines))
        return len(links)

    # csv（默认）：使用UTF-8 with BOM，避免Excel打开中文乱码
    with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(fields)
        for link in links:
            writer.writerow(_row_data(link, fields, page_texts))
    return len(links)


def media_row_data(item, fields=None) -> list:
    """按字段列表生成一行媒体数据（支持 MediaItem 对象与字典）。"""
    if hasattr(item, "to_dict"):
        data = item.to_dict()
    else:
        data = dict(item or {})
    fields = fields or MEDIA_FIELD_KEYS
    row = []
    for key in fields:
        if key == "referer":
            headers = data.get("headers") or {}
            value = ""
            for header_key, header_value in headers.items():
                if str(header_key).lower() == "referer":
                    value = header_value
                    break
            row.append(value or data.get("source_page", ""))
        elif key == "index":
            row.append(data.get("index", data.get("seq", 0)) or 0)
        else:
            row.append(data.get(key, ""))
    return row


def write_media_export(filepath: str, fmt: str, items, fields=None) -> int:
    """把媒体清单写入文件（CSV / JSON / Markdown）。

    :param items: :class:`core.media.models.MediaItem` 列表（或等价的字典列表）
    :param fields: 字段键列表（见 :data:`MEDIA_FIELD_KEYS`）
    :return: 导出的记录数
    :raises OSError: 写入失败时抛出（由调用方提示用户）
    """
    fmt = fmt if fmt in ("csv", "json", "markdown") else "csv"
    fields = list(fields or MEDIA_FIELD_KEYS)
    records = [media_row_data(item, fields) for item in (items or [])]

    if fmt == "json":
        payload = [dict(zip(fields, row)) for row in records]
        with open(filepath, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        return len(payload)

    headers = [MEDIA_FIELD_LABELS.get(key, key) for key in fields]
    if fmt == "markdown":
        lines = ["| " + " | ".join(headers) + " |",
                 "|" + "|".join(["---"] * len(headers)) + "|"]
        for row in records:
            cells = []
            for cell in row:
                text = str(cell)
                if len(text) > 200:
                    text = text[:200] + "…"
                cells.append(text.replace("|", "\\|").replace("\n", " "))
            lines.append("| " + " | ".join(cells) + " |")
        with open(filepath, "w", encoding="utf-8-sig") as handle:
            handle.write("\n".join(lines))
        return len(records)

    # csv（默认）：UTF-8 with BOM，避免 Excel 打开中文乱码
    with open(filepath, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        writer.writerows(records)
    return len(records)


__all__ = [
    "export_default_name", "export_file_filter", "write_export",
    "MEDIA_FIELDS", "MEDIA_FIELD_KEYS", "MEDIA_FIELD_LABELS",
    "media_row_data", "write_media_export",
]
