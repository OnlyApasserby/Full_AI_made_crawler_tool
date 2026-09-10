"""广告链接与钓鱼风险识别，以及屏蔽正则生成。

纯逻辑模块（不依赖 Qt / 网络），供安全检测对话框与脚本复用：

1. 广告识别
   - 追踪参数：utm_*、gclid、fbclid、adid、banner、click、promo、aff、ref 等
   - 广告联盟域名：doubleclick、googlesyndication、adservice、adnxs、taboola、outbrain 等
   - 广告路径：/ad/、/ads/、/advert/、/banner/、/popup/ 等

2. 钓鱼识别
   - 品牌仿冒域名（paypa1.com、g00gle.com、taobao.net.cn 等）
   - IP 直连 + 非常规端口
   - 敏感词（login/verify/account/secure/update/confirm…）且域名可疑
   - 短链接（bit.ly、tinyurl、t.cn 等）
   - 异常 TLD（.tk .ml .ga .cf .gq）、大量连字符 / 随机串

3. 正则生成
   把识别结果归纳为“覆盖同类模式”的通用正则（而非仅匹配单条 URL），
   可直接通过 URLFilter.add_block_pattern() 加入屏蔽列表。
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urlparse

# ---------------------------------------------------------------------------
# 风险等级
# ---------------------------------------------------------------------------
SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2}

CLASS_AD = "ad"
CLASS_PHISHING = "phishing"

# ---------------------------------------------------------------------------
# 广告特征
# ---------------------------------------------------------------------------
# 高置信广告/追踪参数（命中即广告，生成通用正则）
STRONG_AD_PARAMS = {
    "gclid", "dclid", "fbclid", "msclkid", "gclsrc", "wbraid", "gbraid",
    "yclid", "adid", "ad_id", "adset_id", "adsetid", "adgroup_id", "adgroupid",
    "adserver", "banner_id", "bannerid", "click_id", "clickid", "clicid",
    "promo_id", "promoid", "aff_id", "affid", "affiliate_id", "campaign_id",
    "campaignid", "creative_id", "creativeid", "_openstat", "spm",
}
# 通用广告参数（单独出现仅低风险提示，避免误伤普通站点的 ?from=/?ref=）
WEAK_AD_PARAMS = {
    "banner", "click", "promo", "promotion", "aff", "affiliate",
    "ref", "referrer", "referral", "ad", "ads", "advert", "source", "from",
}
# 广告联盟 / 追踪域名（按完整域名后缀匹配）
AD_DOMAINS = {
    "doubleclick.net", "googlesyndication.com", "googleadservices.com",
    "googletagservices.com", "googletagmanager.com", "adservice.google.com",
    "adnxs.com", "taboola.com", "outbrain.com", "criteo.com", "criteo.net",
    "adsrvr.org", "pubmatic.com", "rubiconproject.com", "casalemedia.com",
    "sharethrough.com", "smartadserver.com", "2mdn.net", "moatads.com",
    "adform.net", "zedo.com", "advertising.com", "exelator.com", "demdex.net",
    "everesttech.net", "mathtag.com", "agkn.com", "rlcdn.com", "crwdcntrl.net",
    "gumgum.com", "sovrn.com", "lijit.com", "indexexchange.com",
    "spotxchange.com", "innovid.com", "flashtalking.com", "serving-sys.com",
    "sizmek.com", "adcolony.com", "applovin.com", "mopub.com", "inmobi.com",
    "chartboost.com", "vungle.com", "tapjoy.com",
}
# 广告路径（/ad/、/ads/、/advert/、/banner/、/popup/ 等）
AD_PATH_RE = re.compile(
    r"/(?:ads?|advert|advertising|banners?|popups?|sponsor(?:ed)?|promo(?:tion)?)(?:/|$)",
    re.IGNORECASE)

# ---------------------------------------------------------------------------
# 钓鱼特征
# ---------------------------------------------------------------------------
# 短链接服务
SHORTENER_DOMAINS = {
    "bit.ly", "tinyurl.com", "t.cn", "goo.gl", "ow.ly", "is.gd", "buff.ly",
    "rebrand.ly", "cutt.ly", "rb.gy", "tiny.cc", "t.ly", "dwz.cn", "url.cn",
    "suo.im", "shorturl.at", "s.id", "u.to",
}
# 高滥用免费 TLD
SUSPICIOUS_TLDS = {"tk", "ml", "ga", "cf", "gq"}
# 登录/账户类敏感词
SENSITIVE_KEYWORDS = {
    "login", "signin", "sign-in", "verify", "verification", "account",
    "secure", "security", "update", "confirm", "password", "passwd",
    "wallet", "banking", "unlock", "recovery", "billing", "invoice", "auth",
}

# 品牌 -> 官方域名（用于仿冒识别；可按需扩充）
OFFICIAL_SITES = {
    "paypal": {"paypal.com", "paypal.me"},
    "google": {"google.com", "google.com.hk", "google.cn", "gmail.com",
               "googleapis.com", "youtube.com"},
    "taobao": {"taobao.com", "tmall.com"},
    "alipay": {"alipay.com"},
    "alibaba": {"alibaba.com", "1688.com"},
    "microsoft": {"microsoft.com", "live.com", "outlook.com", "office.com"},
    "apple": {"apple.com", "icloud.com"},
    "amazon": {"amazon.com", "amazon.cn", "aws.amazon.com"},
    "facebook": {"facebook.com", "fb.com"},
    "instagram": {"instagram.com"},
    "whatsapp": {"whatsapp.com"},
    "wechat": {"wechat.com", "weixin.qq.com", "qq.com"},
    "weibo": {"weibo.com"},
    "jd": {"jd.com"},
    "netflix": {"netflix.com"},
    "linkedin": {"linkedin.com"},
    "twitter": {"twitter.com", "x.com"},
    "telegram": {"telegram.org", "t.me"},
    "binance": {"binance.com"},
    "coinbase": {"coinbase.com"},
    "yahoo": {"yahoo.com"},
    "bing": {"bing.com"},
    "github": {"github.com"},
}
# 视为“品牌官方后缀”的常见注册域后缀（用于放行 paypal.co.uk 这类官方域）
OFFICIAL_TLD_SUFFIXES = {
    "com", "net", "org", "cn", "com.cn", "co.uk", "com.hk", "com.tw",
    "co.jp", "co.kr", "de", "fr", "jp", "io",
}
# 同形字符归一化（1→l、0→o、5→s …）
_HOMOGLYPH_TABLE = str.maketrans({
    "1": "l", "0": "o", "5": "s", "3": "e", "4": "a", "7": "t", "8": "b",
    "9": "g", "@": "a", "$": "s", "!": "i", "|": "l",
})
# 同形字符替换表（用于生成覆盖同类变体的正则）
_CONFUSABLE_CLASSES = {
    "o": "[o0]", "l": "[l1]", "i": "[il1]", "s": "[s5]", "e": "[e3]",
    "a": "[a4@]", "t": "[t7]", "b": "[b8]", "g": "[g9]",
}

# ---------------------------------------------------------------------------
# 通用屏蔽正则（覆盖同类模式）
# ---------------------------------------------------------------------------
REGEX_AD_PARAM = (
    r"(?i)[?&](?:utm_[a-z0-9_]*|"
    r"gclid|dclid|fbclid|msclkid|gclsrc|wbraid|gbraid|yclid|"
    r"adid|ad_id|adset_id|adsetid|adgroup_id|adgroupid|adserver|"
    r"banner_id|bannerid|click_id|clickid|clicid|promo_id|promoid|"
    r"aff_id|affid|affiliate_id|campaign_id|campaignid|"
    r"creative_id|creativeid|_openstat|spm)="
)
REGEX_AD_DOMAIN = (
    r"(?i)//(?:[^/]*\.)?(?:"
    r"doubleclick|googlesyndication|googleadservices|googletagservices|"
    r"googletagmanager|adservice|adnxs|taboola|outbrain|criteo|adsrvr|"
    r"pubmatic|rubiconproject|casalemedia|sharethrough|smartadserver|"
    r"2mdn|moatads|adform|zedo|advertising|exelator|demdex|everesttech|"
    r"mathtag|agkn|rlcdn|crwdcntrl|gumgum|sovrn|lijit|indexexchange|"
    r"spotxchange|innovid|flashtalking|serving-sys|sizmek|adcolony|"
    r"applovin|mopub|inmobi|chartboost|vungle|tapjoy)"
    r"\.(?:com|net|org|io|tv|cn|co)"
)
REGEX_AD_PATH = (
    r"(?i)/(?:ads?|advert|advertising|banners?|popups?|sponsor(?:ed)?|"
    r"promo(?:tion)?)(?:/|$)"
)
REGEX_SHORTENER = (
    r"(?i)//(?:[^/]*\.)?(?:"
    r"bit\.ly|tinyurl\.com|t\.cn|goo\.gl|ow\.ly|is\.gd|buff\.ly|rebrand\.ly|"
    r"cutt\.ly|rb\.gy|tiny\.cc|t\.ly|dwz\.cn|url\.cn|suo\.im|shorturl\.at|"
    r"s\.id|u\.to)(?:[/:?#]|$)"
)
REGEX_BAD_TLD = r"(?i)\.(?:tk|ml|ga|cf|gq)(?:[/:?#]|$)"
REGEX_IP_PORT = (
    r"(?i)^https?://\d{1,3}(?:\.\d{1,3}){3}:"
    r"(?!80(?:[/:?#]|$)|443(?:[/:?#]|$))\d+(?:[/:?#]|$)"
)
REGEX_IP_HOST = r"(?i)^https?://\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?(?:[/:?#]|$)"


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class Finding:
    """单条识别结果。"""
    kind: str            # CLASS_AD / CLASS_PHISHING
    rule: str            # 规则标识（ad_param / brand_imitation / ...）
    title: str           # 简短标题
    detail: str          # 命中详情
    severity: str        # high / medium / low
    regex: str = ""      # 建议屏蔽正则（空表示不建议自动生成）
    auto_select: bool = True   # 建议默认勾选（误伤风险高时为 False）
    hint: str = ""       # 正则说明 / 使用提示


@dataclass
class UrlAnalysis:
    """单条 URL 的识别结果。"""
    url: str
    findings: list[Finding] = field(default_factory=list)

    @property
    def risk_level(self) -> str:
        """最高风险等级（无命中返回空串）"""
        if not self.findings:
            return ""
        return max((f.severity for f in self.findings),
                   key=lambda s: SEVERITY_ORDER.get(s, 0))

    @property
    def kinds(self) -> set[str]:
        """命中的类别集合（ad / phishing）"""
        return {f.kind for f in self.findings}

    @property
    def is_ad(self) -> bool:
        return CLASS_AD in self.kinds

    @property
    def is_phishing(self) -> bool:
        return CLASS_PHISHING in self.kinds

    @property
    def regexes(self) -> list[str]:
        """本 URL 全部建议正则（去重保序）"""
        out: list[str] = []
        for f in self.findings:
            if f.regex and f.regex not in out:
                out.append(f.regex)
        return out


@dataclass
class RegexSuggestion:
    """去重后的通用正则建议。"""
    regex: str
    title: str
    kind: str
    severity: str
    auto_select: bool
    hint: str
    sample_url: str
    count: int = 1


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def _ensure_scheme(url: str) -> str:
    """缺少协议时补 http://，便于 urlparse 正确解析域名"""
    url = (url or "").strip()
    if url and not re.match(r"(?i)^[a-z][a-z0-9+.-]*://", url):
        return "http://" + url
    return url


