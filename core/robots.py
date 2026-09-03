"""robots.txt：离线解析器（RobotsParser）与在线获取线程（RobotsCheckThread）。

- ``RobotsParser``：支持 User-agent / Allow / Disallow 规则的简单解析
- ``RobotsCheckThread``：后台线程抓取目标站点的 robots.txt（供单窗口与多任务调度共用）
"""

from __future__ import annotations

import requests
from urllib.parse import urlparse, urljoin
from PyQt6.QtCore import QThread, pyqtSignal


class RobotsParser:
    """简单的 robots.txt 解析器，支持 User-agent / Allow / Disallow 规则。"""

    def __init__(self, content):
        self.groups = {}  # user-agent -> {"allow": [...], "disallow": [...]}
        if content:
            self.parse(content)

    def parse(self, content):
        """逐行解析 robots.txt，按 User-agent 分组记录 Allow / Disallow 规则"""
        current_agents = []
        for raw_line in content.splitlines():
            line = raw_line.split("#", 1)[0].strip()  # 去掉注释
            if not line or ":" not in line:
                continue
            key, _, value = line.partition(":")
            key = key.strip().lower()
            value = value.strip()
            if key == "user-agent":
                current_agents = [value]
                self.groups.setdefault(value, {"allow": [], "disallow": []})
            elif current_agents:
                if key == "disallow":
                    for agent in current_agents:
                        self.groups[agent]["disallow"].append(value)
                elif key == "allow":
                    for agent in current_agents:
                        self.groups[agent]["allow"].append(value)

    def _match_group(self, user_agent):
        """按最长匹配优先原则返回适用于指定UA的规则组，无匹配时回退到 *"""
        if not user_agent:
            return self.groups.get("*")
        token = user_agent.lower()
        matched = [ag for ag in self.groups if ag != "*" and ag.lower() in token]
        if matched:
            return self.groups[max(matched, key=len)]
        return self.groups.get("*")

    def is_disallowed(self, url, user_agent):
        """判断指定URL是否被robots.txt禁止（Allow优先于Disallow，匹配更长者生效）"""
        rules = self._match_group(user_agent)
        if not rules:
            return False
        path = urlparse(url).path or "/"
        allow_matches = [a for a in rules["allow"] if a and path.startswith(a)]
        disallow_matches = [d for d in rules["disallow"] if d and path.startswith(d)]
        if not disallow_matches:
            return False
        longest_disallow = max(len(d) for d in disallow_matches)
        longest_allow = max((len(a) for a in allow_matches), default=0)
        return longest_disallow > longest_allow


class RobotsCheckThread(QThread):
    """后台获取指定站点 robots.txt 内容的线程。"""

    signal_robots_content = pyqtSignal(str, str)
    signal_error = pyqtSignal(str)

    def __init__(self, target_url):
        super().__init__()
        self.target_url = target_url

    def run(self):
        try:
            parsed_url = urlparse(self.target_url)
            robots_url = urljoin(f"{parsed_url.scheme}://{parsed_url.netloc}", "/robots.txt")
            resp = requests.get(robots_url, timeout=10)
            if resp.status_code == 200:
                self.signal_robots_content.emit(resp.text, robots_url)
            else:
                self.signal_robots_content.emit("该站点未提供robots.txt文件", robots_url)
        except Exception as e:
            self.signal_error.emit(f"获取robots.txt失败：{str(e)}")


__all__ = ["RobotsParser", "RobotsCheckThread"]
