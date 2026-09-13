"""合规辅助筛查对话框（robots.txt 校验的扩展界面）。

界面上的两条硬约束（与后端 ``core.compliance`` 保持一致）：

1. **常驻免责声明横幅**——不提供隐藏或关闭开关，导出文件里同样保留；
2. **不出现"可以/不可以抓取"的操作按钮**——本对话框只负责"把可疑条款连同
   原文摆出来供人工复核"，既不改变爬取流程，也不回写任何许可判断。

界面结构：目标网址 + 扫描控制 → LLM 辅助设置 → 命中列表（可按分类/关键词过滤）
→ 原文详情 → 页面清单 → 日志 → 多格式导出。
"""

from __future__ import annotations

import os

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QFileDialog, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

import config
from core.compliance import guard
from core.compliance import report as report_mod
from core.compliance.models import CATEGORY_LABELS, CATEGORY_ORDER
from core.compliance.scanner import ComplianceScanThread, report_base_name

#: 置信度档位配色
CONFIDENCE_COLOR = {"高": "#c0392b", "中": "#e67e22", "低": "#7f8c8d"}

#: 导出格式 -> (对话框过滤器, 扩展名)
EXPORT_FORMATS = {
    "json": ("JSON 文件 (*.json);;所有文件 (*)", "json"),
    "markdown": ("Markdown 文件 (*.md);;所有文件 (*)", "md"),
    "html": ("HTML 文件 (*.html);;所有文件 (*)", "html"),
}

_DISCLAIMER_BANNER = (
    "⚠ 合规辅助筛查（非法律意见）：本功能只把目标站点公开页面中与爬虫、自动化访问、"
    "数据采集、请求频率、API 使用、绕过限制相关的条款挑出来供人工复核。"
    "它不构成法律意见、不代替律师，也不会判断「可以抓取」或「不可以抓取」——"
    "是否继续采集，请依据 robots.txt 约定与专业法律意见自行决定。"
)


