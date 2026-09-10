"""站点规则管理对话框：查看 / 编辑 / 校验 / 测试 / 导入导出 ``resources/rules/*.json``。

规则驱动的提取（``core.extractor.rule_extractor``）让"适配一个新站点"从改代码
变成改一份 JSON。本对话框是它的界面入口：

- 左侧规则列表（含启用状态与匹配域名），右侧 JSON 编辑器
- **校验**：静态检查（必填项、正则合法性、翻页策略）不联网
- **保存并重载**：写回文件后立即生效，无需重启
- **测试**：真的抓一次目标页面并跑提取，把命中的媒体列出来 ——
  这是配规则时最有用的反馈（选择器写错会立刻表现为"0 条命中"）

测试抓取放在独立线程里执行，避免阻塞界面。
"""

from __future__ import annotations

import json
import os

from PyQt6.QtCore import Qt, QThread, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QFont
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit,
    QPushButton, QSplitter, QVBoxLayout, QWidget,
)

from core.extractor import (
    RuleExtractor, SiteRule, delete_rule, detail_links_for, load_rules,
    page_rule_for, rules_dir, save_rule, validate_rule,
)
from core.fetcher import MODE_DYNAMIC, MODE_STATIC, create_fetcher
from core.media.models import MediaConfig

#: 新建规则时的骨架（可编辑字段均已给出示例）
NEW_RULE_TEMPLATE = {
    "id": "my-site",
    "name": "我的站点",
    "enabled": True,
    "priority": 50,
    "match": {"domain": ["example.com"], "url_regex": ""},
    "headers": {"Referer": "https://www.example.com/"},
    "pagination": {"strategy": "auto", "param": "page", "step": 1, "max_pages": 10},
    "media": [{"selector": "img.photo", "attr": "src", "kind": "image"}],
    "regex": [],
    "detail_links": {},
}


class RuleTestThread(QThread):
    """在后台抓取目标页面并跑一次规则提取（避免阻塞界面）。"""

    signal_log = pyqtSignal(str)
    signal_done = pyqtSignal(list, str)   # 提取结果行 / 错误说明

    def __init__(self, url: str, dynamic: bool = False, parent=None):
        super().__init__(parent)
        self.url = url
        self.dynamic = dynamic

    def run(self) -> None:
        fetcher = None
        try:
            self.signal_log.emit(f"抓取：{self.url}（{'浏览器渲染' if self.dynamic else '静态请求'}）")
            # page_timeout 仅动态抓取器支持，按模式分别构造参数
            if self.dynamic:
                fetcher = create_fetcher(MODE_DYNAMIC, request_delay=0.0,
                                         page_timeout=30.0)
            else:
                fetcher = create_fetcher(MODE_STATIC, request_delay=0.0)
            ctx = fetcher.fetch(self.url)
            if ctx.error:
                self.signal_done.emit([], f"抓取失败：{ctx.error}")
                return
            self.signal_log.emit(
                f"HTTP {ctx.status} | 类型 {ctx.content_type or '未知'} | "
                f"HTML {len(ctx.html)} 字符 | 网络事件 {len(ctx.network_events)}")
            apply = RuleExtractor(MediaConfig())
            items = list(apply.extract(ctx))
            lines = []
            for item in items:
                resolution = f" {item.resolution}" if item.resolution else ""
                lines.append(f"[{item.kind}{resolution}] {item.url}")
            # 顺带展示规则里的翻页与详情页配置是否生效，便于排查
            page_rule = page_rule_for(ctx.base_url)
            if page_rule is not None:
                self.signal_log.emit(
                    f"规则翻页：strategy={page_rule.strategy}"
                    + (f" template={page_rule.template}" if page_rule.template else ""))
            details = detail_links_for(ctx.base_url, ctx.html)
            if details:
                self.signal_log.emit(f"规则详情页链接：{len(details)} 个（{details[0]} 等）")
            self.signal_done.emit(lines, "")
        except Exception as exc:
            self.signal_done.emit([], f"{type(exc).__name__}: {str(exc)[:160]}")
        finally:
            if fetcher is not None:
                try:
                    fetcher.close()
                except Exception:
                    pass


