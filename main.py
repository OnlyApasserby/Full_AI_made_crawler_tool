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


def main() -> int:
    """启动应用，返回进程退出码。"""
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
