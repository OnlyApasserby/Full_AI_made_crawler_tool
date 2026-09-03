"""所有对话框：URL过滤规则管理 / 单任务导出配置 / 多任务添加与对比。

本模块只依赖 manager / core / models / utils，不引用主窗口，
需要主窗口数据时通过构造参数传入（如共享的 URLFilter / TaskScheduler）。
"""

from __future__ import annotations

import os
import time

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDoubleSpinBox,
    QFileDialog, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton,
    QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from core.filter import URLFilter
from models import (
    PRIORITY_TEXT, STATUS_COLORS, TaskConfig, TaskPriority, TaskStatus,
)
from utils.helpers import fmt_duration, now_str
from manager.task_manager import TaskScheduler, export_tasks_to_file


def _exists_color(hex_color: str) -> QColor:
    """把十六进制颜色字符串转为 QColor，非法值回退灰色。"""
    c = QColor(hex_color)
    return c if c.isValid() else QColor("#808080")


def datetime_stamp() -> str:
    """生成适合做文件名的日期时间戳。"""
    return now_str().replace(":", "-").replace(" ", "_")


# ===========================================================================
# 单任务：URL 过滤规则管理对话框
# ===========================================================================
class URLFilterDialog(QDialog):
    """URL过滤规则管理对话框。

    功能：屏蔽正则与域名白名单的增删、过滤统计与被过滤URL样本查看、
    规则导入/导出（JSON）。所有操作直接作用于共享的URLFilter实例，实时生效。
    """

    def __init__(self, parent: QWidget | None, url_filter: URLFilter) -> None:
        super().__init__(parent)
        self.url_filter = url_filter
        self.setWindowTitle("URL过滤规则管理")
        self.setMinimumSize(680, 540)
        self._build_ui()
        self._reload()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        # 过滤统计 + 被过滤URL样本
        stats_group = QGroupBox("过滤统计")
        stats_layout = QVBoxLayout(stats_group)
        self.stats_label = QLabel()
        stats_layout.addWidget(self.stats_label)
        self.samples_list = QListWidget()
        self.samples_list.setMaximumHeight(110)
        self.samples_list.setToolTip("最近被过滤的URL（最多保留100条）")
        stats_layout.addWidget(self.samples_list)
        layout.addWidget(stats_group)

        # 左右两栏：屏蔽正则 / 域名白名单
        split_row = QHBoxLayout()

        block_group = QGroupBox("屏蔽正则规则（任一命中即过滤）")
        block_layout = QVBoxLayout(block_group)
        self.block_list = QListWidget()
        self.block_list.setToolTip("匹配任一正则的URL将不会被爬取")
        block_layout.addWidget(self.block_list)
        block_input_row = QHBoxLayout()
        self.block_input = QLineEdit()
        self.block_input.setPlaceholderText("如 ^https://.*/private")
        self.block_add_btn = QPushButton("添加")
        self.block_add_btn.clicked.connect(self._on_add_block)
        self.block_del_btn = QPushButton("删除选中")
        self.block_del_btn.clicked.connect(self._on_del_block)
        block_input_row.addWidget(self.block_input, 1)
        block_input_row.addWidget(self.block_add_btn)
        block_input_row.addWidget(self.block_del_btn)
        block_layout.addLayout(block_input_row)
        split_row.addWidget(block_group, 1)

        allow_group = QGroupBox("域名白名单（配置后仅白名单域名可爬取）")
        allow_layout = QVBoxLayout(allow_group)
        self.allow_list = QListWidget()
        self.allow_list.setToolTip("留空表示不限制域名；配置后只有白名单内的域名会被爬取")
        allow_layout.addWidget(self.allow_list)
        allow_input_row = QHBoxLayout()
        self.allow_input = QLineEdit()
        self.allow_input.setPlaceholderText("如 www.example.com")
        self.allow_add_btn = QPushButton("添加")
        self.allow_add_btn.clicked.connect(self._on_add_allow)
        self.allow_del_btn = QPushButton("删除选中")
        self.allow_del_btn.clicked.connect(self._on_del_allow)
        allow_input_row.addWidget(self.allow_input, 1)
        allow_input_row.addWidget(self.allow_add_btn)
        allow_input_row.addWidget(self.allow_del_btn)
        allow_layout.addLayout(allow_input_row)
        split_row.addWidget(allow_group, 1)
        layout.addLayout(split_row)

        # 底部：导入 / 导出 / 关闭
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        import_btn = QPushButton("导入规则")
        import_btn.setToolTip("从JSON文件加载屏蔽正则与白名单（替换现有配置）")
        import_btn.clicked.connect(self._on_import)
        export_btn = QPushButton("导出规则")
        export_btn.setToolTip("将当前屏蔽正则与白名单保存为JSON文件")
        export_btn.clicked.connect(self._on_export)
        close_btn = QPushButton("关闭")
        close_btn.setDefault(True)
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(import_btn)
        btn_row.addWidget(export_btn)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

    # ---------- 界面刷新 ----------
    def _reload(self) -> None:
        """从url_filter读取最新状态并刷新界面"""
        stats = self.url_filter.get_stats()
        self.stats_label.setText(
            f"屏蔽正则：{stats['block_pattern_count']} 条 | "
            f"白名单域名：{stats['allow_domain_count']} 个 | "
            f"已过滤URL：{stats['filtered_count']} 个")
        self.block_list.clear()
        self.block_list.addItems(self.url_filter.block_patterns())
        self.allow_list.clear()
        self.allow_list.addItems(self.url_filter.allow_domains())
        self.samples_list.clear()
        for url in stats["recent_filtered"]:
            self.samples_list.addItem(url)

    # ---------- 屏蔽正则操作 ----------
    def _on_add_block(self) -> None:
        pattern = self.block_input.text().strip()
        if not pattern:
            QMessageBox.warning(self, "提示", "请输入屏蔽正则表达式")
            return
        if self.url_filter.add_block_pattern(pattern):
            self.block_input.clear()
            self._reload()
        else:
            QMessageBox.warning(self, "提示", "正则表达式无效或已存在，添加失败")

    def _on_del_block(self) -> None:
        item = self.block_list.currentItem()
        if item is None:
            QMessageBox.warning(self, "提示", "请先选中要删除的正则")
            return
        self.url_filter.remove_block_pattern(item.text())
        self._reload()

    # ---------- 域名白名单操作 ----------
    def _on_add_allow(self) -> None:
        domain = self.allow_input.text().strip()
        if not domain:
            QMessageBox.warning(self, "提示", "请输入域名")
            return
        if self.url_filter.add_allow_domain(domain):
            self.allow_input.clear()
            self._reload()
        else:
            QMessageBox.warning(self, "提示", "域名无效")

    def _on_del_allow(self) -> None:
        item = self.allow_list.currentItem()
        if item is None:
            QMessageBox.warning(self, "提示", "请先选中要删除的域名")
            return
        self.url_filter.remove_allow_domain(item.text())
        self._reload()

    # ---------- 配置导入 / 导出 ----------
    def _on_import(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(self, "导入过滤规则", "",
                                                   "JSON 文件 (*.json);;所有文件 (*)")
        if not file_path:
            return
        try:
            self.url_filter.load_config(file_path)
            self._reload()
            QMessageBox.information(self, "导入成功", f"已从 {file_path} 加载过滤规则")
        except (ValueError, OSError) as e:
            QMessageBox.critical(self, "导入失败", f"加载过滤规则失败：{str(e)}")

    def _on_export(self) -> None:
        default_name = f"url_filter_rules_{time.strftime('%Y%m%d_%H%M%S')}.json"
        file_path, _ = QFileDialog.getSaveFileName(self, "导出过滤规则", default_name,
                                                   "JSON 文件 (*.json);;所有文件 (*)")
        if not file_path:
            return
        try:
            self.url_filter.save_config(file_path)
            QMessageBox.information(self, "导出成功", f"已导出过滤规则到：\n{file_path}")
        except OSError as e:
            QMessageBox.critical(self, "导出失败", f"保存过滤规则失败：{str(e)}")


# ===========================================================================
# 单任务：导出配置对话框
# ===========================================================================
class ExportDialog(QDialog):
    """导出配置对话框：选择导出格式（CSV/JSON/Markdown）与导出字段（可拖拽排序、勾选）"""

    # 可选字段：(字段key, 显示名)
    FIELD_OPTIONS = [
        ("url", "链接地址 (url)"),
        ("domain", "域名 (domain)"),
        ("text", "页面文字 (text)"),
    ]

    def __init__(self, parent=None, default_format="CSV", has_text=True):
        super().__init__(parent)
        self.setWindowTitle("导出配置")
        self.setMinimumWidth(380)

        layout = QVBoxLayout(self)

        # 格式下拉菜单
        fmt_row = QHBoxLayout()
        fmt_row.addWidget(QLabel("导出格式："))
        self.format_combo = QComboBox()
        self.format_combo.addItems(["CSV", "JSON", "Markdown"])
        fmt_row.addWidget(self.format_combo)
        fmt_row.addStretch()
        layout.addLayout(fmt_row)

        # 字段列表（勾选 + 拖拽排序）
        layout.addWidget(QLabel("导出字段（勾选需要包含的字段，可拖拽调整顺序）："))
        self.field_list = QListWidget()
        self.field_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.field_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        for key, label in self.FIELD_OPTIONS:
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, key)
            if key == "text" and not has_text:
                # 无页面文字数据时禁用text字段
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                item.setCheckState(Qt.CheckState.Unchecked)
            else:
                item.setCheckState(Qt.CheckState.Checked)
            self.field_list.addItem(item)
        layout.addWidget(self.field_list)

        # 操作按钮
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        ok_btn = QPushButton("确定")
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self.accept)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(ok_btn)
        layout.addLayout(btn_row)

        # 预选格式
        idx = self.format_combo.findText(default_format)
        if idx >= 0:
            self.format_combo.setCurrentIndex(idx)

    def selected_format(self):
        """返回小写格式名：csv / json / markdown"""
        return self.format_combo.currentText().lower()

    def selected_fields(self):
        """按列表顺序返回已勾选的字段key列表"""
        fields = []
        for i in range(self.field_list.count()):
            item = self.field_list.item(i)
            if (item.flags() & Qt.ItemFlag.ItemIsEnabled and
                    item.checkState() == Qt.CheckState.Checked):
                fields.append(item.data(Qt.ItemDataRole.UserRole))
        return fields