class SiteRuleDialog(QDialog):
    """站点规则管理对话框。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("站点规则管理（resources/rules/*.json）")
        self.resize(1000, 680)
        self._test_thread = None
        self._build_ui()
        self.reload_rules()

    # ---------- 界面 ----------
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # 左侧：规则列表
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(QLabel("已加载规则："))
        self.rule_list = QListWidget()
        self.rule_list.currentRowChanged.connect(self._on_rule_selected)
        left_layout.addWidget(self.rule_list, 1)
        left_button_row = QHBoxLayout()
        for text, slot in (("新建", self.on_new), ("复制", self.on_duplicate),
                           ("删除", self.on_delete)):
            button = QPushButton(text)
            button.clicked.connect(slot)
            left_button_row.addWidget(button)
        left_layout.addLayout(left_button_row)
        left_button_row2 = QHBoxLayout()
        self.toggle_btn = QPushButton("启用/停用")
        self.toggle_btn.clicked.connect(self.on_toggle_enabled)
        left_button_row2.addWidget(self.toggle_btn)
        open_dir_btn = QPushButton("打开规则目录")
        open_dir_btn.clicked.connect(self.on_open_dir)
        left_button_row2.addWidget(open_dir_btn)
        left_layout.addLayout(left_button_row2)
        splitter.addWidget(left)

        # 右侧：编辑器 + 测试
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.addWidget(QLabel("规则内容（JSON）："))
        self.editor = QPlainTextEdit()
        self.editor.setFont(QFont("Consolas", 10))
        right_layout.addWidget(self.editor, 3)

        action_row = QHBoxLayout()
        validate_btn = QPushButton("校验")
        validate_btn.clicked.connect(self.on_validate)
        save_btn = QPushButton("保存并重载")
        save_btn.clicked.connect(self.on_save)
        import_btn = QPushButton("导入…")
        import_btn.clicked.connect(self.on_import)
        export_btn = QPushButton("导出…")
        export_btn.clicked.connect(self.on_export)
        for button in (validate_btn, save_btn, import_btn, export_btn):
            action_row.addWidget(button)
        action_row.addStretch()
        right_layout.addLayout(action_row)

        test_row = QHBoxLayout()
        test_row.addWidget(QLabel("测试地址："))
        self.test_url_input = QLineEdit()
        self.test_url_input.setPlaceholderText("填入命中该规则的页面地址，如 https://example.com/gallery/1")
        test_row.addWidget(self.test_url_input, 1)
        self.test_dynamic_check = QCheckBox("浏览器渲染")
        self.test_dynamic_check.setToolTip("页面靠 JS 渲染时勾选（需要浏览器内核）")
        test_row.addWidget(self.test_dynamic_check)
        self.test_btn = QPushButton("抓取并测试")
        self.test_btn.clicked.connect(self.on_test)
        test_row.addWidget(self.test_btn)
        right_layout.addLayout(test_row)

        right_layout.addWidget(QLabel("测试结果："))
        self.test_output = QPlainTextEdit()
        self.test_output.setReadOnly(True)
        self.test_output.setFont(QFont("Consolas", 9))
        right_layout.addWidget(self.test_output, 2)
        splitter.addWidget(right)
        splitter.setSizes([280, 720])
        outer.addWidget(splitter, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        outer.addWidget(buttons)

    # ---------- 列表与选中 ----------
    def reload_rules(self, select_id: str = "") -> None:
        """重新加载规则并刷新列表。"""
        self.rule_list.blockSignals(True)
        self.rule_list.clear()
        for rule in load_rules(force=True):
            label = (f"{'✔' if rule.enabled else '✘'} {rule.name or rule.id}"
                     f"　[{rule.id}]")
            if rule.domains:
                label += f"　{','.join(rule.domains[:2])}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, rule.id)
            self.rule_list.addItem(item)
        self.rule_list.blockSignals(False)
        if select_id:
            self._select_by_id(select_id)
        elif self.rule_list.count():
            self.rule_list.setCurrentRow(0)
        else:
            self.editor.setPlainText(json.dumps(NEW_RULE_TEMPLATE, ensure_ascii=False,
                                                indent=2))

    def _select_by_id(self, rule_id: str) -> None:
        for row in range(self.rule_list.count()):
            item = self.rule_list.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == rule_id:
                self.rule_list.setCurrentRow(row)
                return

    def _current_rule_id(self) -> str:
        item = self.rule_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else ""

    def _on_rule_selected(self, _row: int) -> None:
        rule = next((r for r in load_rules() if r.id == self._current_rule_id()), None)
        if rule is None:
            return
        payload = rule.to_dict()
        if rule.note:
            payload["_note"] = rule.note
        self.editor.setPlainText(json.dumps(payload, ensure_ascii=False, indent=2))
        self.test_output.clear()
        if rule.priority != 50:
            self.test_output.appendPlainText(f"（该规则优先级为 {rule.priority}）")

    # ---------- 编辑动作 ----------
    def _editor_payload(self) -> dict | None:
        """解析编辑器内容，失败时弹窗提示。"""
        text = self.editor.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, "提示", "规则内容为空")
            return None
        try:
            data = json.loads(text)
        except ValueError as exc:
            QMessageBox.warning(self, "JSON 解析失败", str(exc))
            return None
        if not isinstance(data, dict):
            QMessageBox.warning(self, "格式错误", "规则的最外层必须是 JSON 对象")
            return None
        return data

    def on_new(self) -> None:
        self.rule_list.setCurrentRow(-1)
        self.editor.setPlainText(json.dumps(NEW_RULE_TEMPLATE, ensure_ascii=False,
                                            indent=2))
        self.test_output.appendPlainText(
            "已载入规则骨架：把 id / match / media 改成目标站点的实际配置后点「保存并重载」。")

    def on_duplicate(self) -> None:
        data = self._editor_payload()
        if data is None:
            return
        data["id"] = f"{data.get('id', 'rule')}-copy"
        data["name"] = f"{data.get('name', '')} 副本"
        self.rule_list.setCurrentRow(-1)
        self.editor.setPlainText(json.dumps(data, ensure_ascii=False, indent=2))
        self.test_output.appendPlainText("已复制为副本，请修改 id 后保存。")

    def on_delete(self) -> None:
        rule_id = self._current_rule_id()
        if not rule_id:
            return
        reply = QMessageBox.question(
            self, "删除规则", f"确定删除规则「{rule_id}」对应的文件吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        if delete_rule(rule_id):
            self.reload_rules()
            self.test_output.appendPlainText(f"已删除规则：{rule_id}")
        else:
            QMessageBox.warning(self, "删除失败", "未找到对应的规则文件")

    def on_toggle_enabled(self) -> None:
        data = self._editor_payload()
        if data is None:
            return
        data["enabled"] = not bool(data.get("enabled", True))
        self.editor.setPlainText(json.dumps(data, ensure_ascii=False, indent=2))
        self.on_save()

    def on_validate(self) -> None:
        data = self._editor_payload()
        if data is None:
            return
        problems = validate_rule(data)
        self.test_output.clear()
        if problems:
            self.test_output.appendPlainText("校验未通过：")
            for problem in problems:
                self.test_output.appendPlainText(f"  - {problem}")
        else:
            self.test_output.appendPlainText(
                "校验通过：字段完整、正则合法、翻页策略有效。")

    def on_save(self) -> None:
        data = self._editor_payload()
        if data is None:
            return
        problems = validate_rule(data)
        if problems:
            self.test_output.clear()
            self.test_output.appendPlainText("保存前校验未通过：")
            for problem in problems:
                self.test_output.appendPlainText(f"  - {problem}")
            QMessageBox.warning(self, "校验未通过", "\n".join(problems))
            return
        rule = SiteRule.from_dict(data)
        try:
            path = save_rule(rule)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "保存失败", str(exc))
            return
        self.reload_rules(select_id=rule.id)
        self.test_output.appendPlainText(f"已保存并重载：{path}（立即生效，无需重启）")

    def on_open_dir(self) -> None:
        """在系统文件管理器中打开规则目录（跨平台）。"""
        directory = rules_dir()
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(directory)):
            QMessageBox.warning(self, "无法打开目录", directory)

    def on_import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "导入规则文件", "",
                                              "JSON 文件 (*.json);;所有文件 (*)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "导入失败", str(exc))
            return
        if isinstance(data, list):
            data = data[0] if data else {}
        self.rule_list.setCurrentRow(-1)
        self.editor.setPlainText(json.dumps(data, ensure_ascii=False, indent=2))
        self.test_output.appendPlainText(f"已导入：{path}（确认无误后点「保存并重载」）")

    def on_export(self) -> None:
        data = self._editor_payload()
        if data is None:
            return
        default_name = f"{data.get('id', 'rule')}.json"
        path, _ = QFileDialog.getSaveFileName(self, "导出规则文件", default_name,
                                              "JSON 文件 (*.json);;所有文件 (*)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
            self.test_output.appendPlainText(f"已导出：{path}")
        except OSError as exc:
            QMessageBox.critical(self, "导出失败", str(exc))

    # ---------- 测试 ----------
    def on_test(self) -> None:
        url = self.test_url_input.text().strip()
        if not url.startswith(("http://", "https://")):
            QMessageBox.warning(self, "提示", "请输入以 http(s):// 开头的测试地址")
            return
        if self._test_thread is not None and self._test_thread.isRunning():
            QMessageBox.information(self, "提示", "上一次测试仍在进行中，请稍候")
            return
        # 用编辑器中的内容临时落盘，保证测试的是"当前配置"而非已保存版本
        data = self._editor_payload()
        if data is None:
            return
        problems = validate_rule(data)
        if problems:
            self.test_output.clear()
            self.test_output.appendPlainText("规则校验未通过，请先修正：")
            for problem in problems:
                self.test_output.appendPlainText(f"  - {problem}")
            return
        try:
            save_rule(SiteRule.from_dict(data))
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "保存失败", str(exc))
            return

        self.test_output.clear()
        self.test_btn.setEnabled(False)
        self._test_thread = RuleTestThread(url, self.test_dynamic_check.isChecked(), self)
        self._test_thread.signal_log.connect(self.test_output.appendPlainText)
        self._test_thread.signal_done.connect(self._on_test_done)
        self._test_thread.start()

    def _on_test_done(self, lines, error) -> None:
        self.test_btn.setEnabled(True)
        if error:
            self.test_output.appendPlainText(error)
            return
        self.test_output.appendPlainText(f"规则提取命中 {len(lines)} 条媒体：")
        for line in lines[:200]:
            self.test_output.appendPlainText(f"  {line}")
        if not lines:
            self.test_output.appendPlainText(
                "  0 条命中：请检查 match.domain / url_regex 是否匹配该地址，"
                "以及 media.selector 是否与实际 DOM 一致（可勾选浏览器渲染后复查）")
        self.reload_rules(select_id=self._current_rule_id())

    def closeEvent(self, event) -> None:
        thread = self._test_thread
        if thread is not None and thread.isRunning():
            thread.wait(3000)
        event.accept()


__all__ = ["SiteRuleDialog", "RuleTestThread"]
