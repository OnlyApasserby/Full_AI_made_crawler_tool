"""最小可运行示例：浏览器登录态复用模块（core/browser_session）。

演示完整链路（与模块 README 的「最小示例」一致）::

    启动带登录态的浏览器 → 检查登录态 → 导出 Cookie
        → 用 requests 复用同一会话访问页面

两种运行模式
------------
::

    # 模式一（默认）· 实时模式：复用本地浏览器 User Data 登录态
    #   前置条件：目标浏览器必须**完全退出**（Chromium 对同一 User Data 目录独占）
    python examples/browser_session_minimal.py --domain example.com \
        --browser edge --check-url https://example.com/

    # 模式二 · 纯 Cookie 模式：不启动浏览器，直接用导出的 Cookie 发请求
    #   适用：不想关浏览器 / 服务器上没有图形环境 / 只需要静态请求
    python examples/browser_session_minimal.py --domain example.com --cookie-only \
        --cookie-path resources/sessions/example.com.json \
        --check-url https://example.com/

可选参数
--------
``--export-only``   只导出 Cookie 后退出（拿到登录态留作它用）
``--cookie-path``   导出/加载的 Cookie 文件路径（默认 ``resources/sessions/<domain>.json``）
``--proxy``         代理地址，如 ``http://127.0.0.1:7890``（住宅代理优先）
``--selenium``      改用 Selenium 驱动（默认 Playwright）
``--headless``      无头模式（**不建议**：与复用 User Data 的登录态场景冲突）
``--check-url``     额外用 requests 访问一个页面，验证登录态真的生效

合规提示
--------
本示例只复用**使用者本人**已登录的会话，用于合法授权场景：
不实现验证码绕过、破解加密、伪造身份等功能。
请自行确认目标站点是否允许自动化访问；``--check-url`` 只做一次 GET 请求，
不会对目标站点造成压力。
"""

from __future__ import annotations

import argparse
import os
import sys

# 允许直接 `python examples/browser_session_minimal.py` 运行（把项目根加入路径）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.browser_session import (  # noqa: E402  （必须在 sys.path 调整之后导入）
    BrowserSessionError, SessionConfig, detect_installed_browsers,
    load_browser_session, session_from_cookies,
)

#: 默认导出路径所在的目录（与框架 config 的 SESSIONS_DIRNAME 一致）
DEFAULT_COOKIE_DIR = os.path.join(PROJECT_ROOT, "resources", "sessions")


def parse_args(argv=None) -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description="浏览器登录态复用示例（唯一用途：复用使用者本人的已登录会话）",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    parser.add_argument("--domain", required=True,
                        help="目标域名，如 example.com（Cookie 分组与登录态探测都用它）")
    parser.add_argument("--browser", default="chrome", choices=("chrome", "edge", "chromium"),
                        help="要复用哪个浏览器的 User Data（默认 chrome）")
    parser.add_argument("--profile", default="Default",
                        help="Profile 目录名，如 Default / 'Profile 1'（默认 Default）")
    parser.add_argument("--user-data-dir", default="",
                        help="显式指定 User Data 目录（默认自动探测平台标准位置）")
    parser.add_argument("--cookie-path", default="",
                        help="Cookie 文件路径（默认 resources/sessions/<domain>.json）")
    parser.add_argument("--cookie-only", action="store_true",
                        help="纯 Cookie 模式：不启动浏览器，只用已导出的 Cookie")
    parser.add_argument("--export-only", action="store_true",
                        help="只导出 Cookie 后退出")
    parser.add_argument("--selenium", action="store_true",
                        help="使用 Selenium 驱动（默认 Playwright 持久化上下文）")
    parser.add_argument("--headless", action="store_true",
                        help="无头模式（与复用 User Data 场景冲突，不建议）")
    parser.add_argument("--proxy", default="", help="代理地址（如 http://127.0.0.1:7890）")
    parser.add_argument("--check-url", default="",
                        help="额外用 requests 访问该 URL，验证登录态已生效")
    parser.add_argument("--login-url", default="",
                        help="登录态探测 URL（默认 https://<domain>/）")
    parser.add_argument("--logout-markers", default="",
                        help="逗号分隔的登出关键字，如 '请先登录,登录已过期'")
    parser.add_argument("--login-markers", default="",
                        help="逗号分隔的登录关键字，如 '退出登录,个人中心'")
    parser.add_argument("--keep-open", action="store_true",
                        help="结束后不关闭浏览器（便于手动登录一次后再导出）")
    return parser.parse_args(argv)