def _host_of(url: str) -> str:
    """取小写主机名（无端口），解析失败返回空串"""
    try:
        return (urlparse(_ensure_scheme(url)).hostname or "").lower()
    except ValueError:
        return ""


def _port_of(url: str) -> int | None:
    """取端口号，未显式指定或无/非法端口返回 None"""
    try:
        return urlparse(_ensure_scheme(url)).port
    except ValueError:
        return None


def _path_query_of(url: str) -> str:
    """取路径+查询串（用于敏感词/广告路径检测），解析失败返回原串"""
    try:
        parsed = urlparse(_ensure_scheme(url))
        return unquote(f"{parsed.path}?{parsed.query}" if parsed.query else parsed.path)
    except ValueError:
        return url


def _is_ip(host: str) -> bool:
    """判断主机名是否为 IP 地址（IPv4 / IPv6）"""
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _levenshtein(a: str, b: str, limit: int = 3) -> int:
    """带截断的编辑距离（超过 limit 提前返回 limit+1）"""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        if min(cur) > limit:
            return limit + 1
        prev = cur
    return prev[-1]


def normalize_confusable(text: str) -> str:
    """同形字符归一化：把数字/符号替换为形近字母，并合并 rn/vv"""
    text = (text or "").lower().translate(_HOMOGLYPH_TABLE)
    return text.replace("rn", "m").replace("vv", "w")


