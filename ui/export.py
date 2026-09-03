"""导出功能：把爬取结果（链接 + 页面文字）写为 CSV / JSON / Markdown 文件。

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


def export_default_name(fmt: str) -> str:
    """按导出格式返回带时间戳的默认文件名。"""
    ts = time.strftime("%Y%m%d_%H%M%S")
    template = FORMAT_SPECS.get(fmt, FORMAT_SPECS["csv"])[0]
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


__all__ = ["export_default_name", "export_file_filter", "write_export"]