# ===========================================================================
# 多任务：添加 / 编辑任务对话框
# ===========================================================================
class TaskAddDialog(QDialog):
    """添加任务对话框：支持一次添加多个起始URL（每行一个）。

    - 「使用全局默认配置」：复制标签页顶部的全局默认配置 + 主窗口屏蔽规则快照；
    - 「使用自定义配置」：本对话框内的配置应用到本批全部新任务；
    - 支持优先级与依赖任务ID（可多选/逗号分隔）。
    """

    def __init__(self, parent, defaults: TaskConfig, existing: list[dict],
                 global_patterns: list[str], global_domains: list[str],
                 title: str = "添加爬取任务", edit_cfg: TaskConfig | None = None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(640)
        self.defaults = defaults
        self.existing = existing
        self.global_patterns = list(global_patterns)
        self.global_domains = list(global_domains)
        self.edit_cfg = edit_cfg
        self._build_ui()
        if edit_cfg is not None:
            self._load_edit(edit_cfg)
        else:
            self._load_defaults()

    # ---------------- UI ----------------
    def _build_ui(self):
        root = QVBoxLayout(self)
        if self.edit_cfg is None:
            root.addWidget(QLabel("起始网址（每行一个，每个URL将创建为独立任务）："))
            self.urls_edit = QPlainTextEdit()
            self.urls_edit.setPlaceholderText("https://www.example.com\nhttps://www.example.org")
            self.urls_edit.setMaximumHeight(90)
            root.addWidget(self.urls_edit)
        else:
            self.urls_edit = None

        grp = QGroupBox("任务配置")
        lay = QVBoxLayout(grp)
        mode_row = QHBoxLayout()
        self.global_radio = QCheckBox("使用全局默认配置（与主窗口爬取配置页一致）")
        self.global_radio.setChecked(self.edit_cfg is None)
        mode_row.addWidget(self.global_radio)
        lay.addLayout(mode_row)

        form = QVBoxLayout()
        form.setSpacing(4)
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("深度："))
        self.depth_spin = QSpinBox()
        self.depth_spin.setRange(0, 5)
        row1.addWidget(self.depth_spin)
        row1.addSpacing(10)
        row1.addWidget(QLabel("请求间隔："))
        self.delay_spin = QDoubleSpinBox()
        self.delay_spin.setRange(0.0, 10.0)
        self.delay_spin.setSingleStep(0.1)
        self.delay_spin.setSuffix(" 秒")
        row1.addWidget(self.delay_spin)
        row1.addSpacing(10)
        row1.addWidget(QLabel("每页字符："))
        self.chars_spin = QSpinBox()
        self.chars_spin.setRange(0, 10_000_000)
        self.chars_spin.setSingleStep(10000)
        self.chars_spin.setSuffix(" (0不限)")
        row1.addWidget(self.chars_spin)
        row1.addStretch()
        form.addLayout(row1)

        row2 = QHBoxLayout()
        self.external_check = QCheckBox("允许站外链接")
        row2.addWidget(self.external_check)
        self.robots_check = QCheckBox("校验robots.txt（任务级）")
        self.robots_check.setChecked(True)
        row2.addWidget(self.robots_check)
        row2.addSpacing(10)
        row2.addWidget(QLabel("最大页数："))
        self.max_pages_spin = QSpinBox()
        self.max_pages_spin.setRange(0, 1_000_000)
        self.max_pages_spin.setSuffix(" (0不限)")
        row2.addWidget(self.max_pages_spin)
        row2.addSpacing(10)
        row2.addWidget(QLabel("自动重试："))
        self.retries_spin = QSpinBox()
        self.retries_spin.setRange(0, 10)
        row2.addWidget(self.retries_spin)
        row2.addStretch()
        form.addLayout(row2)

        row3 = QHBoxLayout()
        row3.addWidget(QLabel("定向关键词："))
        self.link_filter_edit = QLineEdit()
        self.link_filter_edit.setPlaceholderText("仅沿包含该关键词的链接继续递归，留空不限")
        row3.addWidget(self.link_filter_edit, 1)
        row3.addSpacing(10)
        row3.addWidget(QLabel("优先级："))
        self.priority_combo = QComboBox()
        for val, text in PRIORITY_TEXT.items():
            self.priority_combo.addItem(text, val)
        row3.addWidget(self.priority_combo)
        form.addLayout(row3)

        row4 = QHBoxLayout()
        row4.addWidget(QLabel("依赖任务ID："))
        self.depends_edit = QLineEdit()
        self.depends_edit.setPlaceholderText("任务B依赖任务A完成，填A的任务ID；多个用逗号分隔")
        row4.addWidget(self.depends_edit, 1)
        form.addLayout(row4)

        row5 = QHBoxLayout()
        row5.addWidget(QLabel("屏蔽URL正则："))
        self.block_edit = QLineEdit()
        self.block_edit.setPlaceholderText("任务级屏蔽（分号;分隔），仅作用于本任务")
        row5.addWidget(self.block_edit, 1)
        form.addLayout(row5)

        row6 = QHBoxLayout()
        row6.addWidget(QLabel("域名白名单："))
        self.domains_edit = QLineEdit()
        self.domains_edit.setPlaceholderText("任务级白名单（逗号,分隔），配置后仅爬取这些域名")
        row6.addWidget(self.domains_edit, 1)
        form.addLayout(row6)
        lay.addLayout(form)
        root.addWidget(grp)

        self.existing_label = QLabel()
        self.existing_label.setWordWrap(True)
        self.existing_label.setStyleSheet("color: #666;")
        root.addWidget(self.existing_label)

        btns = QHBoxLayout()
        self.ok_btn = QPushButton("确定")
        self.ok_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btns.addStretch()
        btns.addWidget(self.ok_btn)
        btns.addWidget(cancel_btn)
        root.addLayout(btns)
        self._refresh_existing()

    def _refresh_existing(self):
        if self.existing:
            summary = "现有任务：\n" + "\n".join(
                f"  ID {m['id']}: {m['name']} [{m['status_text']}]" for m in self.existing[:12])
            self.existing_label.setText(summary)
        else:
            self.existing_label.setText("")

    def _load_defaults(self):
        d = self.defaults
        self.depth_spin.setValue(d.max_depth)
        self.delay_spin.setValue(d.request_delay)
        self.chars_spin.setValue(d.max_chars_per_page)
        self.external_check.setChecked(d.crawl_external)
        self.robots_check.setChecked(d.robots_check)
        self.max_pages_spin.setValue(d.max_pages)
        self.retries_spin.setValue(d.max_retries)
        self.link_filter_edit.setText(d.link_filter)
        self.priority_combo.setCurrentIndex(max(0, d.priority))
        self.block_edit.setText(";".join(d.block_patterns))
        self.domains_edit.setText(",".join(d.allow_domains))
        self._toggle_custom(self.global_radio.isChecked())
        self.global_radio.toggled.connect(self._toggle_custom)

    def _toggle_custom(self, use_global: bool):
        # 优先级与依赖任务与配置来源无关，始终可设置
        for w in (self.depth_spin, self.delay_spin, self.chars_spin, self.external_check,
                  self.robots_check, self.max_pages_spin, self.retries_spin,
                  self.link_filter_edit, self.block_edit, self.domains_edit):
            w.setEnabled(not use_global)

    def _load_edit(self, cfg: TaskConfig):
        self.global_radio.setChecked(False)
        self.global_radio.setEnabled(False)
        self.depth_spin.setValue(cfg.max_depth)
        self.delay_spin.setValue(cfg.request_delay)
        self.chars_spin.setValue(cfg.max_chars_per_page)
        self.external_check.setChecked(cfg.crawl_external)
        self.robots_check.setChecked(cfg.robots_check)
        self.max_pages_spin.setValue(cfg.max_pages)
        self.retries_spin.setValue(cfg.max_retries)
        self.link_filter_edit.setText(cfg.link_filter)
        self.priority_combo.setCurrentIndex(max(0, min(2, cfg.priority)))
        self.block_edit.setText(";".join(cfg.block_patterns))
        self.domains_edit.setText(",".join(cfg.allow_domains))
        self.depends_edit.setText(",".join(str(x) for x in cfg.depends_on))
        self.depends_edit.setEnabled(True)

    # ---------------- 取值 ----------------
    def _collect(self, url: str) -> TaskConfig:
        priority = self.priority_combo.currentData() or TaskPriority.MEDIUM
        depends = self._parse_depends()
        use_global = self.global_radio.isChecked()
        if use_global:
            cfg = TaskConfig(start_url=url.strip())
            d = self.defaults
            cfg.max_depth = d.max_depth
            cfg.crawl_external = d.crawl_external
            cfg.request_delay = d.request_delay
            cfg.max_chars_per_page = d.max_chars_per_page
            cfg.link_filter = d.link_filter
            cfg.jitter = d.jitter
            cfg.max_pages = d.max_pages
            cfg.max_retries = d.max_retries
            cfg.robots_check = d.robots_check
            cfg.priority = priority
            cfg.depends_on = depends
            cfg.block_patterns = list(self.global_patterns)  # 主窗口屏蔽规则快照（任务级）
            cfg.allow_domains = list(self.global_domains)
            return cfg
        return TaskConfig(
            start_url=url.strip(),
            max_depth=self.depth_spin.value(),
            request_delay=self.delay_spin.value(),
            max_chars_per_page=self.chars_spin.value(),
            crawl_external=self.external_check.isChecked(),
            robots_check=self.robots_check.isChecked(),
            max_pages=self.max_pages_spin.value(),
            max_retries=self.retries_spin.value(),
            link_filter=self.link_filter_edit.text().strip(),
            priority=priority,
            depends_on=depends,
            block_patterns=[p for p in (p.strip() for p in self.block_edit.text().split(";")) if p],
            allow_domains=[d for d in (x.strip() for x in self.domains_edit.text().split(",")) if d],
        )

    def _parse_depends(self) -> list[int]:
        ids = []
        for tok in self.depends_edit.text().replace("；", ";").replace("，", ",").split(","):
            tok = tok.strip()
            if not tok:
                continue
            try:
                i = int(tok)
            except ValueError:
                continue
            if i in ids:
                continue
            if not any(m["id"] == i for m in self.existing):
                QMessageBox.warning(self, "依赖任务不存在", f"依赖任务ID {i} 不存在，已忽略")
                continue
            ids.append(i)
        return ids

    def result_configs(self) -> list[TaskConfig]:
        """返回本批新增任务的配置列表（编辑模式返回单个）"""
        if self.edit_cfg is not None:
            return [self._collect(self.edit_cfg.start_url)]
        configs = []
        for raw in self.urls_edit.toPlainText().splitlines():
            url = raw.strip()
            if not url:
                continue
            if not url.startswith(("http://", "https://")):
                QMessageBox.warning(self, "无效网址", f"已跳过无效网址：{url}")
                continue
            configs.append(self._collect(url))
        return configs