def _confusable_pattern(brand: str) -> str:
    """把品牌名转换为覆盖同形变体的正则片段（google → g[o0][o0]g[l1][e3]）"""
    return "".join(_CONFUSABLE_CLASSES.get(ch, re.escape(ch)) for ch in brand)


def _brand_variant_regex(brand: str) -> str:
    """品牌仿冒域名正则：覆盖 www./前缀与 -secure/后缀等同类形态。

    注意：该正则基于品牌同形变体生成，可能同时匹配官方域名，
    因此 auto_select=False，需用户确认后再启用。
    """
    return (r"(?i)//(?:[^/]*[.-])?" + _confusable_pattern(brand) +
            r"(?:[.-][^/]*)?(?::\d+)?(?:/|$)")


def _domain_matches(host: str, domains: set[str]) -> str:
    """host 是否属于给定域名集合（含子域），返回命中的域名，否则空串"""
    for domain in domains:
        if host == domain or host.endswith("." + domain):
            return domain
    return ""


def _registered_domain(host: str) -> str:
    """取注册域（简化实现：最后两段标签）"""
    labels = [l for l in host.split(".") if l]
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


def _is_official(host: str) -> bool:
    """host 是否属于任一品牌的官方域名（含其子域）"""
    for officials in OFFICIAL_SITES.values():
        if _domain_matches(host, officials):
            return True
    return False


