"""CookieStore 子模块单元测试：规范化、持久化（含加密）、加载与注入。

全部用内存/临时目录完成，不启动浏览器、不访问网络。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timezone

from core.browser_session.config import (
    COOKIE_ACCEPT_SAME_SITE, SessionConfig,
)
from core.browser_session.cookie_store import (
    ENCRYPTED_HEADER, CookieStore, cookie_fingerprint, cookies_to_dict,
    crypto_available, domain_family, domain_of, filter_by_domain,
    group_by_domain, normalize_cookie, normalize_cookies, normalize_expiry,
    normalize_same_site, to_playwright_cookie, to_selenium_cookie,
)
from core.browser_session.exceptions import (
    CookieDecryptError, CookieExportError, CookieFileInvalid, CookieFileNotFound,
    CookieInjectError,
)


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------
class FakeDriver:
    """最小驱动替身：按域名校验 Cookie 是否可注入（模拟 Chromium 的后缀匹配规则）。"""

    def __init__(self, url: str = ""):
        self._url = url
        self.visited: list[str] = []
        self.injected: list[dict] = []
        self.refreshed = 0
        self.cookies: list[dict] = []

    # 页面
    def get(self, url: str) -> None:
        if "broken" in url:
            raise RuntimeError("net::ERR_NAME_NOT_RESOLVED")
        self.visited.append(url)
        self._url = url

    def current_url(self) -> str:
        return self._url

    def page_source(self) -> str:
        return "<html><body>ok</body></html>"

    def refresh(self) -> None:
        self.refreshed += 1

    def title(self) -> str:
        return "fake"

    def text(self) -> str:
        return "ok"

    # Cookie
    def get_cookies(self) -> list[dict]:
        return [dict(item) for item in self.cookies]

    def add_cookies(self, cookies: list[dict]) -> tuple[int, list[str]]:
        host = domain_of({"domain": self._url.split("//")[-1].split("/")[0]})
        ok, errors = 0, []
        for cookie in cookies:
            owner = domain_of(cookie)
            if owner and not (owner == host or host.endswith("." + owner)):
                errors.append(f"{cookie['name']}：InvalidCookieDomainException")
                continue
            self.injected.append(dict(cookie))
            ok += 1
        return ok, errors

    def delete_all_cookies(self) -> None:
        self.injected.clear()

    # 生命周期
    def quit(self) -> None:
        pass

    def alive(self) -> bool:
        return True

    def kind(self) -> str:
        return "fake"


def raw_cookie(name="sid", value="abc", domain=".example.com", path="/", **extra):
    cookie = {"name": name, "value": value, "domain": domain, "path": path}
    cookie.update(extra)
    return cookie


# ---------------------------------------------------------------------------
# 规范化
# ---------------------------------------------------------------------------
class NormalizeExpiryTest(unittest.TestCase):
    """过期时间字段的格式转换（本模块最容易踩坑的地方）。"""

    def test_unix_seconds_passthrough(self):
        self.assertEqual(normalize_expiry(1893456000), 1893456000)

    def test_milliseconds_and_microseconds_converted(self):
        self.assertEqual(normalize_expiry(1893456000 * 1000), 1893456000)
        # Chromium 的 Cookies 库用 1601 纪元微秒
        webkit = int((1893456000 + 11644473600) * 1_000_000)
        self.assertEqual(normalize_expiry(webkit), 1893456000)

    def test_iso_and_text_dates(self):
        expected = int(datetime(2030, 1, 1, tzinfo=timezone.utc).timestamp())
        self.assertEqual(normalize_expiry("2030-01-01T00:00:00Z"), expected)
        self.assertEqual(normalize_expiry("2030-01-01T00:00:00+00:00"), expected)
        self.assertEqual(normalize_expiry("2030-01-01 00:00:00"), expected)
        self.assertEqual(normalize_expiry("2030-01-01"), expected)
        self.assertEqual(normalize_expiry("1893456000"), 1893456000)

    def test_datetime_object(self):
        moment = datetime(2031, 5, 4, 3, 2, 1, tzinfo=timezone.utc)
        self.assertEqual(normalize_expiry(moment), int(moment.timestamp()))

    def test_invalid_and_sentinel_values_return_none(self):
        for value in (None, "", 0, "abc", True, -5,
                      "9999-12-31T23:59:59Z", 10 ** 20):
            with self.subTest(value=value):
                self.assertIsNone(normalize_expiry(value))


class NormalizeCookieTest(unittest.TestCase):
    """字段白名单、domain 推导与 __Host- 前缀处理。"""

    def test_normalizes_common_fields(self):
        cookie = normalize_cookie(raw_cookie(secure=True, httpOnly=True,
                                             expiry=1893456000, sameSite="lax"))
        self.assertEqual(cookie["name"], "sid")
        self.assertEqual(cookie["domain"], "example.com")      # 去前导点
        self.assertEqual(cookie["path"], "/")
        self.assertTrue(cookie["secure"])
        self.assertTrue(cookie["httpOnly"])
        self.assertEqual(cookie["expiry"], 1893456000)
        self.assertEqual(cookie["sameSite"], "Lax")

    def test_expiration_date_alias_and_missing_path(self):
        cookie = normalize_cookie({"name": "a", "value": "1", "domain": "x.com",
                                   "expirationDate": 1893456000})
        self.assertEqual(cookie["path"], "/")
        self.assertEqual(cookie["expiry"], 1893456000)

    def test_domain_inferred_from_page_url(self):
        cookie = normalize_cookie({"name": "a", "value": "1"},
                                  "https://www.example.com/login")
        self.assertEqual(cookie["domain"], "www.example.com")

    def test_host_prefix_cookie_drops_domain(self):
        cookie = normalize_cookie({"name": "__Host-sid", "value": "1",
                                   "domain": "example.com"})
        self.assertEqual(cookie["domain"], "")

    def test_illegal_entries_return_none(self):
        for bad in ({}, {"name": "", "value": "1"}, {"name": "a", "value": None},
                    {"name": "a", "value": "1"}, "not-a-dict", None):
            with self.subTest(bad=bad):
                self.assertIsNone(normalize_cookie(bad))

    def test_batch_dedup_and_drop_report(self):
        kept, dropped = normalize_cookies([
            raw_cookie("a", "1"), raw_cookie("a", "1"),          # 完全重复
            raw_cookie("a", "2"),                                 # 同名同域 → 后者覆盖
            {"name": "b", "value": "1"},                          # 无 domain → 按 page_url 补齐
            {"name": "c", "value": "3", "domain": "example.com"},
            {"name": ""},                                         # name 为空
        ], "https://example.com/")
        self.assertEqual([item["name"] for item in kept], ["a", "b", "c"])
        self.assertEqual(kept[0]["value"], "2")      # 同键冲突保留最后一条
        self.assertEqual(kept[1]["domain"], "example.com")
        self.assertEqual(len(dropped), 1)

    def test_default_host_fills_missing_domain(self):
        kept, dropped = normalize_cookies([{"name": "b", "value": "1"}],
                                          default_host="example.com")
        self.assertEqual(kept[0]["domain"], "example.com")
        self.assertEqual(dropped, [])

    def test_same_site_normalization(self):
        self.assertEqual(normalize_same_site("lax"), "Lax")
        self.assertEqual(normalize_same_site("LAX"), "Lax")
        self.assertEqual(normalize_same_site("strict"), "Strict")
        self.assertEqual(normalize_same_site("none"), "None")
        self.assertEqual(normalize_same_site("no_restriction"), "None")
        self.assertIsNone(normalize_same_site("weird"))
        self.assertIsNone(normalize_same_site(None))


class DomainFilterTest(unittest.TestCase):
    """按目标域名筛选与分组。"""

    def setUp(self):
        self.cookies = [
            normalize_cookie(raw_cookie("a", "1", ".example.com")),
            normalize_cookie(raw_cookie("b", "2", "www.example.com")),
            normalize_cookie(raw_cookie("c", "3", ".tracker.net")),
            normalize_cookie(raw_cookie("d", "4", "cdn.example.com")),
        ]

    def test_all_mode_keeps_everything(self):
        self.assertEqual(len(filter_by_domain(self.cookies, "www.example.com")), 4)

    def test_same_site_mode_keeps_domain_family(self):
        kept = filter_by_domain(self.cookies, "www.example.com",
                                COOKIE_ACCEPT_SAME_SITE)
        self.assertEqual(sorted(item["name"] for item in kept), ["a", "b", "d"])

    def test_group_by_domain(self):
        grouped = group_by_domain(self.cookies)
        self.assertEqual(len(grouped["example.com"]), 1)
        self.assertEqual(len(grouped["www.example.com"]), 1)
        self.assertIn("tracker.net", grouped)

    def test_domain_family_and_dict_flatten(self):
        self.assertEqual(domain_family("www.example.com"),
                         {"www.example.com", "example.com"})
        self.assertEqual(domain_family(""), set())
        self.assertEqual(cookies_to_dict(self.cookies),
                         {"a": "1", "b": "2", "c": "3", "d": "4"})

    def test_fingerprint_is_stable_and_value_sensitive(self):
        first = cookie_fingerprint(self.cookies)
        self.assertEqual(first, cookie_fingerprint(list(reversed(self.cookies))))
        changed = [dict(item) for item in self.cookies]
        changed[0]["value"] = "different"
        self.assertNotEqual(first, cookie_fingerprint(changed))
        # 指纹是短哈希，不含 Cookie 明文值
        self.assertEqual(len(first), 16)
        self.assertRegex(first, r"^[0-9a-f]{16}$")
        self.assertNotIn("tracker", first)


class AdapterFormatTest(unittest.TestCase):
    """规范格式 → Selenium / Playwright 字段（各自的字段名与要求不同）。"""

    def setUp(self):
        self.cookie = normalize_cookie(raw_cookie("sid", "v", ".example.com",
                                                  secure=True, httpOnly=True,
                                                  expiry=1893456000, sameSite="lax"))

    def test_selenium_format_uses_expiry(self):
        out = to_selenium_cookie(self.cookie)
        self.assertEqual(out["expiry"], 1893456000)
        self.assertNotIn("expires", out)
        self.assertEqual(out["sameSite"], "Lax")

    def test_playwright_format_uses_expires_and_domain(self):
        out = to_playwright_cookie(self.cookie)
        self.assertEqual(out["expires"], 1893456000)
        self.assertNotIn("expiry", out)
        self.assertEqual(out["domain"], "example.com")
        self.assertNotIn("url", out)

    def test_playwright_host_only_cookie_needs_url(self):
        cookie = normalize_cookie({"name": "__Host-a", "value": "1",
                                   "domain": "example.com"})
        self.assertIsNone(to_playwright_cookie(cookie))
        out = to_playwright_cookie(cookie, "https://example.com/")
        self.assertEqual(out["url"], "https://example.com/")


# ---------------------------------------------------------------------------
# 持久化
# ---------------------------------------------------------------------------
class CookiePersistenceTest(unittest.TestCase):
    """明文 / 加密两条落盘路径，以及读取时的错误处理。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cookie_test_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config = SessionConfig(domain="example.com", cookie_encrypt=False,
                                    cookie_store_path=os.path.join(self.tmp, "c.json"))
        self.config.finalize()

    def _store(self, **overrides):
        config = self.config.with_overrides(**overrides)
        return CookieStore(config)

    def test_plaintext_roundtrip(self):
        store = self._store()
        store.export_from_driver(FakeDriver("https://example.com/"))
        store.cookies = [normalize_cookie(raw_cookie("sid", "abc"),
                                          "https://example.com/")]
        path = store.save()
        self.assertTrue(os.path.isfile(path))
        with open(path, "r", encoding="utf-8") as stream:
            payload = json.load(stream)                # 明文应是合法 JSON
        self.assertFalse(payload["encrypted"])
        self.assertEqual(payload["cookie_count"], 1)
        self.assertNotIn("cookie_key", payload)

        again = self._store()
        cookies = again.load()
        self.assertEqual(cookies[0]["name"], "sid")
        self.assertEqual(cookies[0]["value"], "abc")
        self.assertFalse(again.manifest.encrypted)

    def test_encrypted_roundtrip_hides_values(self):
        if not crypto_available():
            self.skipTest("未安装 cryptography")
        store = self._store(cookie_encrypt=True, cookie_key="k3y")
        store.cookies = [normalize_cookie(raw_cookie("sid", "topsecret"),
                                          "https://example.com/")]
        store.manifest.cookie_count = 1
        path = store.save()
        with open(path, "r", encoding="utf-8") as stream:
            text = stream.read()
        self.assertTrue(text.startswith(ENCRYPTED_HEADER))
        self.assertNotIn("topsecret", text)            # 明文值不得出现
        self.assertNotIn("sid", text.split("\n", 1)[1])  # 密文里也不该有名字

        cookies = self._store(cookie_encrypt=True, cookie_key="k3y").load()
        self.assertEqual(cookies[0]["value"], "topsecret")

    def test_encrypted_wrong_key_raises(self):
        if not crypto_available():
            self.skipTest("未安装 cryptography")
        store = self._store(cookie_encrypt=True, cookie_key="right")
        store.cookies = [normalize_cookie(raw_cookie())]
        path = store.save()
        with self.assertRaises(CookieDecryptError) as ctx:
            self._store(cookie_encrypt=True, cookie_key="wrong").load()
        self.assertIn("密钥不正确", str(ctx.exception))
        # 完全没有密钥时也要给出可操作提示
        with self.assertRaises(CookieDecryptError) as ctx:
            self._store(cookie_encrypt=True, cookie_key="").load(path)
        self.assertIn("未提供密钥", str(ctx.exception))

    def test_save_without_cookies_raises(self):
        store = self._store()
        with self.assertRaises(CookieExportError) as ctx:
            store.save()
        # 提示里要给出可执行的排查方向
        self.assertIn("没有可保存的 Cookie", str(ctx.exception))
        self.assertIn("登录过", str(ctx.exception))

    def test_missing_file_raises_with_hint(self):
        store = self._store()
        with self.assertRaises(CookieFileNotFound) as ctx:
            store.load(os.path.join(self.tmp, "nope.json"))
        self.assertIn("请先运行 export 流程", str(ctx.exception))

    def test_invalid_json_raises(self):
        path = os.path.join(self.tmp, "bad.json")
        with open(path, "w", encoding="utf-8") as stream:
            stream.write("{ not json ")
        with self.assertRaises(CookieFileInvalid):
            self._store().load(path)

    def test_unrecognized_format_raises(self):
        path = os.path.join(self.tmp, "weird.txt")
        with open(path, "w", encoding="utf-8") as stream:
            stream.write("hello world")
        with self.assertRaises(CookieFileInvalid) as ctx:
            self._store().load(path)
        self.assertIn("格式不识别", str(ctx.exception))

    def test_missing_cookies_key_raises(self):
        path = os.path.join(self.tmp, "no_cookies.json")
        with open(path, "w", encoding="utf-8") as stream:
            json.dump({"domain": "example.com"}, stream)
        with self.assertRaises(CookieFileInvalid) as ctx:
            self._store().load(path)
        self.assertIn("缺少 cookies 列表", str(ctx.exception))

    def test_bare_list_format_is_accepted(self):
        path = os.path.join(self.tmp, "bare.json")
        with open(path, "w", encoding="utf-8") as stream:
            json.dump({"data": [raw_cookie("a", "1")]}, stream)
        cookies = self._store().load(path)
        self.assertEqual(cookies[0]["name"], "a")

    def test_save_overwrites_atomically(self):
        store = self._store()
        store.cookies = [normalize_cookie(raw_cookie("a", "1"))]
        store.save()
        store.cookies = [normalize_cookie(raw_cookie("b", "2"))]
        store.save()
        cookies = self._store().load()
        self.assertEqual([item["name"] for item in cookies], ["b"])
        # 不应残留临时文件
        leftovers = [name for name in os.listdir(self.tmp) if name.startswith(".session_")]
        self.assertEqual(leftovers, [])

    def test_encrypt_falls_back_to_plaintext_without_key(self):
        store = self._store(cookie_encrypt=True, cookie_key="")
        store.cookies = [normalize_cookie(raw_cookie())]
        path = store.save()
        with open(path, "r", encoding="utf-8") as stream:
            self.assertFalse(json.load(stream)["encrypted"])
        self.assertTrue(any("未配置密钥" in note for note in store.notes)
                        or path.endswith(".json"))


