"""程序入口。

启动方式（在本项目根目录执行）：

    python main.py

流程：创建 QApplication → 构建主窗口（含任务管理标签页）→ 注册 Ctrl+C 安全
退出（Qt 事件循环默认阻塞在 select 上，用定时器周期性唤醒主线程，使 SIGINT
能可靠触发命令行停止）→ 进入事件循环。
"""

import signal
import sys

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

from ui.main_window import CrawlerMainWindow


def run_selftest() -> int:
    """环境自检（不启动界面）：用于验证打包后的 exe 是否可用。

    用法：``FullAICrawler.exe --selftest``（需在控制台模式下查看输出）。
    逐项检查路径、数据库、浏览器内核、Playwright、规则与可选依赖是否就绪。
    """
    from config import IS_FROZEN, RESOURCES_DIR, ROOT_DIR, ensure_resources_dir

    print("=== 环境自检 ===")
    print(f"运行模式：{'打包 exe' if IS_FROZEN else '源码'} | 应用目录：{ROOT_DIR}")
    print(f"资源目录：{RESOURCES_DIR}")

    try:
        ensure_resources_dir()
        print("[OK]   资源目录可写")
    except OSError as exc:
        print(f"[FAIL] 资源目录不可写：{exc}")

    try:
        from manager.db_manager import ensure_schema

        from config import default_db_path
        ensure_schema(default_db_path())
        print(f"[OK]   数据库可用：{default_db_path()}")
    except Exception as exc:
        print(f"[FAIL] 数据库不可用：{exc}")

    try:
        from core.fetcher import chrome_setup

        path = chrome_setup.find_executable()
        print(f"[{'OK' if path else 'WARN'}]   浏览器内核：{path or '未找到'}"
              + ("" if path else "（把 chrome-win64.zip 放到 exe 同级可自动解压）"))
    except Exception as exc:
        print(f"[FAIL] 浏览器内核检测异常：{exc}")

    try:
        from core.fetcher import playwright_status

        available, reason = playwright_status()
        print(f"[{'OK' if available else 'WARN'}]   Playwright："
              + ("可用" if available else str(reason).splitlines()[0]))
    except Exception as exc:
        print(f"[FAIL] Playwright 检测异常：{exc}")

    try:
        from core.extractor import default_extractors, load_rules

        rules = load_rules()
        names = [extractor.name for extractor in default_extractors()]
        print(f"[OK]   提取器：{names} | 已加载规则 {len(rules)} 条")
    except Exception as exc:
        print(f"[FAIL] 提取器/规则加载异常：{exc}")

    try:
        from core.media.merger import find_ffmpeg

        ffmpeg = find_ffmpeg()
        print(f"[{'OK' if ffmpeg else 'WARN'}]   ffmpeg：{ffmpeg or '未找到（流媒体将保留 .ts 容器）'}")
    except Exception as exc:
        print(f"[FAIL] ffmpeg 检测异常：{exc}")

    try:
        from PIL import Image
        print(f"[OK]   Pillow：{Image.__version__}")
    except Exception:
        print("[WARN] Pillow：不可用（图片压缩/尺寸探测/感知去重将降级）")
    try:
        from Crypto.Cipher import AES  # noqa: F401
        print("[OK]   加密库：可用（支持 AES-128 流媒体解密）")
    except Exception:
        print("[WARN] 加密库：不可用（加密流无法解密）")
    print("=== 自检结束 ===")
    return 0


def run_gui_smoke(seconds: float = 4.0) -> int:
    """界面冒烟：创建主窗口 → 进入事件循环 → 定时自动退出。

    用于验证打包后的 exe 能真正跑起 Qt 界面（仅创建窗口，不执行任何爬取）。
    """
    app = QApplication(sys.argv)
    window = CrawlerMainWindow()
    window.show()
    print(f"[OK]   主窗口已创建（{window.windowTitle()}），{seconds} 秒后自动退出")
    QTimer.singleShot(int(max(1.0, seconds) * 1000), app.quit)
    code = app.exec()
    print("[OK]   事件循环正常退出，GUI 可用")
    return 0 if code == 0 else code


def main() -> int:
    """启动应用，返回进程退出码。"""
    if "--selftest" in sys.argv:
        if "--gui" in sys.argv:
            return run_gui_smoke()
        return run_selftest()
    app = QApplication(sys.argv)
    window = CrawlerMainWindow()
    window.show()

    # Qt事件循环默认阻塞在select上，Python信号处理器无法执行；
    # 用定时器周期性唤醒主线程，使Ctrl+C（SIGINT）能可靠触发命令行停止
    sig_wake_timer = QTimer()
    sig_wake_timer.timeout.connect(lambda: None)
    sig_wake_timer.start(500)

    def _sigint_handler(sig, frame):
        """Ctrl+C命令行停止：停止任务、清理资源后安全退出"""
        window._force_close = True  # 跳过关闭确认
        window.request_stop_and_quit()

    signal.signal(signal.SIGINT, _sigint_handler)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
