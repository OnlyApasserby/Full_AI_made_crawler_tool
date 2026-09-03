"""URL 过滤器：正则屏蔽规则 + 域名白名单，线程安全（原 URLFilter）。

判定顺序（should_fetch）：
1. 若配置了域名白名单且URL域名不在白名单内 → 过滤
2. 任一屏蔽正则命中URL → 过滤

规则变化（添加/删除/批量设置）实时生效，无需重启爬取任务。
所有共享数据均受 threading.Lock 保护，可在爬取线程与GUI线程间安全共享。
"""

from __future__ import annotations

import json
import re
import threading
from urllib.parse import urlparse


class URLFilter:
    """URL过滤管理器。

    供 GUI（主窗口工具栏 / 过滤管理对话框）与爬取线程共享同一实例，
    也用于为多任务中的单个任务创建任务级独立过滤实例。
    """

    MAX_SAMPLES = 100  # 最近被过滤URL样本保留上限

    def __init__(self, block_patterns: list[str] | None = None,
                 allow_domains: list[str] | None = None) -> None:
        """初始化过滤规则。

        :param block_patterns: 初始屏蔽正则列表（非法正则自动忽略）
        :param allow_domains: 初始域名白名单列表
        """
        self._lock = threading.Lock()  # 保护所有共享数据
        self._block_patterns: list[str] = []  # 屏蔽正则（原始字符串）
        self._block_compiled: dict[str, re.Pattern] = {}  # 预编译正则缓存
        self._allow_domains: set[str] = set()  # 域名白名单
        self._filtered_count: int = 0  # 被过滤URL累计计数
        self._filtered_samples: list[str] = []  # 最近被过滤的URL样本
        for pattern in (block_patterns or []):
            self.add_block_pattern(pattern)
        for domain in (allow_domains or []):
            self.add_allow_domain(domain)

    # ---------- 工具方法 ----------
    @staticmethod
    def _normalize_domain(domain: str) -> str:
        """规范化域名：去协议前缀/路径/端口/开头点号并转小写，无法识别时返回空串"""
        domain = (domain or "").strip().lower()
        if not domain:
            return ""
        if "://" in domain:
            domain = urlparse(domain).netloc or domain
        return domain.split("/")[0].split(":")[0].lstrip(".")

    def _record_filtered(self, url: str) -> None:
        """记录一次过滤事件（计数+样本，样本最多保留MAX_SAMPLES条）。调用方须持有锁"""
        self._filtered_count += 1
        self._filtered_samples.append(url)
        if len(self._filtered_samples) > self.MAX_SAMPLES:
            self._filtered_samples = self._filtered_samples[-self.MAX_SAMPLES:]

    # ---------- 屏蔽正则规则 ----------
    def add_block_pattern(self, pattern: str) -> bool:
        """添加屏蔽正则规则并验证正则有效性。

        :return: 添加成功返回True；正则非法或已存在返回False
        """
        pattern = pattern.strip()
        if not pattern:
            return False
        try:
            re.compile(pattern)
        except re.error:
            return False
        with self._lock:
            if pattern in self._block_patterns:
                return False
            self._block_patterns.append(pattern)
            self._block_compiled[pattern] = re.compile(pattern)
            return True

    def remove_block_pattern(self, pattern: str) -> bool:
        """移除指定屏蔽正则规则。

        :return: 移除成功返回True；规则不存在返回False
        """
        with self._lock:
            if pattern in self._block_patterns:
                self._block_patterns.remove(pattern)
                self._block_compiled.pop(pattern, None)
                return True
        return False

    def set_block_patterns(self, patterns: list[str]) -> int:
        """批量设置屏蔽正则（替换现有全部规则），非法正则自动忽略。

        :return: 实际生效的正则数量
        """
        valid: list[str] = []
        for pattern in patterns:
            pattern = str(pattern).strip()
            if not pattern or pattern in valid:
                continue
            try:
                re.compile(pattern)
            except re.error:
                continue
            valid.append(pattern)
        with self._lock:
            self._block_patterns = valid
            self._block_compiled = {p: re.compile(p) for p in valid}
        return len(valid)

    def block_patterns(self) -> list[str]:
        """返回当前全部屏蔽正则（副本，线程安全）"""
        with self._lock:
            return list(self._block_patterns)

    # ---------- 域名白名单 ----------
    def add_allow_domain(self, domain: str) -> bool:
        """添加域名白名单（自动规范化：去协议/路径/端口/开头点号）。

        :return: 添加成功返回True；空值返回False
        """
        domain = self._normalize_domain(domain)
        if not domain:
            return False
        with self._lock:
            self._allow_domains.add(domain)
        return True

    def remove_allow_domain(self, domain: str) -> bool:
        """移除域名白名单。

        :return: 移除成功返回True；域名不存在返回False
        """
        domain = self._normalize_domain(domain)
        with self._lock:
            if domain in self._allow_domains:
                self._allow_domains.remove(domain)
                return True
        return False

    def allow_domains(self) -> list[str]:
        """返回当前全部白名单域名（副本，排序，线程安全）"""
        with self._lock:
            return sorted(self._allow_domains)

    # ---------- 判定与统计 ----------
    def should_fetch(self, url: str) -> bool:
        """判断URL是否应被爬取。

        规则：先检查域名白名单（已配置时必须命中才放行），再检查正则屏蔽规则；
        任一规则命中返回False。任何解析/匹配异常都视为放行，保证爬取不中断。
        """
        try:
            host = urlparse(url).netloc.lower()
        except Exception:
            host = ""
        # 1. 域名白名单：已配置时必须命中才放行
        with self._lock:
            if self._allow_domains:
                allowed = any(host == d or host.endswith("." + d)
                              for d in self._allow_domains)
                if not allowed:
                    self._record_filtered(url)
                    return False
        # 2. 正则屏蔽规则：任一命中即过滤
        with self._lock:
            compiled = list(self._block_compiled.values())
        for regex in compiled:
            try:
                if regex.search(url):
                    self._record_filtered(url)
                    return False
            except re.error:
                continue
        return True

    def get_stats(self) -> dict:
        """返回过滤统计信息（副本，线程安全）。

        :return: 含 block_pattern_count / allow_domain_count / filtered_count / recent_filtered 的字典
        """
        with self._lock:
            return {
                "block_pattern_count": len(self._block_patterns),
                "allow_domain_count": len(self._allow_domains),
                "filtered_count": self._filtered_count,
                "recent_filtered": list(self._filtered_samples),
            }

    # ---------- 配置持久化 ----------
    def save_config(self, filepath: str) -> None:
        """保存屏蔽规则与白名单到JSON文件。

        :raises OSError: 文件写入失败时抛出
        """
        data = {
            "block_patterns": self.block_patterns(),
            "allow_domains": self.allow_domains(),
        }
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def load_config(self, filepath: str) -> bool:
        """从JSON文件加载屏蔽规则与白名单（替换现有配置）。

        :raises ValueError: 配置文件格式错误时抛出
        :raises OSError: 文件读取失败时抛出
        :return: 加载成功返回True
        """
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        patterns = data.get("block_patterns", [])
        domains = data.get("allow_domains", [])
        if not isinstance(patterns, list) or not isinstance(domains, list):
            raise ValueError("配置文件格式错误：block_patterns/allow_domains 必须是列表")
        self.set_block_patterns([str(p) for p in patterns])
        valid_domains: set[str] = set()
        for domain in domains:
            normalized = self._normalize_domain(str(domain))
            if normalized:
                valid_domains.add(normalized)
        with self._lock:
            self._allow_domains = valid_domains
        return True


__all__ = ["URLFilter"]