# ===========================================================================
# 多任务：对比汇总对话框
# ===========================================================================
class TaskCompareDialog(QDialog):
    """任务对比汇总对话框：各任务完成情况对比 + 合并导出。"""

    def __init__(self, parent, scheduler: TaskScheduler):
        super().__init__(parent)
        self.scheduler = scheduler
        self.setWindowTitle("任务对比汇总")
        self.resize(880, 460)
        self._build_ui()
        self._reload()

    def _build_ui(self):
        root = QVBoxLayout(self)
        self.table = QTableWidget(0, 10)
        self.table.setHorizontalHeaderLabels(
            ["ID", "任务名/起始URL", "状态", "页面数", "链接数", "媒体数",
             "当前深度/最大", "重试", "耗时", "失败原因"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(9, QHeaderView.ResizeMode.Stretch)
        root.addWidget(self.table)

        btns = QHBoxLayout()
        self.export_btn = QPushButton("合并导出全部任务...")
        self.export_btn.clicked.connect(self._on_export)
        btns.addWidget(self.export_btn)
        btns.addStretch()
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        btns.addWidget(close_btn)
        root.addLayout(btns)

    def _reload(self):
        metas = self.scheduler.task_metas()
        self.table.setRowCount(len(metas))
        totals = self.scheduler.summary()
        for r, m in enumerate(metas):
            values = [
                str(m["id"]),
                f"{m['name']}\n{m['start_url']}",
                m["status_text"],
                str(m["pages"]),
                str(m["link_count"]),
                str(m["media_count"]),
                f"{m['current_depth']}/{m['max_depth']}",
                f"{m['retry_count']}/{m['max_retries']}",
                fmt_duration(m["duration"]),
                m["error"] or "",
            ]
            for c, v in enumerate(values):
                item = QTableWidgetItem(v)
                if c == 2:
                    item.setForeground(_exists_color(STATUS_COLORS.get(m["status"], "#808080")))
                if c == 0:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(r, c, item)
        # 汇总行
        r = self.table.rowCount()
        self.table.insertRow(r)
        total_text = (f"合计 {totals['total']} 个任务：已完成 {totals[TaskStatus.COMPLETED]}，"
                      f"执行中 {totals[TaskStatus.RUNNING]}，暂停 {totals[TaskStatus.PAUSED]}，"
                      f"失败 {totals[TaskStatus.FAILED]}")
        item = QTableWidgetItem(total_text)
        item.setForeground(QColor("#1565c0"))
        self.table.setItem(r, 1, item)
        self.table.setItem(r, 3, QTableWidgetItem(str(totals["pages"])))
        self.table.setItem(r, 4, QTableWidgetItem(str(totals["links"])))
        self.table.setItem(r, 5, QTableWidgetItem(str(totals["media"])))
        self.table.setItem(r, 8, QTableWidgetItem(fmt_duration(totals["elapsed"])))

    def _on_export(self):
        rows = self.scheduler.merged_results()
        if not rows:
            QMessageBox.information(self, "导出", "没有可导出的任务结果（先运行任务）")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "合并导出全部任务结果",
            os.path.join(os.getcwd(), f"tasks_export_{datetime_stamp()}.csv"),
            "CSV 文件 (*.csv);;JSON 文件 (*.json);;Markdown 文件 (*.md)")
        if not path:
            return
        try:
            n = export_tasks_to_file(rows, path)
            QMessageBox.information(self, "导出成功", f"已导出 {n} 条记录到：\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))


__all__ = [
    "URLFilterDialog", "ExportDialog", "TaskAddDialog", "TaskCompareDialog",
    "datetime_stamp",
]
