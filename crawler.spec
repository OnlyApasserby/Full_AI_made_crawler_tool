# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：把本项目打成单个 exe（onefile）。

用法
----
    # 常规构建（推荐，exe 约 200MB）
    pyinstaller crawler.spec --noconfirm

    # 完全自包含：把本地浏览器内核也打进去（exe 约 650MB，见下方说明）
    set BUNDLE_BROWSER=1
    pyinstaller crawler.spec --noconfirm

    # 需要查看启动报错时（临时开启控制台窗口）
    set BUILD_CONSOLE=1
    pyinstaller crawler.spec --noconfirm

是否打包浏览器内核的取舍（重要）
------------------------------
``--onefile`` 的 exe **每次启动都会把内容解压到临时目录**：
- 不打内核：exe ≈ 200MB，解压快，启动约 5–15 秒；
  动态渲染会自动使用：系统已装的 Chrome / exe 同级 ``resources\\browsers`` /
  放在 exe 同级的 ``chrome-win64.zip``（首次渲染自动解压一次）。
- 打内核（BUNDLE_BROWSER=1）：exe ≈ 650MB，**每次启动都要解压 600MB+**，
  启动明显变慢、占用临时目录数 GB；好处是完全离线自包含、拷走就能用。

因此默认**不打包**内核：把 ``chrome-win64.zip`` 或解压好的 ``resources\\browsers``
放在 exe 同级即可获得动态渲染能力。
"""

import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# ---------------------------------------------------------------------------
# 构建开关（环境变量）
# ---------------------------------------------------------------------------
BUNDLE_BROWSER = os.environ.get("BUNDLE_BROWSER", "0").strip().lower() in (
    "1", "true", "yes", "on")
BUILD_CONSOLE = os.environ.get("BUILD_CONSOLE", "0").strip().lower() in (
    "1", "true", "yes", "on")

SPEC_DIR = globals().get("SPECPATH", os.getcwd())
# 控制台版用于排查（可运行 --selftest 查看输出），与发布版区分文件名
APP_EXE_NAME = "FullAICrawler" + ("-debug" if BUILD_CONSOLE else "")

# ---------------------------------------------------------------------------
# 数据文件
# ---------------------------------------------------------------------------
datas = []

# 1) 站点规则模板与随包规则（只读内置；用户自己的规则写在 exe 同级 resources/rules）
rules_dir = os.path.join(SPEC_DIR, "resources", "rules")
if os.path.isdir(rules_dir):
    datas.append((rules_dir, os.path.join("resources", "rules")))

# 2) 本地浏览器内核（默认不打，见文件头说明）
if BUNDLE_BROWSER:
    browsers_dir = os.path.join(SPEC_DIR, "resources", "browsers")
    if os.path.isdir(browsers_dir):
        datas.append((browsers_dir, os.path.join("resources", "browsers")))

# 3) playwright 的 driver（node.exe + cli.js）：同步 API 运行必需，约 100MB
try:
    datas.extend(collect_data_files("playwright"))
except Exception:
    pass

# ---------------------------------------------------------------------------
# 隐藏导入：运行时动态 import / 延迟 import 的模块，静态分析发现不了
# ---------------------------------------------------------------------------
hiddenimports = [
    # 本项目：延迟导入的模块必须显式声明
    "ui.widgets",              # 「任务管理」标签页在 main_window 中延迟导入
    "core.media.hls",          # 流媒体解析在下载器中延迟导入
    "core.media.dash",
    "core.media.merger",
    "core.extractor.sites",    # 插件目录（pkgutil 动态扫描）
    "core.extractor.rule_extractor",
    # playwright 及其依赖
    "playwright",
    "playwright.sync_api",
    "playwright.async_api",
    "greenlet",
    "pyee",
    "pyee.base",
    # 网络与编码
    "requests",
    "urllib3",
    "urllib3.contrib",
    "charset_normalizer",
    "certifi",
    "idna",
    # 可选能力（缺失时程序会降级，打包进去才能使用完整功能）
    "PIL",
    "PIL.Image",
    "Crypto",
    "Crypto.Cipher",
    "Crypto.Cipher.AES",
    "Crypto.Util.Padding",
    "cryptography",
    "bs4",
    "bs4.builder",
    "lxml",
    "lxml.etree",
    "lxml._elementpath",
    # Qt
    "PyQt6.QtCore",
    "PyQt6.QtGui",
    "PyQt6.QtWidgets",
    # 标准库
    "sqlite3",
    "concurrent.futures",
    "xml.etree.ElementTree",
]
# 插件子模块（如后续新增站点适配器，需在此登记才会被打包）
hiddenimports.extend(collect_submodules("core"))

# ---------------------------------------------------------------------------
# 排除：体积大头且与本项目无关
# ---------------------------------------------------------------------------
excludes = [
    "tkinter", "unittest", "doctest", "pydoc", "pdb",
    "pytest", "_pytest", "IPython", "notebook",
    "PyQt5", "PySide2", "PySide6",
    "numpy", "matplotlib", "scipy", "pandas",
    "PyQt6.QtWebEngineCore", "PyQt6.QtWebEngineWidgets", "PyQt6.Qt3DCore",
    "PyQt6.QtQuick", "PyQt6.QtQml", "PyQt6.QtMultimedia", "PyQt6.QtCharts",
    "PyQt6.QtDataVisualization", "PyQt6.QtBluetooth", "PyQt6.QtNfc",
    "PyQt6.QtPositioning", "PyQt6.QtSensors", "PyQt6.QtSerialPort",
    "PyQt6.QtSql", "PyQt6.QtTest", "PyQt6.QtDesigner", "PyQt6.QtHelp",
]

# ---------------------------------------------------------------------------
# 构建
# ---------------------------------------------------------------------------
block_cipher = None

a = Analysis(
    ["main.py"],
    pathex=[SPEC_DIR],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name=APP_EXE_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                 # 不压缩：换启动速度（压缩率低且解压更慢）
    runtime_tmpdir=None,      # onefile：使用系统临时目录
    console=BUILD_CONSOLE,    # GUI 程序默认不弹控制台；调试时设 BUILD_CONSOLE=1
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,                 # 如需图标：改成 icon="path/to/app.ico"
    version=None,
)
