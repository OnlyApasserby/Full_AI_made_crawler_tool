"""主窗口：单任务递归爬取 / 媒体抓取 + 反爬设置 + 下载管理（+ 任务管理标签页）。

业务逻辑（爬取/下载/数据库）均已下沉到 core / manager / utils，
本模块只负责界面与用户交互、线程的信号连接与结果展示。

「爬取配置」页提供两种流程：
- **递归爬取**（原有）：以页面文字与链接为目标，BFS 深度遍历
- **媒体抓取**（新增）：以图像/视频为目标，静态与浏览器渲染双模式，
  支持懒加载滚动、图集翻页、XHR 网络监听、去重与质量过滤
"""

from __future__ import annotations

import os
import re
import shutil
import time

from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QMainWindow, QMessageBox,
    QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QTabWidget,
    QTextEdit, QToolBar, QVBoxLayout, QWidget,
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QShortcut, QKeySequence

from config import (
    APP_NAME, CRAWLER_USER_AGENT, DEFAULT_MEDIA_FILENAME_TEMPLATE,
    DEFAULT_MEDIA_MAX_PAGES, DEFAULT_MEDIA_MAX_SCROLL, DEFAULT_MEDIA_MIN_SIZE,
    DEFAULT_MEDIA_PAGE_TIMEOUT, DEFAULT_MEDIA_RENDER_MODE, ROOT_DIR,
    default_db_path,
)
from core.crawler import RecursiveCrawlerThread
from core.downloader import ContentDownloader
from core.extractor import plugin_classes
from core.fetcher import chrome_setup, playwright_status, prepare_browser
from core.filter import URLFilter
from core.media.models import (
    RENDER_AUTO, RENDER_DYNAMIC, RENDER_STATIC, MediaConfig,
)
from core.media_crawler import MediaCrawlThread
from core.robots import RobotsCheckThread, RobotsParser
from utils.helpers import format_size
from utils.validators import validate_url
from utils.user_agents import UserAgentPool
from manager.db_manager import (
    abandon_unfinished_tasks, clear_crawled_records, ensure_schema,
    get_unfinished_tasks, save_media_items,
)
from ui.export import (
    export_default_name, export_file_filter, write_export, write_media_export,
)
from ui.dialogs import ExportDialog, URLFilterDialog
from ui.rule_dialog import SiteRuleDialog
from ui.security_dialog import LinkGuardDialog

#: 媒体结果表格最多展示的行数（超出的仍会参与下载，仅界面截断以保证流畅）
MEDIA_TABLE_LIMIT = 500
#: 媒体结果表格列定义：(标题, 取值函数)
MEDIA_TABLE_COLUMNS = (
    ("类型", lambda item: item.kind),
    ("合集", lambda item: item.album),
    ("序号", lambda item: str(item.index or "")),
    ("分辨率", lambda item: item.resolution),
    ("来源", lambda item: item.origin),
    ("资源URL", lambda item: item.url),
)


