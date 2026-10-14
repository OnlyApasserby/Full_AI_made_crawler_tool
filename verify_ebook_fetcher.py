"""ebook_fetcher 的本地端到端验证脚本（临时测试站点，跑完自动清理）。

用法（项目根目录）：
    python verify_ebook_fetcher.py

说明：沙箱内 Playwright 无法启动浏览器驱动（子进程管道被拒），
因此 dynamic 分支用"假抓取器"验证参数路由，真实渲染留待有浏览器的环境验证。
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import core.ebook_fetcher as ef

ef.logger.setLevel("ERROR")   # 只保留失败日志，避免用例输出被进度日志淹没

PAGES = {
    "/robots.txt": "User-agent: *\nDisallow: /blocked/\n",
    "/blocked/1.html": "<html><body><div id='content'>秘密</div></body></html>",
    "/book/1.html": """<html><head><title>第一章 初入江湖_某书_示例站</title></head><body>
      <nav class="nav">导航栏噪声</nav>
      <div class="ad">广告位：点我</div>
      <h1>第一章 初入江湖</h1>
      <div id="content">
        <p>山风拂面，少年负剑而行。</p>
        <p>他不知道自己即将改变这个世界。</p>
      </div>
      <div class="chapterlist"><a href="/book/99.html">目录噪声</a></div>
      <a id="next" href="2.html">下一页</a>
      </body></html>""",
    "/book/2.html": """<html><head><title>第二章 入城_某书_示例站</title></head><body>
      <h1>第二章 入城</h1>
      <div id="content">
        <p>城门高耸，人声鼎沸。</p>
      </div>
      <div class="ad">广告位：点我</div>
      <a href="/book/3.html">下一章</a>
      </body></html>""",
    "/book/3.html": """<html><head><title>第三章 收徒_某书_示例站</title></head><body>
      <h1>第三章 收徒</h1>
      <div class="content">他收下了第一个徒弟。</div>
      <span>没有下一页了</span>
      </body></html>""",
    # 死循环站点：1 → 2 → 1 → 2 …
    "/loop/1.html": """<html><body><div id="content">循环正文一</div>
      <a href="/loop/2.html">下一页</a></body></html>""",
    "/loop/2.html": """<html><body><div id="content">循环正文二</div>
      <a href="/loop/1.html">下一页</a></body></html>""",
    # JS 渲染站点：正文由脚本注入（静态请求拿不到）
    "/js/1.html": """<html><body>
      <div id="content"></div>
      <script>
        document.getElementById('content').innerHTML = '<p>渲染出来的正文内容</p>';
      </script>
      </body></html>""",
    # 无"下一页"链接，但页面列出了第 2 页链接 → 验证 URL 规律推断
    "/guess/1.html": """<html><body>
      <div id="content">第一章正文</div>
      <div class="chapterlist">
        <a href="/guess/2.html">第二章</a>
        <a href="/guess/3.html">第三章</a>
      </div>
      </body></html>""",
    "/guess/2.html": """<html><body>
      <div id="content">第二章正文</div>
      <div class="chapterlist"><a href="/guess/3.html">第三章</a></div>
      </body></html>""",
    "/guess/3.html": """<html><body>
      <div id="content">第三章正文（末页）</div>
      </body></html>""",
}

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"{'PASS' if condition else 'FAIL'} | {name}" + (f" | {detail}" if detail else ""))
    if not condition:
        failures.append(f"{name} :: {detail}")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server 约定
        body = PAGES.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"not found")
            return
        payload = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # 静音
        return


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    workdir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "output", "_ebook_verify")
    shutil.rmtree(workdir, ignore_errors=True)
    os.makedirs(workdir, exist_ok=True)
    print(f"临时站点：{base} | 输出目录：{workdir}\n")

    real_create = ef.create_fetcher

    try:
        # ---------- 用例 1：三页静态抓取 + 清洗 + 元数据 ----------
        out1 = os.path.join(workdir, "book.txt")
        saved = ef.fetch_book(f"{base}/book/1.html", out1, delay=(0, 0),
                              max_pages=1000)
        check("返回值为输出路径", saved == out1, saved)
        text = open(out1, encoding="utf-8").read()
        check("包含书名标题行", text.startswith("《第一章 初入江湖》"), text[:30])
        check("正文内容完整（三章）",
              all(s in text for s in ("山风拂面", "城门高耸", "他收下了第一个徒弟")))
        check("章节标题写入", "第二章 入城" in text and "第三章 收徒" in text)
        check("排除导航噪声", "导航栏噪声" not in text)
        check("排除广告噪声", "广告位" not in text)
        check("排除目录噪声", "目录噪声" not in text)
        check("无 HTML 残渣", "<" not in text.replace("《", "").replace("》", ""))
        meta = json.load(open(os.path.splitext(out1)[0] + ".meta.json", encoding="utf-8"))
        check("元数据：书名", meta["book_title"] == "第一章 初入江湖", meta.get("book_title"))
        check("元数据：页数=3", meta["page_count"] == 3, str(meta.get("page_count")))
        check("元数据：章节列表", len(meta["chapters"]) == 3)
        check("元数据：抓取时间", bool(meta["fetch_time"]))
        check("元数据：结束原因", meta["stop_reason"].startswith("未找到下一页"),
              meta.get("stop_reason"))
        check("元数据：逐页明细", len(meta["pages"]) == 3 and meta["failed_pages"] == 0)
        check("章节不重复堆叠", text.count("第二章 入城") == 1,
              str(text.count("第二章 入城")))

        # ---------- 用例 2：max_pages 上限 ----------
        out2 = os.path.join(workdir, "limited.txt")
        ef.fetch_book(f"{base}/book/1.html", out2, delay=(0, 0), max_pages=2)
        meta2 = json.load(open(os.path.splitext(out2)[0] + ".meta.json", encoding="utf-8"))
        check("max_pages 生效", meta2["page_count"] == 2, str(meta2["page_count"]))
        check("max_pages 停止原因", "页数上限" in meta2["stop_reason"], meta2["stop_reason"])

        # ---------- 用例 3：死循环去重 ----------
        out3 = os.path.join(workdir, "loop.txt")
        ef.fetch_book(f"{base}/loop/1.html", out3, delay=(0, 0))
        meta3 = json.load(open(os.path.splitext(out3)[0] + ".meta.json", encoding="utf-8"))
        check("死循环被 set 去重打断", meta3["page_count"] == 2, str(meta3["page_count"]))
        check("死循环停止原因", "已访问过" in meta3["stop_reason"], meta3["stop_reason"])

        # ---------- 用例 4：正则降级（模拟未安装 bs4） ----------
        out4 = os.path.join(workdir, "regex.txt")
        original_flag, original_soup = ef.HAS_BS4, ef.BeautifulSoup
        ef.HAS_BS4, ef.BeautifulSoup = False, None
        try:
            ef.fetch_book(f"{base}/book/1.html", out4, delay=(0, 0))
        finally:
            ef.HAS_BS4, ef.BeautifulSoup = original_flag, original_soup
        text4 = open(out4, encoding="utf-8").read()
        check("降级正则仍取到正文", "山风拂面" in text4 and "城门高耸" in text4, text4[:60])
        check("降级正则排除噪声", "导航栏噪声" not in text4 and "广告位" not in text4)

        # ---------- 用例 5：robots.txt 遵守 ----------
        blocked = False
        detail = "未抛异常"
        try:
            ef.fetch_book(f"{base}/blocked/1.html",
                          os.path.join(workdir, "blocked.txt"), delay=(0, 0))
        except ef.EbookFetchError as exc:
            blocked = True
            detail = str(exc)
        check("robots 禁止路径被拦截", blocked, detail)
        check("robots 拦截时给出准确原因", "robots" in detail, detail)
        check("robots 禁止时不落盘空文件",
              not os.path.exists(os.path.join(workdir, "blocked.txt")))
        out5 = os.path.join(workdir, "norobots.txt")
        ef.fetch_book(f"{base}/blocked/1.html", out5, delay=(0, 0), respect_robots=False)
        check("respect_robots=False 可抓取",
              "秘密" in open(out5, encoding="utf-8").read())

        # ---------- 用例 6：动态模式路由（假抓取器） ----------
        record: list = []

        class FakeDynamicFetcher:
            """把动态模式请求转成静态请求，并模拟 JS 注入后的 DOM。"""

            def __init__(self, **kwargs):
                self._inner = ef.create_fetcher("static", **kwargs)

            def fetch(self, url, *, wait_selector="", scroll=False, max_scroll=8,
                      timeout=None, referer=""):
                record.append({"url": url, "wait_selector": wait_selector,
                               "scroll": scroll})
                ctx = self._inner.fetch(url, timeout=timeout)
                if not ctx.error:
                    # 模拟浏览器执行脚本后注入的正文
                    ctx.html = ("<html><head><title>动态第一章</title></head><body>"
                                "<div id='content'><p>渲染出来的正文内容</p></div>"
                                "</body></html>")
                return ctx

            def close(self):
                self._inner.close()

        def fake_create(mode="static", **kwargs):
            if mode == "dynamic":
                return FakeDynamicFetcher(**kwargs)
            return real_create(mode, **kwargs)

        ef.create_fetcher = fake_create
        try:
            out6 = os.path.join(workdir, "dynamic.txt")
            ef.fetch_book(f"{base}/js/1.html", out6, delay=(0, 0), dynamic=True,
                          wait_selector="#content", scroll=True)
        finally:
            ef.create_fetcher = real_create
        text6 = open(out6, encoding="utf-8").read()
        check("动态模式取到 JS 注入正文", "渲染出来的正文内容" in text6, text6[:60])
        page_calls = [item for item in record if not item["url"].endswith("robots.txt")]
        check("动态模式路由到 dynamic 抓取器", bool(page_calls), str(record[:2]))
        check("wait_selector 透传",
              bool(page_calls) and page_calls[0]["wait_selector"] == "#content",
              str(page_calls[:1]))
        check("scroll 透传", bool(page_calls) and page_calls[0]["scroll"] is True)

        # ---------- 用例 7：渲染兜底（静态正文为空 → 自动升级渲染） ----------
        fallback_record: list = []

        class FakeFallbackFetcher:
            def __init__(self, **kwargs):
                self._inner = ef.create_fetcher("static", **kwargs)

            def fetch(self, url, *, wait_selector="", scroll=False, max_scroll=8,
                      timeout=None, referer=""):
                fallback_record.append(url)
                ctx = self._inner.fetch(url, timeout=timeout)
                ctx.html = ("<html><body><div id='content'><p>兜底渲染出来的正文</p>"
                            "</div></body></html>")
                return ctx

            def close(self):
                self._inner.close()

        def fake_create2(mode="static", **kwargs):
            if mode == "dynamic":
                return FakeFallbackFetcher(**kwargs)
            return real_create(mode, **kwargs)

        ef.create_fetcher = fake_create2
        try:
            out7 = os.path.join(workdir, "fallback.txt")
            ef.fetch_book(f"{base}/js/1.html", out7, delay=(0, 0),
                          render_fallback=True)
        finally:
            ef.create_fetcher = real_create
        text7 = open(out7, encoding="utf-8").read()
        check("渲染兜底生效并取到正文", "兜底渲染出来的正文" in text7, text7[:60])
        check("兜底只对失败页触发一次", len(fallback_record) == 1,
              str(fallback_record))

        # ---------- 用例 7b：URL 页码规律推断（页面无"下一页"链接但有第 2 页链接） ----------
        out7b = os.path.join(workdir, "guess.txt")
        ef.fetch_book(f"{base}/guess/1.html", out7b, delay=(0, 0))
        meta7b = json.load(open(os.path.splitext(out7b)[0] + ".meta.json",
                                encoding="utf-8"))
        text7b = open(out7b, encoding="utf-8").read()
        check("按 URL 规律推断并翻到第 2、3 页", meta7b["page_count"] == 3,
              str(meta7b["page_count"]))
        check("推断翻页取到后续正文",
              "第二章正文" in text7b and "第三章正文" in text7b)
        check("推断结果进入元数据", meta7b["pages"][0]["next_url"].endswith("/guess/2.html"),
              str(meta7b["pages"][0]["next_url"]))

        # ---------- 用例 7c：整页抓取失败 → 收尾阶段渲染二次尝试补回 ----------
        refetch_calls: list = []

        class FlakyStatic:
            """主循环里对 /book/2.html 一律返回 500（模拟反爬/限流）。"""

            def __init__(self, real_create=real_create, **kwargs):
                self._inner = real_create("static", **kwargs)

            def fetch(self, url, *, timeout=None, **kwargs):
                refetch_calls.append(("static", url))
                if url.endswith("/book/2.html"):
                    ctx = ef.FetchContext(url=url, status=500, fetcher="fake")
                    ctx.error = "HTTP 500"
                    return ctx
                return self._inner.fetch(url, timeout=timeout)

            def close(self):
                self._inner.close()

        class FlakyDynamic:
            """渲染路径首次也失败，第二次（收尾阶段）才成功 —— 验证"二次尝试补回"。"""

            def __init__(self, real_create=real_create, **kwargs):
                self._inner = real_create("static", **kwargs)
                self._calls = 0

            def fetch(self, url, *, timeout=None, **kwargs):
                refetch_calls.append(("dynamic", url))
                if url.endswith("/book/2.html"):
                    self._calls += 1
                    if self._calls == 1:
                        ctx = ef.FetchContext(url=url, status=503, fetcher="fake")
                        ctx.error = "HTTP 503"
                        return ctx
                    ctx = ef.FetchContext(url=url, status=200, fetcher="fake")
                    ctx.html = ("<html><head><title>第二章 补回</title></head><body>"
                                "<div id='content'><p>二次尝试补回的正文</p></div>"
                                "<a id='next' href='3.html'>下一页</a></body></html>")
                    return ctx
                return self._inner.fetch(url, timeout=timeout)

            def close(self):
                self._inner.close()

        def fake_create3(mode="static", **kwargs):
            return FlakyDynamic(**kwargs) if mode == "dynamic" \
                else FlakyStatic(**kwargs)

        ef.create_fetcher = fake_create3
        try:
            out7c = os.path.join(workdir, "refetch.txt")
            # retries=1：主循环静态尝试彻底失败 → 收尾二次尝试先遇 503、
            # 重试一次成功补回，用于验证"失败页不中断，事后补回"
            ef.fetch_book(f"{base}/book/1.html", out7c, delay=(0, 0), retries=1,
                          backoff=0.1, refetch_failed_pages=True,
                          render_fallback=False)
        finally:
            ef.create_fetcher = real_create
        text7c = open(out7c, encoding="utf-8").read()
        meta7c = json.load(open(os.path.splitext(out7c)[0] + ".meta.json",
                                encoding="utf-8"))
        check("失败页二次尝试补回正文", "二次尝试补回的正文" in text7c, text7c[:120])
        check("二次尝试后页数仍按页序计", meta7c["page_count"] == 3,
              str(meta7c["page_count"]))
        check("二次尝试后失败页清零", meta7c["failed_pages"] == 0,
              str(meta7c["failed_pages"]))
        check("二次尝试走了渲染路径",
              any(kind == "dynamic" for kind, _ in refetch_calls), str(refetch_calls))
        check("补回顺序正确（第一、二、三章依次）",
              text7c.index("山风拂面") < text7c.index("二次尝试补回的正文")
              < text7c.index("他收下了第一个徒弟"))

        # ---------- 用例 8：错误路径 ----------
        bad_url = False
        try:
            ef.fetch_book("ftp://x/y", os.path.join(workdir, "x.txt"))
        except ef.EbookFetchError:
            bad_url = True
        check("非法 URL 抛 EbookFetchError", bad_url)

        empty_path = False
        try:
            ef.fetch_book(f"{base}/book/1.html", "")
        except ef.EbookFetchError:
            empty_path = True
        check("空输出路径抛 EbookFetchError", empty_path)

        # ---------- 用例 9：工具函数 ----------
        check("normalize_delay 交换乱序区间", ef.normalize_delay((1.5, 0.5)) == (0.5, 1.5))
        check("normalize_delay 单值", ef.normalize_delay(0.8) == (0.8, 0.8))
        check("normalize_delay None", ef.normalize_delay(None) == (0.0, 0.0))
        check("clean_text 压空行", ef.clean_text("a\n\n\n\nb") == "a\n\nb")
        check("clean_text 去零宽字符", ef.clean_text("a\u200bb") == "a b")
    finally:
        ef.create_fetcher = real_create
        server.shutdown()
        server.server_close()
        shutil.rmtree(workdir, ignore_errors=True)

    print("\n================ 结果 ================")
    if failures:
        print(f"失败 {len(failures)} 项：")
        for item in failures:
            print(" -", item)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