def _looks_random(label: str) -> bool:
    """标签是否为随机字符串（长且元音稀少 / 连续辅音）"""
    if len(label) < 12:
        return False
    vowels = sum(ch in "aeiou" for ch in label)
    if vowels / max(len(label), 1) < 0.2:
        return True
    return bool(re.search(r"[bcdfghjklmnpqrstvwxyz]{5,}", label))


# ---------------------------------------------------------------------------
# 广告识别
# ---------------------------------------------------------------------------
def detect_ad(url: str) -> list[Finding]:
    """识别 URL 中的广告特征（参数 / 域名 / 路径）。"""
    findings: list[Finding] = []
    host = _host_of(url)
    path_query = _path_query_of(url)

    # 1. 广告联盟域名
    hit_domain = _domain_matches(host, AD_DOMAINS)
    if hit_domain:
        findings.append(Finding(
            kind=CLASS_AD, rule="ad_domain", title="广告联盟域名",
            detail=f"域名 {host} 属于已知广告/追踪网络（{hit_domain}）",
            severity="high", regex=REGEX_AD_DOMAIN,
            hint="覆盖 doubleclick / googlesyndication / taboola 等常见广告联盟域名"))

    # 2. 追踪参数
    try:
        params = {k.lower() for k in parse_qs(urlparse(_ensure_scheme(url)).query,
                                              keep_blank_values=True)}
    except ValueError:
        params = set()
    strong = sorted(p for p in params
                    if p in STRONG_AD_PARAMS or p.startswith("utm_"))
    weak = sorted(p for p in params if p in WEAK_AD_PARAMS)
    is_ad_context = bool(hit_domain) or bool(AD_PATH_RE.search(path_query))
    if strong:
        findings.append(Finding(
            kind=CLASS_AD, rule="ad_param", title="广告追踪参数",
            detail="命中高置信追踪参数：" + "、".join(strong),
            severity="high", regex=REGEX_AD_PARAM,
            hint="覆盖 utm_* / gclid / fbclid / adid / click_id 等同类型追踪参数"))
    elif weak and is_ad_context:
        findings.append(Finding(
            kind=CLASS_AD, rule="ad_param_weak", title="通用广告参数",
            detail="命中通用广告参数：" + "、".join(weak),
            severity="medium", regex="",
            hint="单一通用参数（click/banner/ref 等）误伤率较高，未自动生成正则"))
    elif weak:
        findings.append(Finding(
            kind=CLASS_AD, rule="ad_param_weak", title="通用广告参数（弱特征）",
            detail="命中通用参数：" + "、".join(weak) + "（可能是普通业务参数）",
            severity="low", regex="",
            hint="如需屏蔽可自行添加 [?&](click|banner|promo|aff|ref)= 等规则"))

    # 3. 广告路径
    match = AD_PATH_RE.search(path_query)
    if match:
        findings.append(Finding(
            kind=CLASS_AD, rule="ad_path", title="广告路径",
            detail=f"路径命中广告目录模式：{match.group(0)}",
            severity="medium", regex=REGEX_AD_PATH,
            hint="覆盖 /ad/ /ads/ /advert/ /banner/ /popup/ 等同类型路径"))
    return findings


