"""广告 / 钓鱼链接安全检测对话框。

功能：
- 粘贴待检测 URL（每行一个），或一键载入本次爬取结果
- 展示识别结果（广告 / 钓鱼 / 风险等级 / 命中规则 / 说明）
- 汇总去重后的“同类模式”屏蔽正则，支持勾选后一键加入共享的屏蔽列表（实时生效）
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView, QDialog, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from core.filter import URLFilter
from core.security import (
    CLASS_AD, CLASS_PHISHING, analyze_urls, collect_regex_suggestions, summarize,
)

# 风险等级显示与配色
SEVERITY_TEXT = {"high": "高", "medium": "中", "low": "低"}
SEVERITY_COLOR = {"high": "#c0392b", "medium": "#e67e22", "low": "#7f8c8d"}


def _kind_text(kinds: set[str]) -> str:
    """类别集合 -> 显示文本"""
    parts = []
    if CLASS_AD in kinds:
        parts.append("广告")
    if CLASS_PHISHING in kinds:
        parts.append("钓鱼")
    return "+".join(parts) if parts else "正常"


class LinkGuardDialog(QDialog):
    """广告/钓鱼链接安全检测与屏蔽正则生成对话框。

    :param url_filter: 共享的 URLFilter 实例，选中的正则直接写入并实时生效
    :param initial_urls: 初始候选 URL（通常为本次爬取结果，可一键载入）
    """

    def __init__(self, parent: QWidget | None, url_filter: URLFilter,
                 initial_urls: list[str] | None = None) -> None:
        super().__init__(parent)
        self.url_filter = url_filter
        self.initial_urls = [u for u in (initial_urls or []) if u]
        self._analyses = []
        self._suggestions = []
        self.setWindowTitle("广告 / 钓鱼链接安全检测")
        self.setMinimumSize(960, 700)
        self._build_ui()

    # ---------- 界面 ----------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        # 1. 输入区
        input_group = QGroupBox("待检测链接（每行一个，可含完整URL或域名）")
        input_layout = QVBoxLayout(input_group)
        self.url_input = QPlainTextEdit()
        self.url_input.setPlaceholderText(
            "https://example.com/page?utm_source=ad&gclid=123\n"
            "http://paypa1-secure.tk/login\n"
            "http://bit.ly/xxxx")
        self.url_input.setMaximumHeight(110)
        input_layout.addWidget(self.url_input)

        input_btn_row = QHBoxLayout()
        self.scan_btn = QPushButton("开始检测")
        self.scan_btn.setDefault(True)
        self.scan_btn.clicked.connect(self._on_scan)
        self.load_crawl_btn = QPushButton(
            f"载入爬取结果（{len(self.initial_urls)}）" if self.initial_urls
            else "载入爬取结果")
        self.load_crawl_btn.setToolTip("把本次爬取得到的链接填入待检测列表")
        self.load_crawl_btn.setEnabled(bool(self.initial_urls))
        self.load_crawl_btn.clicked.connect(self._on_load_crawl_results)
        self.clear_btn = QPushButton("清空")
        self.clear_btn.clicked.connect(self._on_clear)
        input_btn_row.addWidget(self.scan_btn)
        input_btn_row.addWidget(self.load_crawl_btn)
        input_btn_row.addWidget(self.clear_btn)
        input_btn_row.addStretch(1)
        self.stats_label = QLabel("尚未检测")
        input_btn_row.addWidget(self.stats_label)
        input_layout.addLayout(input_btn_row)
        layout.addWidget(input_group)

        # 2. 识别结果
        result_group = QGroupBox("识别结果（点击行查看详情）")
        result_layout = QVBoxLayout(result_group)
        self.result_table = QTableWidget(0, 5)
        self.result_table.setHorizontalHeaderLabels(
            ["URL", "类别", "风险", "命中规则", "说明"])
        self.result_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.result_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.result_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.result_table.verticalHeader().setVisible(False)
        header = self.result_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in (1, 2, 3):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.result_table.itemSelectionChanged.connect(self._on_result_selected)
        result_layout.addWidget(self.result_table)

        self.detail_view = QPlainTextEdit()
        self.detail_view.setReadOnly(True)
        self.detail_view.setPlaceholderText("选中上方结果可查看该链接的全部命中详情")
        self.detail_view.setMaximumHeight(130)
        result_layout.addWidget(self.detail_view)
        layout.addWidget(result_group, 1)

        # 3. 建议屏蔽正则
        regex_group = QGroupBox("建议屏蔽正则（已归纳为覆盖同类模式的通用规则）")
        regex_layout = QVBoxLayout(regex_group)
        self.regex_list = QListWidget()
        self.regex_list.setToolTip("勾选要加入屏蔽列表的规则；取消勾选的规则不会被添加")
        self.regex_list.setMaximumHeight(150)
        self.regex_list.itemSelectionChanged.connect(self._on_regex_selected)
        self.regex_list.itemChanged.connect(self._on_regex_item_changed)
        regex_layout.addWidget(self.regex_list)

        regex_btn_row = QHBoxLayout()
        select_all_btn = QPushButton("全选")
        select_all_btn.clicked.connect(lambda: self._set_all_checked(True))
        select_none_btn = QPushButton("取消全选")
        select_none_btn.clicked.connect(lambda: self._set_all_checked(False))
        self.apply_btn = QPushButton("加入选中规则到屏蔽列表")
        self.apply_btn.setEnabled(False)
        self.apply_btn.clicked.connect(self._on_apply)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        regex_btn_row.addWidget(select_all_btn)
        regex_btn_row.addWidget(select_none_btn)
        regex_btn_row.addStretch(1)
        regex_btn_row.addWidget(self.apply_btn)
        regex_btn_row.addWidget(close_btn)
        regex_layout.addLayout(regex_btn_row)
        layout.addWidget(regex_group)

    # ---------- 输入操作 ----------
    def _on_load_crawl_results(self) -> None:
        """把主窗口传入的爬取结果填入输入框"""
        if not self.initial_urls:
            QMessageBox.information(self, "提示", "本次爬取暂无可用的链接结果")
            return
        self.url_input.setPlainText("\n".join(self.initial_urls))

    def _on_clear(self) -> None:
        self.url_input.clear()
        self.result_table.setRowCount(0)
        self.regex_list.clear()
        self.detail_view.clear()
        self._analyses = []
        self._suggestions = []
        self.stats_label.setText("尚未检测")
        self.apply_btn.setEnabled(False)

    # ---------- 检测 ----------
    def _on_scan(self) -> None:
        """识别输入框中的全部链接并刷新结果与正则建议"""
        urls = [line.strip() for line in self.url_input.toPlainText().splitlines()
                if line.strip()]
        if not urls:
            QMessageBox.warning(self, "提示", "请先输入待检测的链接（每行一个）")
            return

        self._analyses = analyze_urls(urls)
        self._suggestions = collect_regex_suggestions(self._analyses)
        self._fill_result_table()
        self._fill_regex_list()

        stats = summarize(self._analyses)
        self.stats_label.setText(
            f"共检测 {stats['url_count']} 条 | 广告 {stats['ad_count']} 条 | "
            f"钓鱼 {stats['phishing_count']} 条 | 高风险 {stats['high_count']} 条 | "
            f"建议规则 {stats['regex_count']} 条")

    def _fill_result_table(self) -> None:
        self.result_table.setRowCount(0)
        for analysis in self._analyses:
            row = self.result_table.rowCount()
            self.result_table.insertRow(row)
            url_item = QTableWidgetItem(analysis.url)
            url_item.setToolTip(analysis.url)
            self.result_table.setItem(row, 0, url_item)

            kinds = _kind_text(analysis.kinds)
            kind_item = QTableWidgetItem(kinds)
            if analysis.is_phishing:
                kind_item.setForeground(QColor(SEVERITY_COLOR["high"]))
            elif analysis.is_ad:
                kind_item.setForeground(QColor(SEVERITY_COLOR["medium"]))
            self.result_table.setItem(row, 1, kind_item)

            level = analysis.risk_level
            risk_item = QTableWidgetItem(SEVERITY_TEXT.get(level, "-"))
            if level:
                risk_item.setForeground(QColor(SEVERITY_COLOR.get(level, "#000000")))
            self.result_table.setItem(row, 2, risk_item)

            self.result_table.setItem(row, 3, QTableWidgetItem(
                "、".join(dict.fromkeys(f.title for f in analysis.findings)) or "-"))
            brief = analysis.findings[0].detail if analysis.findings else "未发现广告/钓鱼特征"
            self.result_table.setItem(row, 4, QTableWidgetItem(brief))

    def _fill_regex_list(self) -> None:
        self.regex_list.blockSignals(True)
        self.regex_list.clear()
        for sug in self._suggestions:
            label = (f"[{SEVERITY_TEXT.get(sug.severity, '-')}] {sug.title}"
                     f"（命中 {sug.count} 条）")
            if not sug.auto_select:
                label += "  ⚠ 需人工确认"
            item = QListWidgetItem(label)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if sug.auto_select
                               else Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, sug)
            item.setToolTip(f"{sug.regex}\n\n{sug.hint}")
            self.regex_list.addItem(item)
        self.regex_list.blockSignals(False)
        self.apply_btn.setEnabled(bool(self._suggestions))

    # ---------- 详情展示 ----------
    def _on_result_selected(self) -> None:
        rows = self.result_table.selectionModel().selectedRows()
        if not rows:
            return
        analysis = self._analyses[rows[0].row()]
        lines = [analysis.url, ""]
        if not analysis.findings:
            lines.append("未发现广告或钓鱼特征。")
        for finding in analysis.findings:
            lines.append(f"[{SEVERITY_TEXT.get(finding.severity, '-')}] "
                         f"{finding.title}：{finding.detail}")
            if finding.hint:
                lines.append(f"    提示：{finding.hint}")
        self.detail_view.setPlainText("\n".join(lines))

    def _on_regex_selected(self) -> None:
        item = self.regex_list.currentItem()
        if item is None:
            return
        sug = item.data(Qt.ItemDataRole.UserRole)
        self.detail_view.setPlainText(
            f"规则名称：{sug.title}\n"
            f"来源示例：{sug.sample_url}\n"
            f"命中数量：{sug.count}\n\n"
            f"正则表达式：\n{sug.regex}\n\n"
            f"说明：{sug.hint}")

    def _on_regex_item_changed(self, _item) -> None:
        self.apply_btn.setEnabled(
            any(self.regex_list.item(i).checkState() == Qt.CheckState.Checked
                for i in range(self.regex_list.count())))

    def _set_all_checked(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for i in range(self.regex_list.count()):
            self.regex_list.item(i).setCheckState(state)

    # ---------- 应用规则 ----------
    def _on_apply(self) -> None:
        """把勾选的正则写入共享的屏蔽列表（实时生效）"""
        selected = []
        for i in range(self.regex_list.count()):
            item = self.regex_list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                selected.append(item.data(Qt.ItemDataRole.UserRole))
        if not selected:
            QMessageBox.warning(self, "提示", "请至少勾选一条要加入的规则")
            return

        added, skipped = 0, 0
        for sug in selected:
            if self.url_filter.add_block_pattern(sug.regex):
                added += 1
            else:
                skipped += 1
        msg = f"已添加 {added} 条屏蔽正则"
        if skipped:
            msg += f"，{skipped} 条已存在"
        msg += "。\n规则已实时生效，关闭后可在主界面「屏蔽URL正则」中查看。"
        QMessageBox.information(self, "已加入屏蔽列表", msg)
        self.accept()


__all__ = ["LinkGuardDialog"]