def build_config(args) -> SessionConfig:
    """把命令行参数组装成 :class:`SessionConfig`。"""
    domain = args.domain.strip()
    cookie_path = args.cookie_path or os.path.join(DEFAULT_COOKIE_DIR, f"{domain}.json")
    overrides = {
        "browser": args.browser,
        "profile": args.profile,
        "domain": domain,
        "cookie_store_path": cookie_path,
        "launcher": "selenium" if args.selenium else "playwright",
        "headless": bool(args.headless),
        # 复用本地 Profile 的真实指纹（反检测脚本只做一致性补全）
        "impersonate": True,
        # Cookie 落盘加密：设置 CRAWLER_SESSION_KEY 后自动启用
        "cookie_encrypt": bool(os.environ.get("CRAWLER_SESSION_KEY")),
    }
    if args.user_data_dir:
        overrides["user_data_dir"] = args.user_data_dir
    if args.proxy:
        overrides["proxy"] = {"https": args.proxy, "http": args.proxy}
    if args.login_url:
        overrides["login_url"] = args.login_url
    if args.logout_markers:
        overrides["logout_markers"] = [item.strip() for item in
                                       args.logout_markers.split(",") if item.strip()]
    if args.login_markers:
        overrides["login_markers"] = [item.strip() for item in
                                      args.login_markers.split(",") if item.strip()]
    return SessionConfig.resolve(overrides).finalize()


def step(index: int, text: str) -> None:
    """打印步骤标题（让示例输出可读）。"""
    print(f"\n[{index}] {text}")
    print("-" * 66)


def show_environment() -> None:
    """打印本机探测到的浏览器与 User Data 目录（只读）。"""
    detected = detect_installed_browsers()
    if not detected:
        print("未探测到 Chrome / Edge 的默认 User Data 目录。")
        print("可用 --user-data-dir 显式指定，或设置 "
              "CRAWLER_SESSION_USER_DATA_DIR 环境变量。")
        return
    print("本机探测到的浏览器 User Data 目录：")
    for kind, path in detected.items():
        print(f"  · {kind:<9} {path}")


def run_cookie_only(config: SessionConfig, args) -> int:
    """纯 Cookie 模式：不启动浏览器，直接用 Cookie 构造 requests 会话。"""
    step(1, "纯 Cookie 模式：载入已导出的登录态（不启动浏览器）")
    print(f"Cookie 文件：{config.cookie_store_path}")
    if not os.path.isfile(config.cookie_store_path):
        print("Cookie 文件不存在。请先运行一次实时模式导出：")
        print(f"  python examples/browser_session_minimal.py --domain {config.domain} "
              "--export-only")
        return 2

    handle = session_from_cookies(config)
    try:
        print(handle.cookie_summary())

        step(2, "检查登录态（http / cookie 通道）")
        result = handle.check_login_detail()
        print(result.describe())
        if not result.logged_in:
            print("\n登录态已失效，请按以下步骤重新登录并把新登录态导出：")
            print(handle.relogin_hint())

        step(3, "用 requests 复用该登录态访问页面")
        session = handle.get_requests_session()
        print(f"User-Agent：{session.headers.get('User-Agent', '')[:80]}")
        print(f"Cookie 数量：{len(session.cookies)}")
        if args.check_url:
            response = session.get(args.check_url, timeout=15)
            print(f"GET {args.check_url} → HTTP {response.status_code}"
                  f"（{len(response.content)} 字节）")
            print("已登录关键字是否出现在页面中：" +
                  ("是" if handle.last_check and handle.last_check.logged_in else "否"))
        else:
            print("（未指定 --check-url，跳过实际请求）")
        handle.close()
        return 0
    finally:
        handle.close()