# ---------------------------------------------------------------------------
# 钓鱼识别
# ---------------------------------------------------------------------------
def _detect_brand_imitation(host: str, path_query: str) -> list[Finding]:
    """品牌仿冒域名识别（同形字符、品牌+修饰词、品牌占用子域、近似拼写）"""
    if not host or _is_ip(host) or _is_official(host):
        return []
    labels = [l for l in host.split(".") if l]
    if len(labels) < 2:
        return []
    registered = _registered_domain(host)
    findings: list[Finding] = []
    for idx, label in enumerate(labels[:-1]):      # 排除 TLD
        if label == "www":
            continue
        for brand, officials in OFFICIAL_SITES.items():
            if len(brand) < 4:
                continue
            normalized = normalize_confusable(label)
            hit_kind = ""
            if label == brand or normalized == brand:
                hit_kind = "同形字符仿冒" if normalized != label else "品牌名被仿冒使用"
            elif _levenshtein(normalized, brand, limit=1) == 1 and len(label) >= 5:
                hit_kind = "近似拼写仿冒"
            elif len(label) - len(brand) >= 3 and brand in label:
                hit_kind = "品牌名 + 诱饵词"
            if not hit_kind:
                continue
            # 官方域放行：品牌名后紧跟常见官方后缀（如 paypal.com / paypal.co.uk）
            tail = ".".join(labels[idx + 1:])
            if label == brand and tail in OFFICIAL_TLD_SUFFIXES:
                continue
            findings.append(Finding(
                kind=CLASS_PHISHING, rule="brand_imitation",
                title=f"仿冒 {brand} 域名",
                detail=f"{hit_kind}：域名标签 “{label}” 与品牌 “{brand}” 高度相似，"
                       f"注册域 {registered} 非官方域名",
                severity="high", regex=_brand_variant_regex(brand),
                auto_select=False,
                hint="该正则覆盖同形变体，可能同时命中官方品牌域名，"
                     "启用前请确认不会屏蔽需要爬取的正规站点"))
            return findings  # 命中一个品牌即可
    return findings


def _detect_suspicious_keyword(url: str, host: str, domain_suspicious: bool) -> list[Finding]:
    """敏感词 + 可疑域名组合识别（单独出现敏感词不报警）"""
    if not domain_suspicious:
        return []
    text = (host + _path_query_of(url)).lower()
    hits = sorted({kw for kw in SENSITIVE_KEYWORDS if kw in text})
    if not hits:
        return []
    return [Finding(
        kind=CLASS_PHISHING, rule="suspicious_keyword",
        title="敏感词 + 可疑域名",
        detail="URL 含账户/安全类敏感词：" + "、".join(hits) +
               "，且域名存在可疑特征",
        severity="high", regex="",
        hint="已结合可疑域名特征，建议启用该域名的屏蔽规则而非单独屏蔽敏感词")]