class CrawlerMainWindow(QMainWindow):
    """爬虫工具主窗口。"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setGeometry(100, 100, 1080, 820)
        # 主数据库文件位于包内 resources 目录（结构上属于默认资源）
        self.db_path = default_db_path()
        # 提前建表：媒体抓取流程不经过爬虫线程，需要自己保证表结构就绪
        try:
            ensure_schema(self.db_path)
        except Exception:
            pass
        self.ua_pool = UserAgentPool()  # 全局UA池，爬取与下载共用
        self.url_filter = URLFilter()  # 全局URL过滤规则，爬取线程共享同一实例
        self._syncing_block_input = False  # 防递归标志：输入框同步时跳过textChanged
        self.media_urls = []  # 本次爬取发现的媒体资源（URL 列表，供下载管理）
        self.media_items = []  # 本次媒体抓取的 MediaItem 列表（含元信息）
        self.media_thread = None  # 媒体抓取线程（与递归爬取线程互斥使用）
        self.downloader = None
        self.init_ui()
        # 过滤统计定时刷新（过滤发生在爬取线程，需定时同步到GUI显示）
        self.filter_stats_timer = QTimer(self)
        self.filter_stats_timer.timeout.connect(self._update_filter_stats)
        self.filter_stats_timer.start(1000)
        self.check_unfinished_task()  # 启动时检测是否有未完成的任务

    def init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setSpacing(10)
        main_layout.setContentsMargins(20, 20, 20, 20)

        # 0. 主工具栏：屏蔽URL正则输入框（实时生效，无需重启任务）
        toolbar = QToolBar("主工具栏")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        toolbar.addWidget(QLabel("屏蔽URL正则："))
        self.block_regex_input = QLineEdit()
        self.block_regex_input.setPlaceholderText("如 ^https://www\\.example\\.com/private，多个用分号分隔，留空表示不屏蔽")
        self.block_regex_input.setToolTip("匹配任一正则的URL不会被爬取（多个正则用英文分号;分隔）。修改后实时生效，无需重新启动任务")
        self.block_regex_input.setMinimumWidth(340)
        self.block_regex_input.textChanged.connect(self._on_block_regex_changed)
        toolbar.addWidget(self.block_regex_input)
        toolbar.addSeparator()
        self.filter_stats_label = QLabel("已过滤URL：0")
        self.filter_stats_label.setToolTip("被URL过滤规则（屏蔽正则/白名单）拦截的URL累计数量")
        toolbar.addWidget(self.filter_stats_label)
        self.filter_manage_btn = QPushButton("过滤管理")
        self.filter_manage_btn.setToolTip("管理屏蔽正则、域名白名单，查看被过滤URL样本，导入/导出规则")
        self.filter_manage_btn.clicked.connect(self.on_open_filter_dialog)
        toolbar.addWidget(self.filter_manage_btn)
        self.security_scan_btn = QPushButton("安全检测")
        self.security_scan_btn.setToolTip("识别广告/钓鱼链接（追踪参数、广告联盟域名、仿冒域名、短链、异常TLD等），"
                                          "并自动生成覆盖同类模式的屏蔽正则")
        self.security_scan_btn.clicked.connect(self.on_open_security_dialog)
        toolbar.addWidget(self.security_scan_btn)
        self.rule_manage_btn = QPushButton("站点规则")
        self.rule_manage_btn.setToolTip(
            "管理 resources/rules/*.json 站点规则：用选择器/正则适配特定站点，\n"
            "可编辑、校验、导入导出，并直接抓取页面测试命中效果（保存后立即生效）")
        self.rule_manage_btn.clicked.connect(self.on_open_rule_dialog)
        toolbar.addWidget(self.rule_manage_btn)

        # 1. 标签页：爬取配置 / 反爬设置 / 下载管理
        self.tabs = QTabWidget()
        self.crawl_tab = QWidget()
        self.anti_tab = QWidget()
        self.download_tab = QWidget()
        self.tabs.addTab(self.crawl_tab, "爬取配置")
        self.tabs.addTab(self.anti_tab, "反爬设置")
        self.tabs.addTab(self.download_tab, "下载管理")
        main_layout.addWidget(self.tabs, 1)

        self._init_crawl_tab()
        self._init_anti_tab()
        self._init_download_tab()
        self._init_task_manager_tab()  # 任务管理（多目标并发爬取）

        self.pending_crawl_url = ""
        self.robots_content = ""
        self.last_crawl_links = []
        self.last_page_texts = {}

        # 暂停/继续快捷键（不改变UI布局）
        QShortcut(QKeySequence("Ctrl+P"), self,
                  activated=lambda: self._toggle_pause())
        QShortcut(QKeySequence("Ctrl+R"), self,
                  activated=lambda: self._toggle_pause())

    # ---------- 标签页1：爬取配置 ----------
    def _init_crawl_tab(self):
        layout = QVBoxLayout(self.crawl_tab)
        layout.setSpacing(8)

        # 网址输入
        top_layout = QHBoxLayout()
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("请输入起始网址：https://www.example.com")
        top_layout.addWidget(QLabel("起始网址："))
        top_layout.addWidget(self.url_input)
        layout.addLayout(top_layout)

        # 递归参数配置
        param_layout = QHBoxLayout()
        self.depth_spin = QSpinBox()
        self.depth_spin.setRange(0, 5)
        self.depth_spin.setValue(1)
        self.depth_spin.setToolTip("0表示只爬取起始页面，1表示爬取起始页+起始页内所有链接，以此类推")
        self.delay_spin = QDoubleSpinBox()
        self.delay_spin.setRange(0.0, 10.0)
        self.delay_spin.setSingleStep(0.1)
        self.delay_spin.setValue(0.5)
        self.delay_spin.setSuffix(" 秒")
        self.delay_spin.setToolTip("每次HTTP请求之间的基础等待间隔（实际间隔会叠加随机抖动），用于控制请求频率，避免对目标站点造成压力")
        self.crawl_external_check = QCheckBox("允许爬取站外链接")
        param_layout.addWidget(QLabel("递归爬取深度："))
        param_layout.addWidget(self.depth_spin)
        param_layout.addWidget(QLabel("请求间隔："))
        param_layout.addWidget(self.delay_spin)
        param_layout.addWidget(self.crawl_external_check)
        param_layout.addStretch()
        self.start_check_btn = QPushButton("校验robots.txt")
        self.start_check_btn.clicked.connect(self.on_check_robots)
        param_layout.addWidget(self.start_check_btn)
        layout.addLayout(param_layout)

        # 单页最大字符数 + 定向链接过滤
        filter_layout = QHBoxLayout()
        self.max_chars_spin = QSpinBox()
        self.max_chars_spin.setRange(0, 10000000)
        self.max_chars_spin.setSingleStep(10000)
        self.max_chars_spin.setValue(0)
        self.max_chars_spin.setSuffix(" 字符")
        self.max_chars_spin.setToolTip("单个页面最多下载的字符数（0表示不限制），超出部分将被截断，避免下载超大页面")
        self.link_filter_input = QLineEdit()
        self.link_filter_input.setPlaceholderText("如 /product 或 keyword，留空表示不限制")
        self.link_filter_input.setToolTip("定向递归爬取：仅沿URL中包含该关键词的链接继续深入爬取，其余链接只记录不爬取")
        self.max_pages_spin = QSpinBox()
        self.max_pages_spin.setRange(0, 100000)
        self.max_pages_spin.setValue(0)
        self.max_pages_spin.setSuffix(" 页")
        self.max_pages_spin.setToolTip("达到该爬取页数后自动停止（0表示不限制）。手动停止可随时点击「停止爬取」按钮")
        filter_layout.addWidget(QLabel("单页最大字符数："))
        filter_layout.addWidget(self.max_chars_spin)
        filter_layout.addWidget(QLabel("最大爬取页数："))
        filter_layout.addWidget(self.max_pages_spin)
        filter_layout.addWidget(QLabel("定向链接过滤："))
        filter_layout.addWidget(self.link_filter_input)
        filter_layout.addStretch()
        layout.addLayout(filter_layout)

        # 媒体抓取（图像/视频）配置：以媒体资源为目标的增强抓取流程
        layout.addWidget(self._build_media_group())
        # 媒体抓取结果表格（类型/合集/分辨率/来源），爬取过程中实时刷新
        self.media_result_table = QTableWidget(0, len(MEDIA_TABLE_COLUMNS))
        self.media_result_table.setHorizontalHeaderLabels(
            [title for title, _getter in MEDIA_TABLE_COLUMNS])
        self.media_result_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers)
        self.media_result_table.setMaximumHeight(150)
        self.media_result_table.verticalHeader().setVisible(False)
        self.media_result_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.media_result_table)
        self._refresh_browser_status()  # 浏览器内核状态（仅查找，不启动）

        # robots.txt展示区
        layout.addWidget(QLabel("robots.txt 规则预览："))
        self.robots_display = QTextEdit()
        self.robots_display.setReadOnly(True)
        self.robots_display.setMaximumHeight(120)
        layout.addWidget(self.robots_display)

        # 操作按钮区
        btn_layout = QHBoxLayout()
        self.confirm_crawl_btn = QPushButton("确认启动递归爬取")
        self.confirm_crawl_btn.setEnabled(False)
        self.confirm_crawl_btn.clicked.connect(self.on_start_crawl)
        self.stop_crawl_btn = QPushButton("停止爬取")
        self.stop_crawl_btn.setEnabled(False)
        self.stop_crawl_btn.setToolTip("手动终止当前爬取任务：中断进行中的请求、清理资源，并保存已爬数据与进度状态")
        self.stop_crawl_btn.clicked.connect(self.on_stop_crawl)
        self.export_csv_btn = QPushButton("导出CSV")
        self.export_csv_btn.setEnabled(False)
        self.export_csv_btn.clicked.connect(self.on_export_csv)
        self.export_txt_btn = QPushButton("导出TXT")
        self.export_txt_btn.setEnabled(False)
        self.export_txt_btn.clicked.connect(self.on_export_txt)
        self.export_media_btn = QPushButton("导出媒体清单")
        self.export_media_btn.setEnabled(False)
        self.export_media_btn.setToolTip(
            "把本轮媒体抓取结果（含类型/合集/序号/分辨率/来源页）导出为 CSV / JSON / Markdown")
        self.export_media_btn.clicked.connect(self.on_export_media)
        self.clear_btn = QPushButton("清空全部内容")
        self.clear_btn.clicked.connect(self.on_clear_all)
        self.clear_records_btn = QPushButton("清空爬取记录")
        self.clear_records_btn.setToolTip("删除数据库中的已爬URL记录，之后重新爬取同一站点不会被历史记录跳过")
        self.clear_records_btn.clicked.connect(self.on_clear_crawl_records)
        btn_layout.addStretch()
        btn_layout.addWidget(self.confirm_crawl_btn)
        btn_layout.addWidget(self.stop_crawl_btn)
        btn_layout.addWidget(self.export_csv_btn)
        btn_layout.addWidget(self.export_txt_btn)
        btn_layout.addWidget(self.export_media_btn)
        btn_layout.addWidget(self.clear_btn)
        btn_layout.addWidget(self.clear_records_btn)
        layout.addLayout(btn_layout)

        # 统计信息
        self.crawl_stats_label = QLabel("已爬页面：0 | 当前层：-/- | 提取链接：0 | 媒体资源：0")
        layout.addWidget(self.crawl_stats_label)

        # 爬取日志与结果展示区
        self.log_display = QTextEdit()
        self.log_display.setReadOnly(True)
        layout.addWidget(QLabel("爬取进度日志："))
        layout.addWidget(self.log_display, 1)

        self.link_display = QTextEdit()
        self.link_display.setReadOnly(True)
        layout.addWidget(QLabel("页面提取到的所有链接汇总："))
        layout.addWidget(self.link_display, 1)

        # 站点适配器插件提示（core/extractor/sites 下放入 .py 即自动注册）
        plugins = plugin_classes()
        if plugins:
            self.log_display.append(
                f"已加载 {len(plugins)} 个站点适配器插件："
                + "、".join(getattr(cls, "name", cls.__name__) for cls in plugins))

    # ---------- 标签页1：媒体抓取配置区 ----------
    def _build_media_group(self) -> QGroupBox:
        """构建「媒体抓取（图像/视频）」配置分组。

        与原有递归爬取共用起始网址、请求间隔、反爬与过滤规则，
        这里只补充媒体抓取专有的选项（目标类型 / 渲染模式 / 翻页 / 质量过滤）。
        """
        group = QGroupBox("媒体抓取（图像 / 视频）")
        box = QVBoxLayout(group)
        box.setSpacing(6)

        # 第一行：启用开关 + 目标类型 + 抓取模式 + 滚动加载
        row1 = QHBoxLayout()
        self.media_enable_check = QCheckBox("启用媒体抓取增强流程")
        self.media_enable_check.setToolTip(
            "勾选后「确认启动」走媒体抓取流程：静态请求 + 浏览器渲染双模式，\n"
            "自动识别懒加载、图集翻页与 XHR 接口中的图片/视频，并做去重与质量过滤")
        self.media_enable_check.toggled.connect(self._on_media_mode_toggled)
        row1.addWidget(self.media_enable_check)
        row1.addWidget(QLabel("目标类型："))
        self.media_image_check = QCheckBox("图片")
        self.media_image_check.setChecked(True)
        self.media_video_check = QCheckBox("视频")
        self.media_video_check.setChecked(True)
        self.media_audio_check = QCheckBox("音频")
        for check in (self.media_image_check, self.media_video_check,
                      self.media_audio_check):
            row1.addWidget(check)
        row1.addWidget(QLabel("抓取模式："))
        self.media_mode_combo = QComboBox()
        self.media_mode_combo.addItem("自动（静态优先，必要时渲染）", RENDER_AUTO)
        self.media_mode_combo.addItem("仅静态请求（最快）", RENDER_STATIC)
        self.media_mode_combo.addItem("浏览器渲染（最全）", RENDER_DYNAMIC)
        self.media_mode_combo.setToolTip(
            "自动：先发静态请求，若页面含懒加载/JS 特征且产出过少，再升级为浏览器渲染\n"
            "仅静态：速度最快，但拿不到 JS 渲染出来的资源\n"
            "浏览器渲染：最完整（含 XHR 监听到的真实地址），但更慢、更占内存")
        index = self.media_mode_combo.findData(DEFAULT_MEDIA_RENDER_MODE)
        if index >= 0:
            self.media_mode_combo.setCurrentIndex(index)
        row1.addWidget(self.media_mode_combo)
        self.media_scroll_check = QCheckBox("滚动触发懒加载")
        self.media_scroll_check.setChecked(True)
        self.media_scroll_check.setToolTip("渲染模式下自动滚动到底，触发瀑布流/懒加载")
        row1.addWidget(self.media_scroll_check)
        self.media_scroll_spin = QSpinBox()
        self.media_scroll_spin.setRange(0, 50)
        self.media_scroll_spin.setValue(DEFAULT_MEDIA_MAX_SCROLL)
        self.media_scroll_spin.setSuffix(" 次")
        row1.addWidget(self.media_scroll_spin)
        row1.addStretch()
        box.addLayout(row1)

        # 第二行：翻页 / 详情页 / 原图猜解
        row2 = QHBoxLayout()
        self.media_pager_check = QCheckBox("自动翻页")
        self.media_pager_check.setChecked(True)
        self.media_pager_check.setToolTip(
            "自动识别「下一页」链接与 ?page=N / /list_2.html 等页码规律并逐页抓取")
        row2.addWidget(self.media_pager_check)
        self.media_max_pages_spin = QSpinBox()
        self.media_max_pages_spin.setRange(0, 200)
        self.media_max_pages_spin.setValue(DEFAULT_MEDIA_MAX_PAGES)
        self.media_max_pages_spin.setSuffix(" 页")
        self.media_max_pages_spin.setToolTip("媒体抓取的页面总数上限（0=不限，内部按 200 兜底）")
        row2.addWidget(QLabel("页数上限："))
        row2.addWidget(self.media_max_pages_spin)
        self.media_detail_check = QCheckBox("跟进详情页")
        self.media_detail_check.setToolTip(
            "列表页 → 详情页 两段式抓取：从列表页进入每个详情页再提取媒体\n"
            "（单页最多跟进 20 个详情链接）")
        row2.addWidget(self.media_detail_check)
        self.media_detail_pattern_input = QLineEdit()
        self.media_detail_pattern_input.setPlaceholderText("详情页链接关键词，如 /photo/，留空不限制")
        self.media_detail_pattern_input.setMinimumWidth(200)
        row2.addWidget(self.media_detail_pattern_input)
        self.media_upgrade_check = QCheckBox("缩略图猜解原图")
        self.media_upgrade_check.setChecked(True)
        self.media_upgrade_check.setToolTip(
            "命中 /thumb/→/original/ 等命名规律时，额外尝试原图地址（可能有少量 404）")
        row2.addWidget(self.media_upgrade_check)
        row2.addStretch()
        box.addLayout(row2)

        # 第三行：质量过滤 + 去重 + 浏览器状态
        row3 = QHBoxLayout()
        self.media_min_width_spin = QSpinBox()
        self.media_min_width_spin.setRange(0, 10000)
        self.media_min_width_spin.setToolTip("小于该宽度的图片会被过滤（0=不过滤）")
        self.media_min_height_spin = QSpinBox()
        self.media_min_height_spin.setRange(0, 10000)
        self.media_min_height_spin.setToolTip("小于该高度的图片会被过滤（0=不过滤）")
        self.media_min_size_spin = QSpinBox()
        self.media_min_size_spin.setRange(0, 1000000)
        self.media_min_size_spin.setValue(DEFAULT_MEDIA_MIN_SIZE)
        self.media_min_size_spin.setSuffix(" KB")
        self.media_min_size_spin.setToolTip("小于该体积的资源会被过滤（0=不过滤），可有效排除图标与占位图")
        self.media_max_count_spin = QSpinBox()
        self.media_max_count_spin.setRange(0, 1000000)
        self.media_max_count_spin.setToolTip("最多保留的媒体条数（0=不限）")
        row3.addWidget(QLabel("最小宽高："))
        row3.addWidget(self.media_min_width_spin)
        row3.addWidget(QLabel("×"))
        row3.addWidget(self.media_min_height_spin)
        row3.addWidget(QLabel("最小体积："))
        row3.addWidget(self.media_min_size_spin)
        row3.addWidget(QLabel("最大条数："))
        row3.addWidget(self.media_max_count_spin)
        self.media_dedup_check = QCheckBox("内容去重")
        self.media_dedup_check.setChecked(True)
        self.media_dedup_check.setToolTip("按 URL 归一化（忽略 utm 等追踪参数）与文件内容去重")
        self.media_perceptual_check = QCheckBox("感知去重")
        self.media_perceptual_check.setChecked(True)
        self.media_perceptual_check.setToolTip(
            "按图片指纹识别「同图不同尺寸/压缩」的近似重复（需 Pillow）")
        row3.addWidget(self.media_dedup_check)
        row3.addWidget(self.media_perceptual_check)
        self.media_stream_check = QCheckBox("合并流媒体")
        self.media_stream_check.setToolTip(
            "对 m3u8（HLS）与 mpd（DASH）下载全部分片并合并为单个视频文件；\n"
            "安装 ffmpeg 后可自动转封装为 MP4 并合流音轨，未安装则保留原始容器（.ts）")
        row3.addWidget(self.media_stream_check)
        row3.addStretch()
        self.media_browser_label = QLabel("浏览器：检测中…")
        row3.addWidget(self.media_browser_label)
        self.media_browser_btn = QPushButton("准备浏览器")
        self.media_browser_btn.setToolTip(
            "查找/解压本地浏览器内核（项目根目录的 chrome-win64.zip），\n"
            "或联网执行 playwright install chromium")
        self.media_browser_btn.clicked.connect(self.on_prepare_browser)
        row3.addWidget(self.media_browser_btn)
        box.addLayout(row3)
        return group

    def _on_media_mode_toggled(self, checked: bool) -> None:
        """切换媒体抓取开关时同步「确认启动」按钮文案，避免流程歧义。"""
        button = getattr(self, "confirm_crawl_btn", None)
        if button is not None:
            button.setText("确认启动媒体抓取" if checked else "确认启动递归爬取")

    def _collect_media_config(self) -> MediaConfig:
        """从界面控件收集媒体抓取配置。"""
        kinds = []
        if self.media_image_check.isChecked():
            kinds.append("image")
        if self.media_video_check.isChecked():
            kinds.append("video")
        if self.media_audio_check.isChecked():
            kinds.append("audio")
        return MediaConfig(
            enabled=True,
            kinds=kinds or ["image"],
            render_mode=self.media_mode_combo.currentData() or RENDER_AUTO,
            scroll=self.media_scroll_check.isChecked(),
            max_scroll=self.media_scroll_spin.value(),
            wait_timeout=DEFAULT_MEDIA_PAGE_TIMEOUT,
            follow_pagination=self.media_pager_check.isChecked(),
            max_pages=self.media_max_pages_spin.value(),
            max_depth=1 if self.media_detail_check.isChecked() else 0,
            detail_link_pattern=self.media_detail_pattern_input.text().strip(),
            min_width=self.media_min_width_spin.value(),
            min_height=self.media_min_height_spin.value(),
            min_size=self.media_min_size_spin.value() * 1024,
            max_count=self.media_max_count_spin.value(),
            dedup=self.media_dedup_check.isChecked(),
            dedup_perceptual=self.media_perceptual_check.isChecked(),
            upgrade_urls=self.media_upgrade_check.isChecked(),
            merge_stream=self.media_stream_check.isChecked(),
            stream_remux="auto",   # 有 ffmpeg 自动转 MP4，否则保留原始容器
            filename_template=DEFAULT_MEDIA_FILENAME_TEMPLATE,
        )

    # ---------- 浏览器内核状态 ----------
    def _refresh_browser_status(self) -> None:
        """刷新浏览器内核状态标签（只做查找，不启动浏览器）。"""
        path = chrome_setup.find_executable()
        if path:
            self.media_browser_label.setText(f"浏览器：就绪（{os.path.basename(path)}）")
            self.media_browser_label.setStyleSheet("color:#1e7e34;")
        else:
            self.media_browser_label.setText("浏览器：未就绪")
            self.media_browser_label.setStyleSheet("color:#c62828;")

    def on_prepare_browser(self) -> None:
        """准备浏览器内核：先查找本地内核，再尝试解压离线包。"""
        path, reason = prepare_browser(progress=self.log_display.append)
        self._refresh_browser_status()
        if not path:
            QMessageBox.warning(
                self, "浏览器未就绪",
                f"{reason}\n\n两种准备方式（任选其一）：\n"
                "1) 把 Chrome for Testing 压缩包放到项目根目录（如 chrome-win64.zip），"
                "程序会自动解压使用\n"
                "2) 联网下载官方内核：playwright install chromium")
            return
        self.log_display.append(f"✅ 浏览器内核：{path}")
        available, why = playwright_status(force=True, progress=self.log_display.append)
        if available:
            QMessageBox.information(self, "浏览器就绪",
                                    f"浏览器内核已就绪，动态渲染与 XHR 监听可用：\n{path}")
        else:
            QMessageBox.warning(self, "浏览器不可用", why)

    # ---------- 媒体结果表格 ----------
    def _clear_media_table(self) -> None:
        """清空媒体结果表格。"""
        self.media_result_table.setRowCount(0)

    def _fill_media_table(self, items) -> None:
        """按 MediaItem 元信息刷新结果表格（超量仅展示前 N 行）。"""
        rows = list(items)[:MEDIA_TABLE_LIMIT]
        table = self.media_result_table
        table.setRowCount(len(rows))
        for row, item in enumerate(rows):
            for col, (_title, getter) in enumerate(MEDIA_TABLE_COLUMNS):
                try:
                    value = str(getter(item) or "")
                except Exception:
                    value = ""
                table.setItem(row, col, QTableWidgetItem(value[:150]))
        table.resizeColumnsToContents()

    # ---------- 标签页2：反爬设置 ----------
    def _init_anti_tab(self):
        layout = QVBoxLayout(self.anti_tab)
        layout.setSpacing(12)

        # UA池管理
        ua_group = QGroupBox("User-Agent 池（爬取与下载时随机抽取）")
        ua_layout = QVBoxLayout(ua_group)
        self.ua_list = QListWidget()
        self.ua_list.setMaximumHeight(180)
        self.ua_list.addItems(self.ua_pool.all())
        ua_layout.addWidget(self.ua_list)
        ua_btn_row = QHBoxLayout()
        self.ua_add_input = QLineEdit()
        self.ua_add_input.setPlaceholderText("输入新的User-Agent后点击添加")
        self.ua_add_btn = QPushButton("添加UA")
        self.ua_add_btn.clicked.connect(self.on_add_ua)
        self.ua_del_btn = QPushButton("删除选中")
        self.ua_del_btn.clicked.connect(self.on_del_ua)
        self.ua_reset_btn = QPushButton("恢复默认")
        self.ua_reset_btn.clicked.connect(self.on_reset_ua)
        ua_btn_row.addWidget(self.ua_add_input, 1)
        ua_btn_row.addWidget(self.ua_add_btn)
        ua_btn_row.addWidget(self.ua_del_btn)
        ua_btn_row.addWidget(self.ua_reset_btn)
        ua_layout.addLayout(ua_btn_row)
        layout.addWidget(ua_group)

        # 请求策略：间隔随机抖动
        delay_group = QGroupBox("请求策略")
        delay_layout = QHBoxLayout(delay_group)
        self.jitter_spin = QDoubleSpinBox()
        self.jitter_spin.setRange(0.0, 2.0)
        self.jitter_spin.setSingleStep(0.1)
        self.jitter_spin.setValue(0.3)
        self.jitter_spin.setSuffix(" 倍")
        self.jitter_spin.setToolTip("请求间隔的随机抖动幅度：实际间隔 = 设定间隔 × (1 + 随机抖动值)，降低请求规律性")
        delay_layout.addWidget(QLabel("请求间隔随机抖动幅度："))
        delay_layout.addWidget(self.jitter_spin)
        delay_layout.addWidget(QLabel("（0表示无抖动，值越大间隔波动越明显）"))
        delay_layout.addStretch()
        layout.addWidget(delay_group)

        # 代理设置
        proxy_group = QGroupBox("代理设置")
        proxy_layout = QVBoxLayout(proxy_group)
        self.proxy_check = QCheckBox("启用HTTP代理")
        self.proxy_check.setToolTip("启用后，爬取页面与下载媒体文件都将通过该代理进行")
        proxy_layout.addWidget(self.proxy_check)
        proxy_row = QHBoxLayout()
        proxy_row.addWidget(QLabel("代理地址："))
        self.proxy_input = QLineEdit()
        self.proxy_input.setPlaceholderText("如 http://127.0.0.1:7890 或 http://user:pass@host:port")
        self.proxy_input.setEnabled(False)
        self.proxy_check.toggled.connect(self.proxy_input.setEnabled)
        proxy_row.addWidget(self.proxy_input, 1)
        proxy_layout.addLayout(proxy_row)
        layout.addWidget(proxy_group)

        layout.addStretch()

    # ---------- 标签页3：下载管理 ----------
    def _init_download_tab(self):
        layout = QVBoxLayout(self.download_tab)
        layout.setSpacing(8)

        # 下载设置：目录 + 并发/重试/限速
        setting_group = QGroupBox("下载设置")
        setting_layout = QVBoxLayout(setting_group)
        dir_row = QHBoxLayout()
        dir_row.addWidget(QLabel("下载目录："))
        self.download_dir_input = QLineEdit("downloads")
        self.download_dir_input.setPlaceholderText("媒体文件保存目录，按 资源类型/域名 自动分子文件夹")
        self.download_dir_btn = QPushButton("浏览...")
        self.download_dir_btn.clicked.connect(self.on_choose_download_dir)
        dir_row.addWidget(self.download_dir_input, 1)
        dir_row.addWidget(self.download_dir_btn)
        setting_layout.addLayout(dir_row)

        ctrl_row = QHBoxLayout()
        self.download_workers_spin = QSpinBox()
        self.download_workers_spin.setRange(1, 16)
        self.download_workers_spin.setValue(4)
        self.download_workers_spin.setToolTip("并发下载数：同时进行下载的工作线程数")
        self.download_retries_spin = QSpinBox()
        self.download_retries_spin.setRange(0, 10)
        self.download_retries_spin.setValue(3)
        self.download_retries_spin.setToolTip("下载失败后的自动重试次数（指数退避）")
        self.speed_limit_spin = QSpinBox()
        self.speed_limit_spin.setRange(0, 100000)
        self.speed_limit_spin.setValue(0)
        self.speed_limit_spin.setSuffix(" KB/s")
        self.speed_limit_spin.setToolTip("单文件下载速度上限（0表示不限速）")
        ctrl_row.addWidget(QLabel("并发下载数："))
        ctrl_row.addWidget(self.download_workers_spin)
        ctrl_row.addWidget(QLabel("失败重试次数："))
        ctrl_row.addWidget(self.download_retries_spin)
        ctrl_row.addWidget(QLabel("下载限速："))
        ctrl_row.addWidget(self.speed_limit_spin)
        ctrl_row.addStretch()
        setting_layout.addLayout(ctrl_row)

        # 存储优化选项
        opt_row = QHBoxLayout()
        self.md5_check = QCheckBox("MD5校验")
        self.md5_check.setChecked(True)
        self.md5_check.setToolTip("服务器提供校验和时验证MD5，不匹配则重新下载")
        self.dedup_check = QCheckBox("图片内容去重")
        self.dedup_check.setChecked(True)
        self.dedup_check.setToolTip("相同内容的图片只保留一份（按文件MD5去重）")
        opt_row.addWidget(self.md5_check)
        opt_row.addWidget(self.dedup_check)
        opt_row.addWidget(QLabel("图片压缩质量："))
        self.compress_quality_spin = QSpinBox()
        self.compress_quality_spin.setRange(0, 100)
        self.compress_quality_spin.setValue(0)
        self.compress_quality_spin.setToolTip("对图片重新压缩（0表示不压缩，值越低体积越小但画质越差）")
        opt_row.addWidget(self.compress_quality_spin)
        opt_row.addStretch()
        self.download_btn = QPushButton("开始下载")
        self.download_btn.setEnabled(False)
        self.download_btn.clicked.connect(self.on_start_download)
        self.stop_download_btn = QPushButton("停止下载")
        self.stop_download_btn.setEnabled(False)
        self.stop_download_btn.clicked.connect(self.on_stop_download)
        opt_row.addWidget(self.download_btn)
        opt_row.addWidget(self.stop_download_btn)
        setting_layout.addLayout(opt_row)

        # 存储空间监控（定时刷新）
        self.disk_status_label = QLabel("存储空间：--")
        setting_layout.addWidget(self.disk_status_label)
        self.disk_timer = QTimer(self)
        self.disk_timer.timeout.connect(self._update_disk_status)
        self.disk_timer.start(5000)
        layout.addWidget(setting_group)

        # 媒体资源列表（下载队列输入）
        layout.addWidget(QLabel("媒体资源列表（爬取结果自动收集，也可手动编辑，每行一个URL）："))
        self.media_list = QTextEdit()
        self.media_list.setPlaceholderText("爬取完成后此处会自动列出发现的图片/视频URL，可直接编辑增删。下载时图片等页面资源优先，视频大文件靠后")
        layout.addWidget(self.media_list, 1)

        # 下载统计
        self.download_stats_label = QLabel("已完成：0/0 | 成功：0 | 失败：0 | 队列中：0")
        layout.addWidget(self.download_stats_label)

        # 失败资源列表 + 操作
        fail_row = QHBoxLayout()
        fail_row.addWidget(QLabel("失败资源列表（重试耗尽后记录）："))
        fail_row.addStretch()
        self.retry_failed_btn = QPushButton("重新下载失败资源")
        self.retry_failed_btn.setEnabled(False)
        self.retry_failed_btn.clicked.connect(self.on_retry_failed)
        self.clear_failed_btn = QPushButton("清空失败列表")
        self.clear_failed_btn.setEnabled(False)
        self.clear_failed_btn.clicked.connect(self.on_clear_failed)
        fail_row.addWidget(self.retry_failed_btn)
        fail_row.addWidget(self.clear_failed_btn)
        layout.addLayout(fail_row)
        self.failed_list = QListWidget()
        self.failed_list.setMaximumHeight(120)
        layout.addWidget(self.failed_list)

        # 下载日志
        self.download_log = QTextEdit()
        self.download_log.setReadOnly(True)
        layout.addWidget(QLabel("下载日志："))
        layout.addWidget(self.download_log, 1)

    # ---------- 反爬设置操作 ----------
    def on_add_ua(self):
        ua = self.ua_add_input.text().strip()
        if not ua:
            QMessageBox.warning(self, "提示", "请输入要添加的User-Agent")
            return
        if self.ua_pool.add(ua):
            self.ua_list.addItem(ua)
            self.ua_add_input.clear()
            self.log_display.append(f"✅ 已添加User-Agent（当前池共{len(self.ua_pool.all())}个）")
        else:
            QMessageBox.information(self, "提示", "该User-Agent已存在或无效")

    def on_del_ua(self):
        item = self.ua_list.currentItem()
        if item is None:
            QMessageBox.warning(self, "提示", "请先选中要删除的User-Agent")
            return
        ua = item.text()
        if len(self.ua_pool.all()) <= 1:
            QMessageBox.warning(self, "提示", "UA池至少需要保留一个User-Agent")
            return
        if self.ua_pool.remove(ua):
            self.ua_list.takeItem(self.ua_list.row(item))
            self.log_display.append(f"已删除User-Agent（当前池共{len(self.ua_pool.all())}个）")

    def on_reset_ua(self):
        self.ua_pool.reset()
        self.ua_list.clear()
        self.ua_list.addItems(self.ua_pool.all())
        self.log_display.append("已恢复默认User-Agent池")

    def _get_proxy_config(self):
        """根据反爬设置返回requests可用的代理字典，未启用或未填地址时返回空字典"""
        if not self.proxy_check.isChecked():
            return {}
        addr = self.proxy_input.text().strip()
        if not addr:
            return {}
        return {"http": addr, "https": addr}

    # ---------- 下载管理操作 ----------
    def _media_items_for(self, urls):
        """按 urls 顺序取出本轮媒体抓取得到的 MediaItem（无对应项则跳过）。

        用户可能在下载管理列表里手动增删 URL，因此必须按当前列表精确匹配，
        多出来的手工 URL 仍走普通下载流程（无元信息）。
        """
        if not self.media_items:
            return []
        index = {item.url: item for item in self.media_items}
        return [index[url] for url in urls if url in index]

    def _resolve_download_dir(self) -> str:
        """把下载目录解析为绝对路径。

        相对路径以项目根目录（config.ROOT_DIR）为基准，避免双击启动/命令行启动
        工作目录不同导致文件被写到其它位置，也便于在日志中直接确认落盘位置。
        """
        raw = self.download_dir_input.text().strip() or "downloads"
        return raw if os.path.isabs(raw) else os.path.join(ROOT_DIR, raw)

    def _update_disk_status(self):
        """定时刷新下载目录所在磁盘的空间使用情况"""
        download_dir = self._resolve_download_dir()
        try:
            usage = shutil.disk_usage(download_dir if os.path.isdir(download_dir) else ".")
            self.disk_status_label.setText(
                f"存储空间：剩余 {format_size(usage.free)} / "
                f"已用 {format_size(usage.used)} / "
                f"总容量 {format_size(usage.total)}")
        except Exception:
            self.disk_status_label.setText("存储空间：无法读取（目录不存在）")

    def on_choose_download_dir(self):
        directory = QFileDialog.getExistingDirectory(self, "选择下载目录",
                                                     self._resolve_download_dir())
        if directory:
            self.download_dir_input.setText(directory)
            self._update_disk_status()

    def _launch_downloader(self, urls):
        """创建并启动下载器实例（开始下载与重新下载失败资源共用）"""
        download_dir = self._resolve_download_dir()
        try:
            os.makedirs(download_dir, exist_ok=True)
        except Exception as e:
            QMessageBox.critical(self, "错误", f"无法创建下载目录：{download_dir}\n{str(e)}")
            return
        if not os.access(download_dir, os.W_OK):
            QMessageBox.critical(self, "错误",
                                 f"下载目录不可写：{download_dir}\n请检查目录权限或更换目录")
            return

        # 携带媒体元信息（合集/序号/请求头）时：按模板命名归档 + 质量过滤
        # 未经过媒体抓取流程（手动粘贴 URL）时保持历史行为（不过滤、不感知去重）
        media_items = self._media_items_for(urls)
        download_kwargs = {}
        if media_items:
            media_config = self._collect_media_config()
            download_kwargs["items"] = media_items
            download_kwargs["media_config"] = media_config
            download_kwargs["filename_template"] = media_config.filename_template

        self.downloader = ContentDownloader(
            urls, download_dir,
            max_workers=self.download_workers_spin.value(),
            max_retries=self.download_retries_spin.value(),
            rate_limit=self.speed_limit_spin.value() * 1024,
            enable_dedup=self.dedup_check.isChecked(),
            compress_quality=self.compress_quality_spin.value(),
            proxy=self._get_proxy_config(),
            ua_pool=self.ua_pool,
            db_path=self.db_path,
            **download_kwargs)
        self.downloader.signal_log.connect(self.download_log.append)
        self.downloader.signal_item_done.connect(self.on_download_item_done)
        self.downloader.signal_progress.connect(self.on_download_progress)
        self.downloader.signal_finish.connect(self.on_download_finish)
        self.downloader.signal_disk_status.connect(self.on_download_disk_status)

        self.download_btn.setEnabled(False)
        self.stop_download_btn.setEnabled(True)
        self.retry_failed_btn.setEnabled(False)
        self.download_stats_label.setText(f"已完成：0/{len(urls)} | 成功：0 | 失败：0 | 队列中：{len(urls)}")
        self.download_log.append(
            f"=== 开始下载 {len(urls)} 个媒体资源到: {download_dir} （绝对路径） ===")
        self.downloader.start()

    def on_start_download(self):
        if self.downloader is not None and self.downloader.isRunning():
            QMessageBox.information(self, "提示", "下载任务正在进行中，请先等待或停止")
            return
        urls = [u.strip() for u in self.media_list.toPlainText().splitlines()
                if u.strip().startswith(('http://', 'https://'))]
        if not urls:
            QMessageBox.warning(self, "提示", "媒体资源列表为空，请先完成爬取或手动粘贴媒体URL")
            return
        self._launch_downloader(urls)

    def on_retry_failed(self):
        """将失败列表中的URL重新加入下载队列"""
        if self.downloader is not None and self.downloader.isRunning():
            QMessageBox.information(self, "提示", "当前下载任务仍在运行，请先等待或停止")
            return
        urls = [self.failed_list.item(i).text() for i in range(self.failed_list.count())]
        if not urls:
            QMessageBox.information(self, "提示", "失败列表为空，没有可重新下载的资源")
            return
        self.failed_list.clear()
        self.clear_failed_btn.setEnabled(False)
        self._launch_downloader(urls)

    def on_clear_failed(self):
        self.failed_list.clear()
        self.clear_failed_btn.setEnabled(False)
        self.retry_failed_btn.setEnabled(False)
        self.download_log.append("已清空失败资源列表")

    def on_stop_download(self):
        if self.downloader is not None and self.downloader.isRunning():
            self.downloader.stop()
            self.download_log.append("⏹ 正在停止下载任务（部分文件可能未完成，下次可断点续传）")

    def on_download_item_done(self, url, path, ok, note):
        if ok:
            tail = f" → {path}" + (f"（{note}）" if note else "")
            self.download_log.append(f"✅ {url}{tail}")
        else:
            self.download_log.append(f"❌ {url}（{note}）")
            if self.failed_list.findItems(url, Qt.MatchFlag.MatchExactly):
                return
            self.failed_list.addItem(url)
            self.retry_failed_btn.setEnabled(True)
            self.clear_failed_btn.setEnabled(True)

    def on_download_progress(self, done, total, success, failed):
        remaining = max(0, total - done)
        self.download_stats_label.setText(
            f"已完成：{done}/{total} | 成功：{success} | 失败：{failed} | 队列中：{remaining}")

    def on_download_disk_status(self, text):
        self.disk_status_label.setText(f"存储空间：{text}")

    def on_download_finish(self, success, failed, failed_urls, manifest_path):
        self.download_btn.setEnabled(True)
        self.stop_download_btn.setEnabled(False)
        self.retry_failed_btn.setEnabled(bool(failed_urls))
        self.clear_failed_btn.setEnabled(bool(failed_urls))
        # 失败列表增量补全（防止finish前个别信号丢失）
        for url in failed_urls:
            if not self.failed_list.findItems(url, Qt.MatchFlag.MatchExactly):
                self.failed_list.addItem(url)
        msg = f"媒体下载结束：\n成功 {success} 个，失败 {failed} 个"
        if manifest_path:
            msg += f"\n\n资源清单：\n{manifest_path}"
        if failed_urls:
            msg += f"\n\n{failed} 个资源下载失败，可点击「重新下载失败资源」重试"
        self.download_log.append(f"🎉 媒体下载结束：成功 {success} 个，失败 {failed} 个")
        QMessageBox.information(self, "下载完成", msg)

    # ---------- 断点续爬：启动时检测未完成任务 ----------
    def _get_unfinished_tasks(self):
        """查询数据库中状态为running的爬取任务"""
        return get_unfinished_tasks(self.db_path)

    def _abandon_unfinished_tasks(self):
        """将未完成任务标记为完成（用户选择开始新任务时调用）"""
        abandon_unfinished_tasks(self.db_path)

    def check_unfinished_task(self):
        """启动时检测未完成任务，提供"继续上次任务"或"开始新任务"选项"""
        tasks = self._get_unfinished_tasks()
        if not tasks:
            return
        task = tasks[0]
        reply = QMessageBox.question(
            self, "发现未完成的任务",
            f"检测到上次存在未完成的爬取任务：\n\n"
            f"起始网址：{task['start_url']}\n"
            f"递归深度：{task['max_depth']}\n\n"
            f"是否继续上次任务？\n（已爬取的页面会自动跳过）\n"
            f"选择\"否\"则放弃上次任务并开始新任务。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            self.pending_crawl_url = task["start_url"]
            self.url_input.setText(task["start_url"])
            self.depth_spin.setValue(task["max_depth"])
            self.log_display.append("=== 检测到未完成任务，正在继续上次任务（已爬页面自动跳过） ===")
            self._start_crawl_with(task["start_url"], task["max_depth"], resume_task_id=task["id"])
        elif reply == QMessageBox.StandardButton.No:
            self._abandon_unfinished_tasks()
            self.log_display.append("已放弃上次未完成任务，请配置新的爬取任务")

    # ---------- URL过滤规则（工具栏输入框 / 过滤管理对话框，共享URLFilter实例） ----------
    def _on_block_regex_changed(self, text):
        """工具栏输入框内容变化：解析分号分隔的多个正则，批量同步到URLFilter（实时生效）"""
        if self._syncing_block_input:
            return  # 程序自动同步文本时跳过，避免递归
        patterns = [p.strip() for p in re.split(r"[;；]", text) if p.strip()]
        self.url_filter.set_block_patterns(patterns)
        self._update_filter_stats()

    def _sync_block_input(self):
        """将URLFilter中的全部屏蔽正则同步回工具栏输入框（分号分隔）"""
        text = ";".join(self.url_filter.block_patterns())
        if text == self.block_regex_input.text():
            return
        self._syncing_block_input = True
        try:
            self.block_regex_input.setText(text)
        finally:
            self._syncing_block_input = False
        self._update_filter_stats()

    def _update_filter_stats(self):
        """刷新工具栏过滤统计显示"""
        stats = self.url_filter.get_stats()
        self.filter_stats_label.setText(f"已过滤URL：{stats['filtered_count']}")

    def on_open_filter_dialog(self):
        """打开URL过滤规则管理对话框（正则/白名单/统计样本/导入导出）"""
        dialog = URLFilterDialog(self, self.url_filter)
        # 关闭时把对话框内的规则变更同步回工具栏输入框
        dialog.accepted.connect(self._sync_block_input)
        dialog.exec()

    def on_open_security_dialog(self):
        """打开广告/钓鱼安全检测对话框。

        识别广告与钓鱼链接并生成覆盖同类模式的屏蔽正则，
        选中的规则直接写入共享的 URLFilter（实时生效）；
        关闭时把新增规则同步回工具栏的屏蔽正则输入框。
        """
        dialog = LinkGuardDialog(self, self.url_filter,
                                 initial_urls=list(self.last_crawl_links))
        dialog.accepted.connect(self._sync_block_input)
        dialog.exec()

    def on_open_rule_dialog(self):
        """打开站点规则管理对话框（编辑/校验/测试 resources/rules/*.json）。"""
        dialog = SiteRuleDialog(self)
        dialog.exec()
        self._refresh_browser_status()   # 规则不影响浏览器，但保持状态提示同步

    def _toggle_pause(self):
        """切换暂停/继续状态（对当前活动的抓取线程生效：媒体抓取或递归爬取）"""
        thread = self._active_thread()
        if thread is None:
            return
        paused = (thread.is_paused() if hasattr(thread, "is_paused")
                  else not thread._pause_event.is_set())
        if not paused:
            thread.pause()
            self.log_display.append("⏸ 已暂停抓取（按 Ctrl+P / Ctrl+R 继续）")
        else:
            thread.resume()
            self.log_display.append("▶ 已继续抓取")

    def on_check_robots(self):
        ok, err_msg = validate_url(self.url_input.text())
        if not ok:
            QMessageBox.warning(self, "提示", err_msg)
            return

        self.start_check_btn.setEnabled(False)
        self.robots_display.setText("正在获取robots.txt内容...")

        self.robots_thread = RobotsCheckThread(self.url_input.text().strip())
        self.robots_thread.signal_robots_content.connect(self.on_receive_robots)
        self.robots_thread.signal_error.connect(self.on_robots_error)
        self.robots_thread.start()

    def on_receive_robots(self, content, robots_url):
        self.robots_display.setText(f"robots.txt 地址：{robots_url}\n\n{content}")
        self.pending_crawl_url = self.url_input.text().strip()
        self.robots_content = content
        self.start_check_btn.setEnabled(True)

        # 解析robots.txt规则，若禁止爬虫则直接结束爬取流程并弹窗提示
        parser = RobotsParser(content)
        if parser.is_disallowed(self.pending_crawl_url, CRAWLER_USER_AGENT):
            self.confirm_crawl_btn.setEnabled(False)
            self.log_display.append("robots.txt 规则禁止爬虫访问，已终止爬取流程")
            QMessageBox.warning(self, "robots.txt 禁止爬取",
                                f"该站点的 robots.txt 规则禁止爬虫访问：\n\n{self.pending_crawl_url}\n\n已终止爬取流程。")
            return

        self.confirm_crawl_btn.setEnabled(True)

        reply = QMessageBox.question(self, "爬取确认",
                                     f"已获取站点robots规则，即将启动最大深度为{self.depth_spin.value()}的递归爬取，请确认你已遵守站点爬取规范，是否继续？",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            self.on_start_crawl()

    def on_robots_error(self, error_msg):
        self.robots_display.setText(error_msg)
        self.start_check_btn.setEnabled(True)
        QMessageBox.critical(self, "错误", error_msg)

    def on_start_crawl(self):
        ok, err_msg = validate_url(self.pending_crawl_url)
        if not ok:
            QMessageBox.warning(self, "提示", err_msg)
            return

        # 兜底校验：若robots.txt规则禁止爬取则直接结束
        if self.robots_content:
            parser = RobotsParser(self.robots_content)
            if parser.is_disallowed(self.pending_crawl_url, CRAWLER_USER_AGENT):
                self.confirm_crawl_btn.setEnabled(False)
                QMessageBox.warning(self, "robots.txt 禁止爬取",
                                    "该站点的 robots.txt 规则禁止爬虫访问，已终止爬取流程。")
                return

        # 媒体抓取（图像/视频）与递归爬取共用起始网址与反爬设置，按开关分流
        if self.media_enable_check.isChecked():
            if not any(check.isChecked() for check in
                       (self.media_image_check, self.media_video_check,
                        self.media_audio_check)):
                QMessageBox.warning(self, "提示",
                                    "请至少勾选一种目标类型（图片 / 视频 / 音频）")
                return
            self._start_media_crawl_with(self.pending_crawl_url)
        else:
            self._start_crawl_with(self.pending_crawl_url, self.depth_spin.value())

    def _start_crawl_with(self, url, max_depth, resume_task_id=None):
        """启动爬取线程（首次启动与断点续爬共用）

        resume_task_id: 续爬时传入原任务ID，复用其已爬记录实现断点续爬；
                        None 表示全新任务（全量爬取，不受历史记录影响）。
        """
        self.confirm_crawl_btn.setEnabled(False)
        self.log_display.append("=== 递归爬取任务启动 ===")

        # 重置本次任务结果缓存（新一轮爬取重新收集媒体资源）
        self.media_urls = []
        self.last_crawl_links = []
        self.last_page_texts = {}
        self.media_list.clear()
        self.download_btn.setEnabled(False)

        self.crawl_thread = RecursiveCrawlerThread(
            url, max_depth,
            crawl_external=self.crawl_external_check.isChecked(),
            request_delay=self.delay_spin.value(),
            max_chars_per_page=self.max_chars_spin.value(),
            link_filter=self.link_filter_input.text().strip(),
            db_path=self.db_path,
            ua_pool=self.ua_pool,
            jitter=self.jitter_spin.value(),
            proxy=self._get_proxy_config(),
            url_filter=self.url_filter,  # 共享过滤规则实例，实时生效
            max_pages=self.max_pages_spin.value(),  # 自动停止阈值
            resume_task_id=resume_task_id)  # 续爬时复用原任务ID

        self.crawl_thread.signal_log.connect(self.log_display.append)
        self.crawl_thread.signal_single_page_done.connect(lambda url, info: self.log_display.append(f"✅ 完成 [{url}] {info}"))
        self.crawl_thread.signal_progress.connect(self.on_crawl_progress)
        self.crawl_thread.signal_media_found.connect(self.on_media_found)
        self.crawl_thread.signal_finish.connect(self.on_crawl_finish)
        self.crawl_thread.signal_stopped.connect(self.on_crawl_stopped)
        self.crawl_thread.signal_error.connect(self.on_crawl_error)
        self.crawl_thread.start()
        # 启动后启用停止按钮（手动停止入口）
        self.stop_crawl_btn.setEnabled(True)

    def on_crawl_progress(self, pages, depth, max_depth):
        self.crawl_stats_label.setText(
            f"已爬页面：{pages} | 当前层：{min(depth + 1, max_depth + 1)}/{max_depth + 1} | "
            f"提取链接：{len(self.last_crawl_links)} | 媒体资源：{len(self.media_urls)}")

    def on_media_found(self, media_urls):
        """爬取线程回传媒体资源（爬取中逐页增量推送，结束/停止/异常时为最终快照）。

        与下载管理（媒体资源列表）的同步策略：
        - 合并现有内容（含用户手动编辑/粘贴的URL），去重保序，不覆盖用户输入
        - 仅在内容确有变化时刷新控件，避免频繁重绘
        """
        incoming = [str(u).strip() for u in (media_urls or [])
                    if str(u).strip().startswith(("http://", "https://"))]
        existing = [ln.strip() for ln in self.media_list.toPlainText().splitlines() if ln.strip()]
        merged = list(existing)
        for url in incoming:
            if url not in merged:
                merged.append(url)
        self.media_urls = [u for u in merged if u.startswith(("http://", "https://"))]
        if merged and "\n".join(merged) != self.media_list.toPlainText():
            self.media_list.setPlainText("\n".join(merged))
        self.download_btn.setEnabled(bool(self.media_urls))
        # 统计实时刷新：爬取过程中即可看到媒体资源数量增长
        self.crawl_stats_label.setText(
            f"已爬页面：{len(self.last_crawl_links) if self.last_crawl_links else 0} | 当前层：爬取中 | "
            f"提取链接：{len(self.last_crawl_links)} | 媒体资源：{len(self.media_urls)}")

    # ---------- 媒体抓取流程（图像/视频增强） ----------
    def _start_media_crawl_with(self, url):
        """启动媒体抓取线程（与递归爬取互斥，按钮状态统一管理）。"""
        config = self._collect_media_config()
        self.confirm_crawl_btn.setEnabled(False)
        self.stop_crawl_btn.setEnabled(True)
        # 重置本轮结果
        self.media_urls = []
        self.media_items = []
        self.last_crawl_links = []
        self.last_page_texts = {}
        self.media_list.clear()
        self._clear_media_table()
        self.download_btn.setEnabled(False)
        self.log_display.clear()
        self.log_display.append("=== 媒体抓取任务启动（图像/视频增强流程） ===")

        max_chars = self.max_chars_spin.value()
        self.media_thread = MediaCrawlThread(
            url,
            config=config,
            request_delay=self.delay_spin.value(),
            jitter=self.jitter_spin.value(),
            proxy=self._get_proxy_config(),
            ua_pool=self.ua_pool,          # 复用反爬设置中的 UA 池
            url_filter=self.url_filter,    # 复用屏蔽正则（域名白名单不作用于媒体）
            page_timeout=DEFAULT_MEDIA_PAGE_TIMEOUT,
            max_page_bytes=max_chars * 4 if max_chars > 0 else 0)
        self.media_thread.signal_log.connect(self.log_display.append)
        self.media_thread.signal_page_done.connect(
            lambda page_url, info: self.log_display.append(f"✅ [{page_url}] {info}"))
        self.media_thread.signal_media_found.connect(self.on_media_found)
        self.media_thread.signal_media_items.connect(self.on_media_items)
        self.media_thread.signal_progress.connect(self.on_media_progress)
        self.media_thread.signal_finish.connect(self.on_media_finish)
        self.media_thread.signal_stopped.connect(self.on_media_stopped)
        self.media_thread.signal_error.connect(self.on_media_error)
        self.media_thread.start()

    def on_media_items(self, items):
        """媒体抓取线程回传 MediaItem 列表（含合集/序号/分辨率元信息）。"""
        self.media_items = list(items or [])
        self._fill_media_table(self.media_items)
        self.export_media_btn.setEnabled(bool(self.media_items))

    def on_media_progress(self, pages, media, pending):
        """媒体抓取进度：已抓页面 / 已发现媒体 / 队列剩余。"""
        self.crawl_stats_label.setText(
            f"已抓页面：{pages} | 媒体资源：{media} | 待抓页面：{pending} | "
            f"提取链接：{len(self.last_crawl_links)}")

    def on_media_finish(self, pages, media_count, stats):
        """媒体抓取正常结束：入库并提示。"""
        self.confirm_crawl_btn.setEnabled(True)
        self.stop_crawl_btn.setEnabled(False)
        saved = save_media_items(self.db_path, self.media_items)
        self.log_display.append(f"🎉 媒体抓取完成：页面 {pages} 个，媒体 {media_count} 个"
                                f"（已入库 {saved} 条）")
        if self.media_items and not saved:
            self.log_display.append("⚠ 媒体元信息入库失败（数据库不可用？），不影响下载流程")
        self._finish_media_ui(pages, media_count, stats, "媒体抓取完成")

    def on_media_stopped(self, pages, media_count, stats):
        """媒体抓取被手动停止：同样入库已发现结果。"""
        saved = save_media_items(self.db_path, self.media_items)
        self.log_display.append(f"⏹ 媒体抓取已停止：页面 {pages} 个，媒体 {media_count} 个"
                                f"（已入库 {saved} 条）")
        self._finish_media_ui(pages, media_count, stats, "媒体抓取已停止")

    def _finish_media_ui(self, pages, media_count, stats, title):
        """媒体抓取结束后的界面收尾（统计、按钮、消息框）。"""
        self.media_urls = [item.url for item in self.media_items]
        self.download_btn.setEnabled(bool(self.media_urls))
        self.export_media_btn.setEnabled(bool(self.media_items))
        if self.media_urls:
            # 结果同步到下载管理列表（保留用户手动编辑的内容）
            existing = [line.strip() for line in
                        self.media_list.toPlainText().splitlines() if line.strip()]
            merged = list(existing)
            for url in self.media_urls:
                if url not in merged:
                    merged.append(url)
            self.media_list.setPlainText("\n".join(merged))
        self.crawl_stats_label.setText(
            f"已抓页面：{pages} | 媒体资源：{media_count} | 过滤统计：{stats}")
        message = (f"共抓取 {pages} 个页面，发现 {media_count} 个媒体资源。\n\n"
                   f"过滤统计：{stats}")
        if self.media_urls:
            message += "\n\n已同步到「下载管理」，点击「开始下载」即可保存到本地。"
        else:
            message += ("\n\n未发现媒体资源：可尝试切换为「浏览器渲染」模式，"
                        "或放宽最小宽高/体积限制后重试。")
        QMessageBox.information(self, title, message)

    def on_media_error(self, error_msg):
        """媒体抓取异常：恢复按钮并提示。"""
        self.log_display.append(error_msg)
        self.confirm_crawl_btn.setEnabled(True)
        self.stop_crawl_btn.setEnabled(False)
        QMessageBox.critical(self, "媒体抓取异常", error_msg)

    # ---------- 停止爬取（手动/自动） ----------
    def on_stop_crawl(self):
        """手动停止按钮：请求当前抓取线程停止并清理资源。

        停止过程：设置停止标志 → 中断当前HTTP请求 → 线程在检查点退出 →
        递归爬取的已爬数据已写入数据库且任务保持running状态，重启后可选「继续上次任务」。
        媒体抓取为动态模式时，正在渲染的页面需等其超时后退出（浏览器对象
        只能在创建它的线程内操作）。
        """
        thread = self._active_thread()
        if thread is None:
            self.stop_crawl_btn.setEnabled(False)
            return
        self.stop_crawl_btn.setEnabled(False)
        self.log_display.append("⏹ 正在停止抓取任务：中断请求并保存进度...")
        thread.stop()  # 线程安全：设置标志并中断进行中的请求

    def _active_thread(self):
        """返回当前正在运行的抓取线程（媒体抓取优先）。"""
        for candidate in (getattr(self, "media_thread", None),
                          getattr(self, "crawl_thread", None)):
            if candidate is not None and candidate.isRunning():
                return candidate
        return None

    def on_crawl_stopped(self, total_count, all_links, page_texts):
        """爬取线程手动停止后回传部分结果（GUI线程处理）"""
        self.log_display.append(f"⏹ 爬取任务已停止，累计爬取 {total_count} 个页面（进度已保存，可继续上次任务）")
        self.stop_crawl_btn.setEnabled(False)
        # 输出已收集到的链接
        self.link_display.setText(f"共提取到 {len(all_links)} 个唯一链接（已停止，部分结果）：\n" + "-" * 50 + "\n")
        for idx, link in enumerate(all_links, 1):
            self.link_display.append(f"{idx}. {link}")
        # 缓存部分结果供导出
        self.last_crawl_links = all_links
        self.last_page_texts = page_texts
        self.confirm_crawl_btn.setEnabled(True)
        self.export_csv_btn.setEnabled(True)
        self.export_txt_btn.setEnabled(True)
        self.crawl_stats_label.setText(
            f"已爬页面：{total_count} | 当前层：已停止 | "
            f"提取链接：{len(all_links)} | 媒体资源：{len(self.media_urls)}")
        QMessageBox.information(self, "爬取已停止",
                                f"爬取任务已停止，共爬取 {total_count} 个页面，提取到 {len(all_links)} 个链接，"
                                f"发现 {len(self.media_urls)} 个媒体资源。\n"
                                f"进度已保存：重启程序后可选择「继续上次任务」。")

    def on_crawl_finish(self, total_count, all_links, page_texts):
        self.log_display.append(f"\n🎉 全部爬取完成，累计成功爬取 {total_count} 个页面")

        # 输出全部提取到的链接
        self.link_display.setText(f"共提取到 {len(all_links)} 个唯一链接：\n" + "-" * 50 + "\n")
        for idx, link in enumerate(all_links, 1):
            self.link_display.append(f"{idx}. {link}")

        # 缓存结果供导出CSV/TXT使用
        self.last_crawl_links = all_links
        self.last_page_texts = page_texts
        self.confirm_crawl_btn.setEnabled(True)
        self.stop_crawl_btn.setEnabled(False)
        self.export_csv_btn.setEnabled(True)
        self.export_txt_btn.setEnabled(True)
        self.crawl_stats_label.setText(
            f"已爬页面：{total_count} | 当前层：完成 | "
            f"提取链接：{len(all_links)} | 媒体资源：{len(self.media_urls)}")
        QMessageBox.information(self, "爬取完成", f"递归爬取已结束，共爬取 {total_count} 个页面，提取到 {len(all_links)} 个链接，发现 {len(self.media_urls)} 个媒体资源")

    def on_crawl_error(self, error_msg):
        self.log_display.append(error_msg)
        self.confirm_crawl_btn.setEnabled(True)
        self.stop_crawl_btn.setEnabled(False)
        QMessageBox.critical(self, "爬取异常", error_msg)

    # ---------- 导出（CSV / JSON / Markdown） ----------
    def on_export_csv(self):
        """导出按钮：打开导出配置对话框（默认CSV格式）"""
        self._open_export_dialog("CSV")

    def on_export_txt(self):
        """导出按钮：打开导出配置对话框（默认Markdown格式）"""
        self._open_export_dialog("Markdown")

    def on_export_media(self):
        """导出媒体清单（类型/合集/序号/分辨率/来源页等元信息）。"""
        if not self.media_items:
            QMessageBox.warning(self, "提示",
                                "当前没有可导出的媒体清单，请先执行一次媒体抓取")
            return
        file_path, _selected = QFileDialog.getSaveFileName(
            self, "导出媒体清单", export_default_name("csv", prefix="media"),
            "CSV 文件 (*.csv);;JSON 文件 (*.json);;Markdown 文件 (*.md);;所有文件 (*)")
        if not file_path:
            return
        lowered = file_path.lower()
        fmt = "json" if lowered.endswith(".json") else \
            "markdown" if lowered.endswith((".md", ".markdown")) else "csv"
        try:
            count = write_media_export(file_path, fmt, self.media_items)
        except OSError as exc:
            QMessageBox.critical(self, "导出失败", f"写入文件失败：{str(exc)}")
            return
        self.log_display.append(f"✅ 已导出 {count} 条媒体清单到: {file_path}")
        QMessageBox.information(self, "导出成功",
                                f"已成功导出 {count} 条媒体记录到：\n{file_path}")

    def _open_export_dialog(self, default_format):
        """打开ExportDialog，按用户选择的格式与字段导出"""
        if not self.last_crawl_links:
            QMessageBox.warning(self, "提示", "当前没有可导出的爬取结果，请先完成一次爬取")
            return

        dialog = ExportDialog(self, default_format=default_format,
                              has_text=bool(self.last_page_texts))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        fmt = dialog.selected_format()
        fields = dialog.selected_fields()
        if not fields:
            QMessageBox.warning(self, "提示", "请至少勾选一个导出字段")
            return

        self._export_results(fmt, fields)

    def _export_results(self, fmt, fields):
        """按选定格式与字段把爬取结果写入用户选择的文件（委托 ui.export）"""
        fmt = fmt if fmt in ("csv", "json", "markdown") else "csv"
        default_name = export_default_name(fmt)
        file_filter = export_file_filter(fmt)
        file_path, _ = QFileDialog.getSaveFileName(self, "导出爬取结果", default_name, file_filter)
        if not file_path:
            return
        try:
            count = write_export(file_path, fmt, fields,
                                 self.last_crawl_links, self.last_page_texts)
            self.log_display.append(f"✅ 已导出 {count} 条记录到: {file_path}")
            QMessageBox.information(self, "导出成功", f"已成功导出 {count} 条记录到：\n{file_path}")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", f"写入文件失败：{str(e)}")

    def on_clear_all(self):
        self.url_input.clear()
        self.robots_display.clear()
        self.log_display.clear()
        self.link_display.clear()
        self.media_list.clear()
        self.media_items = []
        self.media_urls = []
        self._clear_media_table()
        self.export_media_btn.setEnabled(False)
        self.download_log.clear()
        self.failed_list.clear()
        self.confirm_crawl_btn.setEnabled(False)
        self.stop_crawl_btn.setEnabled(False)
        self.export_csv_btn.setEnabled(False)
        self.export_txt_btn.setEnabled(False)
        self.download_btn.setEnabled(False)
        self.retry_failed_btn.setEnabled(False)
        self.clear_failed_btn.setEnabled(False)
        self.crawl_stats_label.setText("已爬页面：0 | 当前层：-/- | 提取链接：0 | 媒体资源：0")
        self.download_stats_label.setText("已完成：0/0 | 成功：0 | 失败：0 | 队列中：0")
        self.pending_crawl_url = ""
        self.robots_content = ""
        self.last_crawl_links = []
        self.last_page_texts = {}
        self.media_urls = []

    def on_clear_crawl_records(self):
        """清空数据库中的已爬URL记录，使重新爬取同一站点不受历史去重影响"""
        if self.crawl_thread is not None and self.crawl_thread.isRunning():
            QMessageBox.warning(self, "提示", "请先停止正在运行的爬取任务，再清空爬取记录。")
            return
        reply = QMessageBox.question(
            self, "清空爬取记录",
            "确定要清空数据库中的已爬URL记录吗？\n\n"
            "清空后重新爬取同一站点时，将不再跳过历史URL（已下载的媒体文件不受影响）。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            count = clear_crawled_records(self.db_path)
            self.log_display.append(f"🗑 已清空 {count} 条爬取记录，重新爬取同一站点将不再被历史记录跳过")
        except Exception as e:
            QMessageBox.critical(self, "错误", f"清空爬取记录失败：{str(e)}")

    # ---------- 任务管理标签页（多目标并发爬取） ----------
    def _init_task_manager_tab(self):
        """初始化「任务管理」标签页（延迟导入 ui.widgets，避免包初始化耦合）。"""
        from ui.widgets import TaskManagerTab
        self.task_tab = TaskManagerTab(main_window=self)
        self.tabs.addTab(self.task_tab, "任务管理")

    def _stop_task_manager(self):
        """停止任务管理器中所有运行中的多任务爬虫并保存任务队列（窗口关闭时调用）"""
        task_tab = getattr(self, "task_tab", None)
        if task_tab is not None:
            try:
                task_tab.scheduler.shutdown(save_queue=True)
            except Exception:
                pass

    # ---------- 安全退出与命令行停止 ----------
    def request_stop_and_quit(self):
        """命令行/信号触发（如 Ctrl+C）的停止与安全退出。

        停止所有运行中的爬取/下载线程并等待资源清理完成后关闭窗口。
        已爬数据已写入数据库，重启后可选择"继续上次任务"恢复。
        """
        self.log_display.append("⏹ 收到停止指令（Ctrl+C），正在停止任务并安全退出...")
        self._stop_crawl_threads()
        downloader = self.downloader
        if downloader is not None and downloader.isRunning():
            downloader.stop()
            downloader.wait(5000)
        self.close()  # 触发closeEvent完成退出

    def _stop_crawl_threads(self, timeout_ms: int = 5000) -> None:
        """停止递归爬取与媒体抓取线程并等待其释放资源（浏览器/连接池）。"""
        for name in ("media_thread", "crawl_thread"):
            thread = getattr(self, name, None)
            if thread is not None and thread.isRunning():
                thread.stop()
                thread.wait(timeout_ms)

    def closeEvent(self, event):
        """窗口关闭事件：停止运行中的任务并清理资源后再退出。

        若存在运行中的爬取/下载线程，先询问用户，确认后停止并等待其退出，
        避免残留线程或未释放的连接导致进程无法安全结束。
        """
        # 命令行停止时跳过询问，直接清理退出
        if getattr(self, "_force_close", False):
            self._stop_task_manager()  # 多任务管理器：停止并保存任务队列
            event.accept()
            return
        thread = getattr(self, "crawl_thread", None)
        media_thread = getattr(self, "media_thread", None)
        downloader = self.downloader
        task_tab = getattr(self, "task_tab", None)
        running = (thread is not None and thread.isRunning()) or \
                  (media_thread is not None and media_thread.isRunning()) or \
                  (downloader is not None and downloader.isRunning()) or \
                  (task_tab is not None and task_tab.scheduler.has_running_tasks())
        if running:
            reply = QMessageBox.question(
                self, "退出确认",
                "仍有任务正在运行（爬取/媒体抓取/下载/多任务管理）。\n\n"
                "停止任务并退出？\n（已爬取的数据与进度会保存，重启后可继续上次任务）",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._stop_crawl_threads()
            if downloader is not None and downloader.isRunning():
                downloader.stop()
                downloader.wait(5000)
            self._stop_task_manager()  # 多任务管理器：停止并保存任务队列
        event.accept()


__all__ = ["CrawlerMainWindow"]