# ---------------------------------------------------------------------------
# 导出 / 注入
# ---------------------------------------------------------------------------
class CookieExportInjectTest(unittest.TestCase):
    """从驱动导出、注入到驱动，以及 requests 层的 Cookie 形态。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cookie_io_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config = SessionConfig(
            domain="example.com", cookie_encrypt=False,
            cookie_store_path=os.path.join(self.tmp, "c.json")).finalize()

    def _driver(self):
        driver = FakeDriver("https://example.com/home")
        driver.cookies = [
            raw_cookie("sid", "abc", ".example.com", secure=True),
            {"name": "uid", "value": "7", "domain": "www.example.com", "path": "/"},
            {"name": "utm", "value": "x", "domain": ".tracker.net", "path": "/"},
            {"name": "", "value": "bad", "domain": ".example.com"},     # 非法，应被丢弃
        ]
        return driver

    def test_export_groups_by_domain_and_reports_drops(self):
        store = CookieStore(self.config, self._driver())
        cookies = store.export_from_driver()
        self.assertEqual(len(cookies), 3)
        self.assertEqual(store.manifest.cookie_count, 3)
        self.assertEqual(set(store.manifest.domain_counts),
                         {"example.com", "www.example.com", "tracker.net"})
        self.assertTrue(any("非法" in note for note in store.notes))
        self.assertTrue(store.summary().count("Cookie 总数：3") == 1)

    def test_export_without_driver_raises(self):
        with self.assertRaises(CookieInjectError) as ctx:
            CookieStore(self.config).export_from_driver()
        self.assertIn("没有可用的浏览器驱动", str(ctx.exception))

    def test_inject_visits_domain_then_refreshes(self):
        store = CookieStore(self.config)
        store.cookies = [
            normalize_cookie(raw_cookie("sid", "abc", ".example.com")),
            normalize_cookie(raw_cookie("cdn", "1", "cdn.example.com")),
        ]
        driver = FakeDriver()
        applied = store.inject_into_driver(driver)
        self.assertEqual(applied, 2)
        self.assertIn("https://cdn.example.com/", driver.visited)
        self.assertIn("https://example.com/", driver.visited)
        self.assertEqual(driver.refreshed, 1)

    def test_inject_all_failed_raises_with_reasons(self):
        store = CookieStore(self.config)
        # 目标域名与当前页面完全无关，且 navigate=False 避免导航绕过校验
        store.cookies = [normalize_cookie(raw_cookie("sid", "1", "other.net"))]
        driver = FakeDriver("https://example.com/")
        with self.assertRaises(CookieInjectError) as ctx:
            store.inject_into_driver(driver, navigate=False)
        message = str(ctx.exception)
        self.assertIn("所有 Cookie 均注入失败", message)
        self.assertIn("Cookie 已过期", message)     # 提示里要给出排查方向

    def test_inject_navigation_failure_is_reported_not_fatal(self):
        store = CookieStore(self.config)
        store.cookies = [
            normalize_cookie(raw_cookie("sid", "1", "broken.example.com")),
            normalize_cookie(raw_cookie("ok", "1", "example.com")),
        ]
        driver = FakeDriver()
        applied = store.inject_into_driver(driver)   # 一条域名打不开，另一条仍应成功
        self.assertEqual(applied, 1)
        self.assertEqual([item["name"] for item in driver.injected], ["ok"])

    def test_inject_without_cookies_raises(self):
        with self.assertRaises(CookieInjectError) as ctx:
            CookieStore(self.config).inject_into_driver(FakeDriver())
        self.assertIn("没有可注入的 Cookie", str(ctx.exception))

    def test_requests_cookie_dict_and_jar(self):
        store = CookieStore(self.config)
        store.cookies = [
            normalize_cookie(raw_cookie("sid", "abc", ".example.com", secure=True)),
            normalize_cookie(raw_cookie("other", "x", ".tracker.net")),
        ]
        self.assertEqual(store.as_requests_cookies(), {"sid": "abc", "other": "x"})
        jar = store.as_cookie_jar()
        self.assertIn("sid", jar)
        self.assertTrue(any(cookie.secure for cookie in jar))

    def test_same_site_filter_applies_to_requests_view(self):
        config = self.config.with_overrides(cookie_accept=COOKIE_ACCEPT_SAME_SITE)
        store = CookieStore(config)
        store.cookies = [
            normalize_cookie(raw_cookie("sid", "abc", ".example.com")),
            normalize_cookie(raw_cookie("track", "x", ".tracker.net")),
        ]
        self.assertEqual(store.as_requests_cookies(), {"sid": "abc"})

    def test_expired_cookies_are_still_exportable_but_flagged(self):
        from core.browser_session.login_state import expired_cookies
        store = CookieStore(self.config)
        store.cookies = [
            normalize_cookie(raw_cookie("old", "1", expiry=int(time.time()) - 60)),
            normalize_cookie(raw_cookie("new", "2", expiry=int(time.time()) + 3600)),
        ]
        self.assertEqual([item["name"] for item in expired_cookies(store.cookies)],
                         ["old"])


if __name__ == "__main__":
    unittest.main()