def detect_phishing(url: str) -> list[Finding]:
    """识别 URL 中的钓鱼风险特征。"""
    findings: list[Finding] = []
    host = _host_of(url)
    if not host:
        return findings
    port = _port_of(url)
    path_query = _path_query_of(url)

    is_ip = _is_ip(host)
    hit_short = _domain_matches(host, SHORTENER_DOMAINS)
    labels = [l for l in host.split(".") if l]
    tld = labels[-1] if labels else ""

    # 1. IP 直连（+ 非常规端口）
    if is_ip:
        if port is not None and port not in (80, 443):
            findings.append(Finding(
                kind=CLASS_PHISHING, rule="ip_port", title="IP 直连 + 非常规端口",
                detail=f"直接使用 IP 地址 {host}:{port} 访问，绕过域名与证书验证",
                severity="high", regex=REGEX_IP_PORT,
                hint="屏蔽所有“IP + 非 80/443 端口”的访问，覆盖同类型地址"))
        else:
            findings.append(Finding(
                kind=CLASS_PHISHING, rule="ip_host", title="IP 直连访问",
                detail=f"直接使用 IP 地址 {host} 访问，无法通过域名验证站点归属",
                severity="low", regex=REGEX_IP_HOST,
                hint="屏蔽纯 IP 直连链接，可能影响内网测试站点，请按需启用"))

    # 2. 品牌仿冒
    findings.extend(_detect_brand_imitation(host, path_query))

    # 3. 短链接（目标未知）
    if hit_short:
        findings.append(Finding(
            kind=CLASS_PHISHING, rule="shortener", title="短链接跳转",
            detail=f"{host} 为短链接服务，真实目标地址不可见（{hit_short}）",
            severity="medium", regex=REGEX_SHORTENER,
            hint="覆盖 bit.ly / tinyurl / t.cn 等常见短链服务域名"))

    # 4. 异常 TLD
    if tld in SUSPICIOUS_TLDS:
        findings.append(Finding(
            kind=CLASS_PHISHING, rule="bad_tld", title="高风险 TLD",
            detail=f"域名使用高滥用免费顶级域 .{tld}，钓鱼站点高发",
            severity="medium", regex=REGEX_BAD_TLD,
            hint="覆盖 .tk / .ml / .ga / .cf / .gq 免费顶级域"))

    # 5. 可疑域名形态（多连字符 / 随机串）
    hyphen_labels = [l for l in labels[:-1] if l.count("-") >= 2 or "--" in l]
    if hyphen_labels:
        findings.append(Finding(
            kind=CLASS_PHISHING, rule="hyphen_domain", title="域名含大量连字符",
            detail="域名标签 " + "、".join(hyphen_labels) + " 含多个连字符，是仿冒域名常见写法",
            severity="low", regex="",
            hint="连字符特征误伤率较高（正规站点亦常见），未自动生成正则"))
    random_labels = [l for l in labels[:-1] if _looks_random(l)]
    if random_labels:
        findings.append(Finding(
            kind=CLASS_PHISHING, rule="random_domain", title="域名含随机字符串",
            detail="域名标签 " + "、".join(random_labels) + " 形似随机生成（元音稀少/长串辅音）",
            severity="low", regex="",
            hint="随机串特征误伤率较高，未自动生成正则"))

    # 6. 敏感词 + 可疑域名
    domain_suspicious = bool(
        is_ip or hit_short or tld in SUSPICIOUS_TLDS or hyphen_labels
        or random_labels or any(f.rule == "brand_imitation" for f in findings))
    findings.extend(_detect_suspicious_keyword(url, host, domain_suspicious))
    return findings


# ---------------------------------------------------------------------------
# 汇总接口
# ---------------------------------------------------------------------------
def analyze_url(url: str) -> UrlAnalysis:
    """识别单条 URL 的广告与钓鱼风险。"""
    url = (url or "").strip()
    if not url:
        return UrlAnalysis(url=url)
    findings = detect_ad(url) + detect_phishing(url)
    order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: order.get(f.severity, 3))
    return UrlAnalysis(url=url, findings=findings)


def analyze_urls(urls) -> list[UrlAnalysis]:
    """批量识别（自动跳过空行与重复 URL，保持输入顺序）。"""
    results: list[UrlAnalysis] = []
    seen: set[str] = set()
    for raw in urls:
        url = (raw or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        results.append(analyze_url(url))
    return results


def collect_regex_suggestions(analyses) -> list[RegexSuggestion]:
    """把识别结果归纳为去重后的通用正则建议（按风险等级排序）。"""
    merged: dict[str, RegexSuggestion] = {}
    for analysis in analyses:
        for finding in analysis.findings:
            if not finding.regex:
                continue
            if finding.regex in merged:
                merged[finding.regex].count += 1
                continue
            merged[finding.regex] = RegexSuggestion(
                regex=finding.regex, title=finding.title, kind=finding.kind,
                severity=finding.severity, auto_select=finding.auto_select,
                hint=finding.hint, sample_url=analysis.url)
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(merged.values(),
                  key=lambda s: order.get(s.severity, 3))


def summarize(analyses) -> dict:
    """统计识别结果：URL 数 / 广告数 / 钓鱼数 / 高风险数 / 建议正则数。"""
    return {
        "url_count": len(analyses),
        "ad_count": sum(1 for a in analyses if a.is_ad),
        "phishing_count": sum(1 for a in analyses if a.is_phishing),
        "high_count": sum(1 for a in analyses if a.risk_level == "high"),
        "regex_count": len(collect_regex_suggestions(analyses)),
    }


__all__ = [
    "CLASS_AD", "CLASS_PHISHING", "SEVERITY_ORDER",
    "Finding", "UrlAnalysis", "RegexSuggestion",
    "detect_ad", "detect_phishing", "analyze_url", "analyze_urls",
    "collect_regex_suggestions", "summarize", "normalize_confusable",
]