def run_live(config: SessionConfig, args) -> int:
    """实时模式：复用本地 User Data 启动浏览器，导出 Cookie 并用 requests 复用。"""
    show_environment()

    step(1, "启动带登录态的浏览器（复用本地 User Data）")
    print(config.describe())
    print("\n提示：如果报「User Data 目录被占用」，请先**完全退出**该浏览器"
          "（含任务栏后台驻留进程），再重新运行。")
    handle = load_browser_session(config)
    try:
        driver = handle.get_driver()
        print(f"浏览器已启动（驱动：{driver.kind()}）")
        # 访问目标站点首页，让服务端会话生效
        driver.get(config.login_url)
        print(f"已访问：{config.login_url}")

        step(2, "检查登录态")
        result = handle.check_login_detail()
        print(result.describe())
        if result.logged_in:
            print("登录态有效，可以继续自动化流程。")
        else:
            print("\n未检测到有效登录态。请按以下步骤手动登录一次：")
            print(handle.relogin_hint())
            print("\n若浏览器窗口仍开着，可直接在其中完成登录，然后重新运行本脚本"
                  "（或加 --export-only）导出新的登录态。")
            if not args.keep_open:
                print("（未使用 --keep-open，示例将在退出时关闭该浏览器窗口）")

        step(3, "导出 Cookie 到本地（默认加密，需设置 CRAWLER_SESSION_KEY）")
        path = handle.export_cookies()
        print(f"已写入：{path}")
        print(handle.cookie_summary())
        if not config.cookie_encrypt:
            print("提示：未设置 CRAWLER_SESSION_KEY，本次以**明文**保存。")
            print("      Cookie 等同账号凭据，建议设置该环境变量后再导出：")
            print("      Windows:  set CRAWLER_SESSION_KEY=你的口令")
            print("      Linux/macOS:  export CRAWLER_SESSION_KEY=你的口令")

        if args.export_only:
            print("\n（--export-only：跳过 requests 复用步骤）")
            return 0

        step(4, "用 requests 复用同一登录态访问页面")
        session = handle.get_requests_session()
        print(f"User-Agent：{session.headers.get('User-Agent', '')[:80]}")
        print(f"Cookie 数量：{len(session.cookies)}")
        url = args.check_url or config.login_url
        response = session.get(url, timeout=15)
        print(f"GET {url} → HTTP {response.status_code}（{len(response.content)} 字节）")
        print("说明：状态码 200 且内容非登录页，即表示 requests 已复用浏览器登录态。")

        step(5, "把登录态交给框架抓取层（可选）")
        fetcher = handle.get_fetcher("static")
        context = fetcher.fetch(config.login_url)
        print(f"框架抓取器 fetch() → HTTP {context.status}，"
              f"内容 {len(context.html)} 字符，fetcher={context.fetcher}")
        fetcher.close()
        return 0
    finally:
        if args.keep_open:
            print("\n（--keep-open：保留浏览器窗口，请手动关闭）")
        else:
            handle.close()
            print("\n浏览器已关闭，会话资源已释放。")


def main(argv=None) -> int:
    """示例入口：按模式分发，统一处理可预期的错误。"""
    args = parse_args(argv)
    config = build_config(args)
    try:
        if args.cookie_only:
            return run_cookie_only(config, args)
        return run_live(config, args)
    except BrowserSessionError as exc:
        # 本模块的所有异常都带可操作提示，直接原样展示
        print(f"\n[失败] {type(exc).__name__}\n{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
