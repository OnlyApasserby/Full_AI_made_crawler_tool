"""UA 池：常用浏览器 User-Agent 集合与线程安全随机抽取提供者。

``UserAgentPool`` 供爬取线程与下载线程共用，保证多线程并发随机取 UA 时安全。
"""

from __future__ import annotations

import random
import threading

# 常用浏览器 UA 池（UserAgentPool 默认使用）
DEFAULT_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edge/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
]


class UserAgentPool:
    """UA池：线程安全地提供随机User-Agent，支持添加/删除。"""

    def __init__(self, user_agents=None):
        self._lock = threading.Lock()
        self._user_agents = list(user_agents) if user_agents else list(DEFAULT_USER_AGENTS)

    def random(self):
        """随机返回一个UA（线程安全）"""
        with self._lock:
            return random.choice(self._user_agents)

    def add(self, ua):
        ua = ua.strip()
        if ua and ua not in self._user_agents:
            with self._lock:
                self._user_agents.append(ua)
            return True
        return False

    def remove(self, ua):
        with self._lock:
            if ua in self._user_agents:
                self._user_agents.remove(ua)
                return True
        return False

    def reset(self):
        """恢复默认UA列表"""
        with self._lock:
            self._user_agents = list(DEFAULT_USER_AGENTS)

    def all(self):
        with self._lock:
            return list(self._user_agents)


__all__ = ["DEFAULT_USER_AGENTS", "UserAgentPool"]