class ComplianceDialog(QDialog):
    """合规辅助筛查对话框。

    :param start_url: 初始目标网址（通常为「爬取配置」页填写的起始网址）
    :param ua_pool: 共享 UA 池（与爬取流程共用，保证 UA 一致）
    :param proxy: 共享代理配置
    :param request_delay: 页面抓取间隔（秒），默认取 ``COMPLIANCE_PAGE_DELAY``
    """

    def __init__(self, parent: QWidget | None = None, *, start_url: str = "",
                 ua_pool=None, proxy: dict | None = None, verify_ssl: bool = True,
                 request_delay: float | None = None, jitter: float = 0.3,
                 page_timeout: float | None = None, executable_path: str = "",
                 db_path: str = "", task_id: int = 0) -> None:
        super().__init__(parent)
        self.start_url = (start_url or "").strip()
        self.ua_pool = ua_pool
        self.proxy = proxy or {}
        self.verify_ssl = bool(verify_ssl)
        self.request_delay = (config.COMPLIANCE_PAGE_DELAY if request_delay is None
                              else float(request_delay))
        self.jitter = float(jitter or 0.0)
        self.page_timeout = page_timeout
        self.executable_path = executable_path or ""
        self.db_path = db_path or config.default_db_path()
        self.task_id = int(task_id or 0)

        self.thread: ComplianceScanThread | None = None
        self.report = None
        self._filtered: list = []

        self.setWindowTitle("合规辅助筛查（非法律意见）")
        self.setMinimumSize(1080, 760)
        self._build_ui()
        self.url_input.setText(self.start_url)

    # ------------------------------------------------------------------
    # 界面
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        # 1. 常驻免责声明横幅
        banner = QLabel(_DISCLAIMER_BANNER)
        banner.setWordWrap(True)
        banner.setStyleSheet(
            "QLabel{background:#fff8e1;border:1px solid #f0c36d;"
            "border-left:5px solid #e6a23c;border-radius:6px;padding:10px 12px;"
            "color:#7a4f01;}")
        layout.addWidget(banner)

        # 2. 目标与扫描控制
        control_group = QGroupBox("扫描目标与进度")
        control_layout = QVBoxLayout(control_group)
        url_row = QHBoxLayout()
        url_row.addWidget(QLabel("目标网址："))
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("https://example.com/")
        url_row.addWidget(self.url_input, 1)
        self.start_btn = QPushButton("开始筛查")
        self.start_btn.setDefault(True)
        self.start_btn.clicked.connect(self._on_start)
        self.pause_btn = QPushButton("暂停")
        self.pause_btn.setEnabled(False)
        self.pause_btn.clicked.connect(self._on_pause_resume)
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._on_stop)
        url_row.addWidget(self.start_btn)
        url_row.addWidget(self.pause_btn)
        url_row.addWidget(self.stop_btn)
        control_layout.addLayout(url_row)

        info_row = QHBoxLayout()
        self.progress_label = QLabel("尚未开始扫描")
        info_row.addWidget(self.progress_label)
        info_row.addStretch(1)
        self.pages_btn = QPushButton("查看已扫描 / 跳过页面")
        self.pages_btn.setEnabled(False)
        self.pages_btn.clicked.connect(self._on_show_pages)
        info_row.addWidget(self.pages_btn)
        control_layout.addLayout(info_row)
        layout.addWidget(control_group)

        # 3. LLM 辅助设置
        llm_group = QGroupBox("LLM 辅助标注（可选，失败自动降级为纯本地检测）")
        llm_layout = QVBoxLayout(llm_group)
        llm_row = QHBoxLayout()
        self.llm_check = QCheckBox("启用 LLM 辅助标注")
        self.llm_check.setChecked(bool(config.LLM_ENABLED))
        self.llm_check.setToolTip(
            "只把候选条款文本片段（默认置信度最高的 30 条）发送到接口，"
            "不发送整页 HTML；模型仅做分类标签与说明，不参与引文与结论。")
        llm_row.addWidget(self.llm_check)
        llm_row.addWidget(QLabel("API Key："))
        self.key_input = QLineEdit()
        self.key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_input.setPlaceholderText(
            f"留空则读取环境变量 {config.LLM_API_KEY_ENV}")
        llm_row.addWidget(self.key_input, 1)
        self.llm_hint = QLabel()
        llm_row.addWidget(self.llm_hint)
        llm_layout.addLayout(llm_row)
        hint = QLabel(
            f"接口：{config.LLM_BASE_URL} ｜ 模型：{config.LLM_MODEL} ｜ "
            f"环境变量 {config.LLM_API_KEY_ENV} 优先；界面输入仅存于内存，"
            "不落盘、不入库、不写入报告。")
        hint.setStyleSheet("color:#57606a;")
        hint.setWordWrap(True)
        llm_layout.addWidget(hint)
        layout.addWidget(llm_group)
        self._refresh_key_hint()

        # 4. 结果过滤与列表
        result_group = QGroupBox("条款线索（点击行查看原文；全部标注为需人工确认）")
        result_layout = QVBoxLayout(result_group)
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("分类："))
        self.category_combo = QComboBox()
        self.category_combo.addItem("全部", "")
        for code in CATEGORY_ORDER:
            self.category_combo.addItem(CATEGORY_LABELS[code], code)
        self.category_combo.currentIndexChanged.connect(lambda _i: self._apply_filter())
        filter_row.addWidget(self.category_combo)
        filter_row.addWidget(QLabel("关键词："))
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("在原文与命中词条中搜索")
        self.search_input.textChanged.connect(lambda _t: self._apply_filter())
        filter_row.addWidget(self.search_input, 1)
        self.result_label = QLabel("尚未扫描")
        filter_row.addWidget(self.result_label)
        result_layout.addLayout(filter_row)

        self.result_table = QTableWidget(0, 6)
        self.result_table.setHorizontalHeaderLabels(
            ["分类", "置信度", "命中词条", "原文摘要", "小节 / 来源页", "人工复核"])
        self.result_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self.result_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.result_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.result_table.verticalHeader().setVisible(False)
        header = self.result_table.horizontalHeader()
        for col in (0, 1, 5):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.result_table.itemSelectionChanged.connect(self._on_row_selected)
        result_layout.addWidget(self.result_table, 1)

        detail_row = QHBoxLayout()
        detail_row.addWidget(QLabel("条款原文与核对提示："))
        detail_row.addStretch(1)
        self.export_btns: list = []
        for fmt, label in (("json", "导出 JSON"), ("markdown", "导出 Markdown"),
                           ("html", "导出 HTML")):
            export_btn = QPushButton(label)
            export_btn.setEnabled(False)
            export_btn.setToolTip("导出文件内同样包含固定免责声明")
            export_btn.clicked.connect(lambda _checked, f=fmt: self._on_export(f))
            detail_row.addWidget(export_btn)
            self.export_btns.append(export_btn)
        result_layout.addLayout(detail_row)

        self.detail_view = QPlainTextEdit()
        self.detail_view.setReadOnly(True)
        self.detail_view.setPlaceholderText("选中上方条目可查看完整原文、来源位置与核对提示")
        self.detail_view.setMaximumHeight(190)
        result_layout.addWidget(self.detail_view)
        layout.addWidget(result_group, 1)

        # 5. 日志与底部
        log_group = QGroupBox("扫描日志")
        log_layout = QVBoxLayout(log_group)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumHeight(120)
        log_layout.addWidget(self.log_view)
        layout.addWidget(log_group)

        bottom_row = QHBoxLayout()
        boundary = QLabel("本报告不改变爬取流程：是否继续采集，请依据 robots.txt 与人工复核结果"
                          "（必要时咨询律师）自行决定。")
        boundary.setStyleSheet("color:#57606a;")
        boundary.setWordWrap(True)
        bottom_row.addWidget(boundary, 1)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        bottom_row.addWidget(close_btn)
        layout.addLayout(bottom_row)

    def _refresh_key_hint(self) -> None:
        """提示 Key 的来源（环境变量优先），不展示 Key 本身。"""
        from core.compliance.llm import LLMConfig

        source = LLMConfig.resolve().key_source
        self.llm_hint.setText(f"当前 Key 来源：{source}")
        self.llm_hint.setStyleSheet("color:#57606a;")

    # ------------------------------------------------------------------
    # 扫描控制
    # ------------------------------------------------------------------
    def _on_start(self) -> None:
        url = self.url_input.text().strip()
        if not url.startswith(("http://", "https://")):
            QMessageBox.warning(self, "提示", "请输入以 http:// 或 https:// 开头的网址")
            return
        self.start_url = url
        self.thread = ComplianceScanThread(
            url, ua_pool=self.ua_pool, proxy=self.proxy,
            verify_ssl=self.verify_ssl, request_delay=self.request_delay,
            jitter=self.jitter, page_timeout=self.page_timeout,
            executable_path=self.executable_path,
            llm_enabled=self.llm_check.isChecked(),
            llm_api_key=self.key_input.text().strip(),
            db_path=self.db_path, task_id=self.task_id)
        self.thread.signal_log.connect(self._on_log)
        self.thread.signal_page.connect(self._on_page)
        self.thread.signal_progress.connect(self._on_progress)
        self.thread.signal_finish.connect(self._on_finish)
        self.thread.signal_stopped.connect(self._on_stopped)
        self.thread.signal_error.connect(self._on_error)
        self.thread.signal_paused.connect(lambda: self._on_state("paused"))
        self.thread.signal_resumed.connect(lambda: self._on_state("running"))
        self.thread.start()
        self._on_state("running")
        self.log_view.clear()
        self.result_table.setRowCount(0)
        self.detail_view.clear()
        self.progress_label.setText("正在扫描…")

    def _on_pause_resume(self) -> None:
        if self.thread is None:
            return
        if self.thread.is_paused():
            self.thread.resume()
        else:
            self.thread.pause()

    def _on_stop(self) -> None:
        if self.thread is not None:
            self.thread.stop()
            self.stop_btn.setEnabled(False)
            self.progress_label.setText("正在停止…")

    def _on_state(self, state: str) -> None:
        running = state == "running"
        self.start_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.stop_btn.setEnabled(True)
        self.pause_btn.setText("暂停" if running else "继续")
        self.url_input.setEnabled(False)

    def _reset_buttons(self) -> None:
        self.start_btn.setEnabled(True)
        self.pause_btn.setEnabled(False)
        self.pause_btn.setText("暂停")
        self.stop_btn.setEnabled(False)
        self.url_input.setEnabled(True)
        for btn in self.export_btns:
            btn.setEnabled(self.report is not None)
        self.pages_btn.setEnabled(self.report is not None)

    # ------------------------------------------------------------------
    # 线程信号
    # ------------------------------------------------------------------
    def _on_log(self, text: str) -> None:
        self.log_view.appendPlainText(text)

    def _on_page(self, url: str, note: str) -> None:
        self.progress_label.setText(f"{note} ｜ {url}")

    def _on_progress(self, done: int, total: int, clauses: int) -> None:
        self.progress_label.setText(
            f"已扫描 {done}/{total} 个候选页面 ｜ 已切分条款 {clauses} 条")

    def _on_finish(self, report) -> None:
        self._on_report_ready(report, "✅ 扫描完成")

    def _on_stopped(self, report) -> None:
        self._on_report_ready(report, "⏹ 已停止（结果为已扫描页面的局部报告）")

    def _on_report_ready(self, report, prefix: str) -> None:
        self.report = report
        self._reset_buttons()
        self._fill_table()
        self.pages_btn.setEnabled(True)
        summary = (f"{prefix}：命中 {report.total_hits} 条线索，"
                   f"成功扫描 {report.pages_scanned} 页，"
                   f"引擎 {'本地+LLM' if report.engine == 'local+llm' else '纯本地'}")
        self.progress_label.setText(summary)
        self.log_view.appendPlainText(summary)

    def _on_error(self, message: str) -> None:
        self._reset_buttons()
        self.progress_label.setText("扫描失败")
        self.log_view.appendPlainText(f"❌ {message}")
        QMessageBox.critical(self, "错误", message)

    # ------------------------------------------------------------------
    # 结果展示
    # ------------------------------------------------------------------
    def _fill_table(self) -> None:
        self._apply_filter()

    def _apply_filter(self) -> None:
        """按分类 + 关键词过滤命中列表。"""
        if self.report is None:
            return
        category = self.category_combo.currentData() or ""
        keyword = self.search_input.text().strip().lower()
        rows = []
        for hit in self.report.findings:
            if category and hit.category != category:
                continue
            if keyword:
                haystack = " ".join([
                    hit.quote or "", " ".join(hit.matched_terms or []),
                    hit.llm_label or "", hit.llm_note or "",
                ]).lower()
                if keyword not in haystack:
                    continue
            rows.append(hit)
        self._filtered = rows
        self._render_rows(rows)

    def _render_rows(self, rows: list) -> None:
        self.result_table.setRowCount(len(rows))
        for index, hit in enumerate(rows):
            cells = [
                hit.category_label,
                f"{hit.confidence_text} ({hit.confidence})",
                "、".join(hit.matched_terms or [])[:40],
                (hit.quote or "")[:90].replace("\n", " "),
                f"{hit.heading or '—'} ｜ {hit.source_url}",
                guard.REVIEW_NEEDED if hit.needs_human_review else "",
            ]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(str(text))
                if col == 1:
                    item.setForeground(QColor(
                        CONFIDENCE_COLOR.get(hit.confidence_text, "#24292f")))
                if col == 5:
                    item.setForeground(QColor("#b45309"))
                self.result_table.setItem(index, col, item)
        total = len(self.report.findings) if self.report else 0
        shown = len(rows)
        self.result_label.setText(
            f"显示 {shown}/{total} 条" + ("" if shown == total else "（已过滤）"))
        if shown and not self.result_table.selectedItems():
            self.result_table.selectRow(0)

    def _on_row_selected(self) -> None:
        row = self.result_table.currentRow()
        if row < 0 or row >= len(self._filtered):
            return
        hit = self._filtered[row]
        lines = [
            f"【分类】{hit.category_label}",
            f"【置信度】{hit.confidence_text}（{hit.confidence}）"
            f"　【引擎】{'本地关键词 + LLM 辅助' if hit.llm_applied else '本地关键词'}",
            f"【来源页面】{hit.source_url}",
            f"【小节】{hit.heading or '（未识别）'}"
            f"　【位置】第 {hit.clause_index + 1} 条 / 字符偏移 {hit.char_offset}",
            f"【命中词条】{'、'.join(hit.matched_terms or []) or '（无）'}",
        ]
        if hit.context_note:
            lines.append(f"【语境提示】{hit.context_note}")
        if hit.llm_applied:
            lines.append(f"【LLM 辅助标注】{hit.llm_label or '（无）'}"
                         + (f"；{hit.llm_note}" if hit.llm_note else ""))
        lines += ["", "【条款原文（可据此回溯核对）】", hit.quote or "",
                  "", f"【{guard.REVIEW_NEEDED}】{hit.review_hint}"]
        if hit.llm_applied:
            lines.append("【说明】LLM 只提供分类标签与说明，原文引文始终取自本地抓取的条款文本。")
        self.detail_view.setPlainText("\n".join(lines))

    def _on_show_pages(self) -> None:
        """弹出页面清单（已扫描 / 跳过及原因）。"""
        if self.report is None:
            return
        report = self.report
        lines = [f"扫描时间：{report.scan_time}　引擎：{report.engine}", "",
                 "【已扫描页面】"]
        for page in report.pages:
            state = page.error or (f"HTTP {page.status}" if page.status else "成功")
            lines.append(f"· {page.url}\n    状态：{state}｜正文 {page.chars} 字符"
                         f"｜来源：{page.matched_by or '—'}")
        lines += ["", "【未扫描 / 被跳过】"]
        if report.skipped:
            for item in report.skipped:
                lines.append(f"· {item.get('url', '')}\n    原因：{item.get('reason', '')}")
        else:
            lines.append("（无）")
        lines += ["", "【robots.txt 参考信息】",
                  f"· {report.robots_summary.get('note', '（无）')}",
                  f"· 对本次 UA 是否被禁："
                  f"{'是' if report.robots_summary.get('disallowed_for_our_ua') else '否'}"
                  f"（{report.robots_summary.get('disclaimer', '')}）"]
        if report.report_path:
            lines += ["", f"【报告留档】{report.report_path}"]

        dialog = QDialog(self)
        dialog.setWindowTitle("已扫描 / 跳过页面")
        dialog.setMinimumSize(760, 560)
        box = QVBoxLayout(dialog)
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setPlainText("\n".join(lines))
        box.addWidget(view)
        row = QHBoxLayout()
        row.addStretch(1)
        ok = QPushButton("关闭")
        ok.clicked.connect(dialog.accept)
        row.addWidget(ok)
        box.addLayout(row)
        dialog.exec()

    # ------------------------------------------------------------------
    # 导出
    # ------------------------------------------------------------------
    def _on_export(self, fmt: str) -> None:
        if self.report is None:
            QMessageBox.information(self, "提示", "请先完成一次扫描")
            return
        filter_text, extension = EXPORT_FORMATS[fmt]
        default_name = os.path.splitext(report_base_name(self.report))[0]
        path, _selected = QFileDialog.getSaveFileName(
            self, f"导出 {fmt.upper()} 报告", f"{default_name}.{extension}",
            filter_text)
        if not path:
            return
        try:
            count = report_mod.write_report(path, fmt, self.report)
        except OSError as exc:
            QMessageBox.critical(self, "导出失败", str(exc))
            return
        self.log_view.appendPlainText(f"报告已导出：{path}（{count} 条线索）")
        QMessageBox.information(
            self, "导出完成",
            f"已导出 {count} 条线索。\n\n报告内含固定免责声明：本报告不构成法律意见，"
            "请由法务或律师复核后决定后续动作。")

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def closeEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        if self.thread is not None and self.thread.isRunning():
            self.thread.stop()
            self.thread.wait(3000)
        super().closeEvent(event)


__all__ = ["ComplianceDialog", "EXPORT_FORMATS", "CONFIDENCE_COLOR"]
