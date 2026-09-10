"""自定义控件：「任务管理」标签页 TaskManagerTab。

多目标并发爬取的界面入口：并发控制 + 全局默认配置 + 任务列表表格 +
批量/单选操作 + 详情与实时日志 + 汇总统计与导出。
"""

from __future__ import annotations

import os

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDoubleSpinBox, QFileDialog, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
    QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from models import STATUS_COLORS, TaskConfig, TaskStatus
from utils.helpers import fmt_duration
from manager.task_manager import TaskScheduler, export_tasks_to_file
from .dialogs import TaskAddDialog, TaskCompareDialog


def _exists_color(hex_color: str) -> QColor:
    """把十六进制颜色字符串转为 QColor，非法值回退灰色。"""
    c = QColor(hex_color)
    return c if c.isValid() else QColor("#808080")


class TaskManagerTab(QWidget):
    """「任务管理」标签页：并发控制 + 任务列表 + 批量操作 + 汇总统计/导出。"""

    def __init__(self, main_window=None, scheduler: TaskScheduler | None = None):
        super().__init__()
        self.main_window = main_window
        self.scheduler = scheduler or TaskScheduler(
            max_concurrent=3,
            ua_pool=getattr(main_window, "ua_pool", None) if main_window is not None else None)
        self._row_of_id: dict[int, int] = {}
        self._selected_id: int | None = None
        self._build_ui()
        self._connect_signals()
        self._clock = QTimer(self)
        self._clock.timeout.connect(self._tick_clock)
        self._clock.start(1000)
        # 自动恢复未完成任务（重启后恢复）
        try:
            restored = self.scheduler.load_queue()
            if restored:
                self._global_note.setText(f"已从队列文件自动恢复 {restored} 个未完成任务（点击\"开始\"执行）")
        except Exception:
            pass
        self._refresh_summary()

    # ---------------- 构建UI ----------------
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(6)
        root.setContentsMargins(6, 6, 6, 6)

        # ---- 顶部：并发 + 全局默认配置 ----
        cfg_box = QGroupBox("并发控制与全局默认配置（添加任务时可改为独立配置）")
        cfg_lay = QVBoxLayout(cfg_box)
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("最大并发任务数："))
        self.concurrent_spin = QSpinBox()
        self.concurrent_spin.setRange(1, 32)
        self.concurrent_spin.setValue(self.scheduler.get_max_concurrent())
        self.concurrent_spin.setToolTip("并发爬虫线程池大小，可运行时动态调整")
        row1.addWidget(self.concurrent_spin)
        row1.addSpacing(16)
        row1.addWidget(QLabel("默认深度："))
        self.depth_spin = QSpinBox()
        self.depth_spin.setRange(0, 5)
        self.depth_spin.setValue(1)
        row1.addWidget(self.depth_spin)
        row1.addSpacing(10)
        row1.addWidget(QLabel("默认间隔："))
        self.delay_spin = QDoubleSpinBox()
        self.delay_spin.setRange(0.0, 10.0)
        self.delay_spin.setSingleStep(0.1)
        self.delay_spin.setValue(0.5)
        self.delay_spin.setSuffix(" s")
        row1.addWidget(self.delay_spin)
        row1.addSpacing(10)
        row1.addWidget(QLabel("默认字符："))
        self.chars_spin = QSpinBox()
        self.chars_spin.setRange(0, 10_000_000)
        self.chars_spin.setSingleStep(10000)
        row1.addWidget(self.chars_spin)
        row1.addStretch()
        cfg_lay.addLayout(row1)

        row2 = QHBoxLayout()
        self.external_check = QCheckBox("允许站外链接")
        row2.addWidget(self.external_check)
        self.robots_check = QCheckBox("校验robots.txt")
        self.robots_check.setChecked(True)
        row2.addWidget(self.robots_check)
        row2.addSpacing(10)
        row2.addWidget(QLabel("默认重试次数："))
        self.retries_spin = QSpinBox()
        self.retries_spin.setRange(0, 10)
        self.retries_spin.setValue(2)
        row2.addWidget(self.retries_spin)
        row2.addSpacing(10)
        row2.addWidget(QLabel("下载根目录："))
        self.download_dir_edit = QLineEdit(self.scheduler.base_download_dir)
        row2.addWidget(self.download_dir_edit, 1)
        browse_btn = QPushButton("浏览")
        browse_btn.clicked.connect(self._on_browse_download_dir)
        row2.addWidget(browse_btn)
        row2.addSpacing(10)
        self.save_queue_btn = QPushButton("保存队列")
        self.save_queue_btn.setToolTip("将未完成任务保存到 task_data/task_queue.json（关闭时自动保存）")
        self.save_queue_btn.clicked.connect(self._on_save_queue)
        row2.addWidget(self.save_queue_btn)
        self.load_queue_btn = QPushButton("载入队列")
        self.load_queue_btn.setToolTip("从队列文件恢复未完成任务")
        self.load_queue_btn.clicked.connect(self._on_load_queue)
        row2.addWidget(self.load_queue_btn)
        cfg_lay.addLayout(row2)
        root.addWidget(cfg_box)

        # ---- 操作按钮 ----
        act = QHBoxLayout()
        add_btn = QPushButton("添加任务...")
        add_btn.setToolTip("支持一次添加多个起始URL（每行一个），可设置优先级/依赖/任务级过滤")
        add_btn.clicked.connect(self._on_add)
        act.addWidget(add_btn)
        act.addSpacing(8)
        self.select_all_btn = QPushButton("全选")
        self.select_all_btn.clicked.connect(lambda: self._set_all_checked(True))
        act.addWidget(self.select_all_btn)
        self.unselect_btn = QPushButton("取消全选")
        self.unselect_btn.clicked.connect(lambda: self._set_all_checked(False))
        act.addWidget(self.unselect_btn)
        act.addSpacing(8)
        self.start_sel_btn = QPushButton("批量启动")
        self.start_sel_btn.setToolTip("启动选中任务（暂停->恢复，失败->重试）")
        self.start_sel_btn.clicked.connect(self._on_start_selected)
        act.addWidget(self.start_sel_btn)
        self.pause_sel_btn = QPushButton("批量暂停")
        self.pause_sel_btn.clicked.connect(self._on_pause_selected)
        act.addWidget(self.pause_sel_btn)
        self.resume_sel_btn = QPushButton("批量继续")
        self.resume_sel_btn.clicked.connect(self._on_resume_selected)
        act.addWidget(self.resume_sel_btn)
        self.delete_sel_btn = QPushButton("批量删除")
        self.delete_sel_btn.clicked.connect(self._on_delete_selected)
        act.addWidget(self.delete_sel_btn)
        act.addSpacing(8)
        self.start_all_btn = QPushButton("启动全部")
        self.start_all_btn.clicked.connect(self.scheduler.start_all)
        act.addWidget(self.start_all_btn)
        self.pause_all_btn = QPushButton("暂停全部")
        self.pause_all_btn.clicked.connect(self.scheduler.pause_all)
        act.addWidget(self.pause_all_btn)
        self.stop_all_btn = QPushButton("停止全部")
        self.stop_all_btn.clicked.connect(self.scheduler.stop_all)
        act.addWidget(self.stop_all_btn)
        act.addStretch()
        self.compare_btn = QPushButton("任务对比汇总...")
        self.compare_btn.clicked.connect(self._on_compare)
        act.addWidget(self.compare_btn)
        self.export_btn = QPushButton("汇总导出...")
        self.export_btn.clicked.connect(self._on_export_all)
        act.addWidget(self.export_btn)
        root.addLayout(act)

        # ---- 表格 + 详情 ----
        split = QSplitter(Qt.Orientation.Horizontal)
        self.table = QTableWidget(0, 13)
        self.table.setHorizontalHeaderLabels(
            ["选择", "ID", "任务/起始URL", "状态", "优先级", "进度", "页面数",
             "链接数", "深度", "媒体", "重试", "耗时", "备注"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for col in (0, 1, 3, 4, 5, 6, 7, 8, 9, 10, 11):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(12, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 44)
        split.addWidget(self.table)

        right = QWidget()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(0, 0, 0, 0)
        self.detail_label = QLabel("选中任务详情：\n（点击表格行查看）")
        self.detail_label.setWordWrap(True)
        self.detail_label.setStyleSheet("background:#f5f5f5;padding:4px;border:1px solid #ddd;")
        self.detail_label.setMinimumHeight(130)
        right_lay.addWidget(self.detail_label)
        right_lay.addWidget(QLabel("任务日志："))
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(3000)
        right_lay.addWidget(self.log_view, 1)
        row_op = QHBoxLayout()
        self.start_one_btn = QPushButton("启动")
        self.start_one_btn.clicked.connect(lambda: self._operate_selected(self.scheduler.start_task))
        row_op.addWidget(self.start_one_btn)
        self.pause_one_btn = QPushButton("暂停")
        self.pause_one_btn.clicked.connect(lambda: self._operate_selected(self.scheduler.pause_task))
        row_op.addWidget(self.pause_one_btn)
        self.resume_one_btn = QPushButton("继续")
        self.resume_one_btn.clicked.connect(lambda: self._operate_selected(self.scheduler.resume_task))
        row_op.addWidget(self.resume_one_btn)
        self.stop_one_btn = QPushButton("停止")
        self.stop_one_btn.clicked.connect(lambda: self._operate_selected(self.scheduler.stop_task))
        row_op.addWidget(self.stop_one_btn)
        self.retry_one_btn = QPushButton("重试")
        self.retry_one_btn.clicked.connect(lambda: self._operate_selected(self.scheduler.retry_task))
        row_op.addWidget(self.retry_one_btn)
        self.edit_one_btn = QPushButton("编辑配置")
        self.edit_one_btn.clicked.connect(self._on_edit_selected)
        row_op.addWidget(self.edit_one_btn)
        self.export_one_btn = QPushButton("导出该任务")
        self.export_one_btn.clicked.connect(self._on_export_selected)
        row_op.addWidget(self.export_one_btn)
        right_lay.addLayout(row_op)
        split.addWidget(right)
        split.setSizes([760, 300])
        root.addWidget(split, 1)

        # ---- 底部汇总 ----
        self._global_note = QLabel()
        self._global_note.setStyleSheet("color:#1565c0;")
        root.addWidget(self._global_note)
        self.summary_label = QLabel()
        root.addWidget(self.summary_label)

    def _connect_signals(self):
        s = self.scheduler
        self.concurrent_spin.valueChanged.connect(s.set_max_concurrent)
        s.task_added.connect(self._on_task_added)
        s.task_changed.connect(self._on_task_changed)
        s.task_removed.connect(self._on_task_removed)
        s.task_log_line.connect(self._on_task_log_line)
        s.summary_changed.connect(self._refresh_summary)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)

    # ---------------- 表格 ----------------
    def _row_insert(self, tid: int) -> int:
        """按ID升序插入行，返回行号"""
        row = self.table.rowCount()
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 1)
            if item is not None and int(item.text()) > tid:
                row = r
                break
        self.table.insertRow(row)
        self._row_of_id[tid] = row
        # 选择列：复选框
        check = QTableWidgetItem()
        check.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
        check.setCheckState(Qt.CheckState.Unchecked)
        check.setData(Qt.ItemDataRole.UserRole, tid)
        check.setToolTip("勾选后可进行批量操作")
        self.table.setItem(row, self.col("select"), check)
        id_item = QTableWidgetItem(str(tid))
        id_item.setData(Qt.ItemDataRole.UserRole, tid)
        id_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.table.setItem(row, self.col("id"), id_item)
        return row

    @staticmethod
    def col(name: str) -> int:
        return {
            "select": 0, "id": 1, "name": 2, "status": 3, "priority": 4,
            "progress": 5, "pages": 6, "links": 7, "depth": 8, "media": 9,
            "retry": 10, "elapsed": 11, "note": 12,
        }[name]

    def _refresh_row(self, tid: int):
        meta = next((m for m in self.scheduler.task_metas() if m["id"] == tid), None)
        row = self._row_of_id.get(tid)
        if meta is None or row is None or row >= self.table.rowCount():
            return
        texts = {
            self.col("name"): meta["name"] + "\n" + meta["start_url"],
            self.col("status"): meta["status_text"],
            self.col("priority"): meta["priority_text"],
            self.col("progress"): f"{meta['progress']}%",
            self.col("pages"): str(meta["pages"]),
            self.col("links"): str(meta["link_count"]),
            self.col("depth"): f"{meta['current_depth']}/{meta['max_depth']}",
            self.col("media"): str(meta["media_count"]),
            self.col("retry"): f"{meta['retry_count']}/{meta['max_retries']}",
            self.col("elapsed"): fmt_duration(meta["duration"]),
            self.col("note"): meta["error"] or "",
        }
        for c, v in texts.items():
            item = self.table.item(row, c)
            if item is None:
                item = QTableWidgetItem()
                item.setData(Qt.ItemDataRole.UserRole, tid)
                self.table.setItem(row, c, item)
            item.setText(v)
            if c == self.col("status"):
                item.setForeground(_exists_color(STATUS_COLORS.get(meta["status"], "#808080")))
            if c in (self.col("id"), self.col("status"), self.col("priority"),
                     self.col("progress"), self.col("pages"), self.col("links"),
                     self.col("media"), self.col("retry"), self.col("elapsed")):
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        tip = (f"任务ID：{meta['id']}\n起始URL：{meta['start_url']}\n状态：{meta['status_text']}\n"
               f"依赖任务：{','.join(str(x) for x in meta['depends_on']) or '无'}\n"
               f"数据库：{meta['db_path']}\n下载目录：{meta['download_dir']}\n日志：{meta['log_path']}\n"
               f"创建时间：{meta['created_at']}\n开始：{meta['started_at'] or '-'}\n结束：{meta['finished_at'] or '-'}")
        name_item = self.table.item(row, self.col("name"))
        if name_item:
            name_item.setToolTip(tip)

    def _on_task_added(self, tid: int):
        row = self._row_insert(tid)
        self._row_of_id[tid] = row
        self._refresh_row(tid)

    def _on_task_changed(self, tid: int):
        self._refresh_row(tid)
        if tid == self._selected_id:
            self._show_detail(tid)

    def _on_task_removed(self, tid: int):
        row = self._row_of_id.pop(tid, None)
        if row is not None and row < self.table.rowCount():
            self.table.removeRow(row)
            # 重排行号映射
            new_map = {}
            for t, r in self._row_of_id.items():
                new_map[t] = r - 1 if r > row else r
            self._row_of_id = new_map
        if self._selected_id == tid:
            self._selected_id = None
            self._show_detail(None)

    def _on_selection_changed(self):
        rows = self.table.selectionModel().selectedRows()
        tid = None
        if rows:
            item = self.table.item(rows[0].row(), 1)
            if item is not None:
                tid = int(item.data(Qt.ItemDataRole.UserRole))
        self._selected_id = tid
        self._show_detail(tid)

    def _show_detail(self, tid: int | None):
        meta = next((m for m in self.scheduler.task_metas() if m["id"] == tid), None)
        if meta is None:
            self.detail_label.setText("选中任务详情：\n（点击表格行查看）")
            self.log_view.clear()
            return
        self.detail_label.setText(
            f"任务 #{meta['id']}  {meta['name']}\n"
            f"状态：{meta['status_text']}   进度：{meta['progress']}%\n"
            f"页面数：{meta['pages']}   链接数：{meta['link_count']}   媒体：{meta['media_count']}\n"
            f"深度：{meta['current_depth']}/{meta['max_depth']}   耗时：{fmt_duration(meta['duration'])}\n"
            f"失败原因：{meta['error'] or '无'}\n"
            f"下载目录：{meta['download_dir']}")
        rec = self.scheduler.task(tid)
        lines = (rec or {}).get("log_lines", [])
        if lines:
            self.log_view.setPlainText("\n".join(lines))
            self.log_view.verticalScrollBar().setValue(self.log_view.verticalScrollBar().maximum())

    def _on_task_log_line(self, tid: int, msg: str):
        if tid != self._selected_id:
            return
        self.log_view.appendPlainText(msg)
        sb = self.log_view.verticalScrollBar()
        if sb.value() >= sb.maximum() - 30:
            sb.setValue(sb.maximum())

    # ---------------- 选择辅助 ----------------
    def _set_all_checked(self, checked: bool):
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            if item is not None:
                item.setCheckState(state)

    def _checked_ids(self) -> list[int]:
        ids = []
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                tid = item.data(Qt.ItemDataRole.UserRole)
                if tid is not None:
                    ids.append(int(tid))
        return ids

    def _single_id(self) -> int | None:
        if self._selected_id is not None:
            return self._selected_id
        rows = self.table.selectionModel().selectedRows()
        if rows:
            item = self.table.item(rows[0].row(), 1)
            if item is not None:
                return int(item.data(Qt.ItemDataRole.UserRole))
        return None

    def _operate_selected(self, fn):
        tid = self._single_id()
        if tid is None:
            QMessageBox.information(self, "提示", "请先在表格中选择一个任务")
            return
        fn(tid)

    # ---------------- 操作事件 ----------------
    def _on_add(self):
        cfg = self._default_config()
        existing = self.scheduler.task_metas()
        if self.main_window is not None:
            url_filter = getattr(self.main_window, "url_filter", None)
        else:
            url_filter = None
        patterns = list(url_filter.block_patterns()) if url_filter is not None else []
        domains = list(url_filter.allow_domains()) if url_filter is not None else []
        dlg = TaskAddDialog(self, cfg, existing, patterns, domains)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            configs = dlg.result_configs()
            if not configs:
                return
            ids = self.scheduler.add_tasks(configs)
            added = len(ids)
            QMessageBox.information(self, "添加任务", f"已添加 {added} 个任务到队列\n"
                                                      f"（按最大并发数 {self.scheduler.get_max_concurrent()} 自动调度执行）")

    def _on_edit_selected(self):
        tid = self._single_id()
        if tid is None:
            QMessageBox.information(self, "提示", "请先选择一个任务")
            return
        rec = self.scheduler.task(tid)
        if rec is None:
            return
        existing = self.scheduler.task_metas()
        dlg = TaskAddDialog(self, self._default_config(), existing,
                            [], [], title=f"编辑任务 #{tid} 配置", edit_cfg=rec["config"])
        if dlg.exec() == QDialog.DialogCode.Accepted:
            configs = dlg.result_configs()
            if not configs:
                return
            ok = self.scheduler.update_config(tid, configs[0])
            if not ok:
                QMessageBox.warning(self, "无法编辑", "执行中/暂停的任务不能直接修改配置，请先停止后再编辑")
                return
            self._refresh_row(tid)
            self._show_detail(tid)

    def _on_start_selected(self):
        ids = self._checked_ids() or ([self._single_id()] if self._single_id() is not None else [])
        if not ids:
            QMessageBox.information(self, "提示", "请先勾选要操作的任务（或选择一行）")
            return
        for tid in ids:
            self.scheduler.start_task(tid)

    def _on_pause_selected(self):
        for tid in self._selected_by_state(TaskStatus.RUNNING):
            self.scheduler.pause_task(tid)

    def _on_resume_selected(self):
        for tid in self._selected_by_state(TaskStatus.PAUSED):
            self.scheduler.resume_task(tid)

    def _selected_by_state(self, *states) -> list[int]:
        checked = self._checked_ids()
        if not checked:
            tid = self._single_id()
            checked = [tid] if tid is not None else []
        out = []
        for tid in checked:
            meta = self.scheduler.task(tid)
            if meta and meta["status"] in states:
                out.append(tid)
        return out

    def _on_delete_selected(self):
        ids = self._checked_ids() or ([self._single_id()] if self._single_id() is not None else [])
        if not ids:
            QMessageBox.information(self, "提示", "请先勾选要删除的任务（或选择一行）")
            return
        reply = QMessageBox.question(
            self, "批量删除", f"确定删除选中的 {len(ids)} 个任务？\n"
                              f"（任务数据目录保留，如需彻底清理请手动删除 task_data 下对应目录）",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        for tid in ids:
            self.scheduler.remove_task(tid)

    def _on_export_selected(self):
        tid = self._single_id()
        if tid is None:
            QMessageBox.information(self, "提示", "请先选择一个任务")
            return
        rows = self.scheduler.merged_results([tid])
        if not rows:
            QMessageBox.information(self, "导出", "该任务暂无可导出的页面结果")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出该任务结果", os.path.join(os.getcwd(), f"task_{tid}_export.csv"),
            "CSV 文件 (*.csv);;JSON 文件 (*.json);;Markdown 文件 (*.md)")
        if not path:
            return
        try:
            n = export_tasks_to_file(rows, path)
            QMessageBox.information(self, "导出成功", f"已导出 {n} 条记录到：\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))

    def _on_export_all(self):
        rows = self.scheduler.merged_results()
        if not rows:
            QMessageBox.information(self, "导出", "暂无可汇总的任务结果（先运行任务）")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "汇总导出全部任务结果", os.path.join(os.getcwd(), "tasks_export.csv"),
            "CSV 文件 (*.csv);;JSON 文件 (*.json);;Markdown 文件 (*.md)")
        if not path:
            return
        try:
            n = export_tasks_to_file(rows, path)
            QMessageBox.information(self, "导出成功", f"已合并导出 {n} 条记录到：\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))

    def _on_compare(self):
        dlg = TaskCompareDialog(self, self.scheduler)
        dlg.exec()

    def _on_browse_download_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择下载根目录", self.download_dir_edit.text())
        if d:
            self.download_dir_edit.setText(d)
            self.scheduler.base_download_dir = d

    def _on_save_queue(self):
        self.scheduler.save_queue()
        QMessageBox.information(self, "保存队列",
                                f"已保存未完成任务到：\n{self.scheduler.queue_file}")

    def _on_load_queue(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择队列文件", self.scheduler.queue_file, "JSON (*.json)")
        if not path:
            return
        n = self.scheduler.load_queue(path)
        QMessageBox.information(self, "载入队列", f"已恢复 {n} 个任务（点击\"开始\"执行）")

    # ---------------- 汇总与时钟 ----------------
    def _main_media_config(self) -> dict:
        """主窗口「爬取配置」页的媒体设置快照（可能为空字典）。"""
        collector = getattr(self.main_window, "_collect_media_config", None)
        if collector is None:
            return {}
        try:
            return collector().to_dict()
        except Exception:
            return {}

    def _default_config(self) -> TaskConfig:
        """全局默认配置：爬取参数 + 主窗口「爬取配置」页的媒体设置快照。

        媒体设置的 ``enabled`` 字段在此不作为"是否媒体任务"的依据，
        真正的任务类型由「添加任务」对话框的任务类型选择决定。
        """
        return TaskConfig(
            media=self._main_media_config(),
            max_depth=self.depth_spin.value(),
            request_delay=self.delay_spin.value(),
            max_chars_per_page=self.chars_spin.value(),
            crawl_external=self.external_check.isChecked(),
            robots_check=self.robots_check.isChecked(),
            max_retries=self.retries_spin.value(),
        )

    def _refresh_summary(self):
        s = self.scheduler.summary()
        self.summary_label.setText(
            f"全局统计：任务总数 {s['total']} ｜ 待执行 {s[TaskStatus.PENDING]} ｜ "
            f"执行中 {s[TaskStatus.RUNNING]} ｜ 暂停 {s[TaskStatus.PAUSED]} ｜ "
            f"已完成 {s[TaskStatus.COMPLETED]} ｜ 失败 {s[TaskStatus.FAILED]} ｜ "
            f"总爬取页面 {s['pages']} ｜ 总链接数 {s['links']} ｜ 总耗时 {fmt_duration(s['elapsed'])}")

    def _tick_clock(self):
        """每秒刷新运行中任务的耗时与进度"""
        for tid in self._row_of_id:
            meta = next((m for m in self.scheduler.task_metas() if m["id"] == tid), None)
            if meta is None:
                continue
            if meta["status"] in (TaskStatus.RUNNING, TaskStatus.PAUSED):
                row = self._row_of_id.get(tid)
                if row is not None and row < self.table.rowCount():
                    item = self.table.item(row, self.col("elapsed"))
                    if item is None:
                        item = QTableWidgetItem()
                        self.table.setItem(row, self.col("elapsed"), item)
                    item.setText(fmt_duration(meta["duration"]))


__all__ = ["TaskManagerTab"]
