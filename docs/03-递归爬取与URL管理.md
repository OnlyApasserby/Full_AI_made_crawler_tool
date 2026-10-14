# 阶段 03：递归爬取与 URL 管理

## 阶段目标

- 掌握 BFS 递归爬取的实现骨架与深度控制；
- 理解三层去重体系：URL 归一化去重、内容 MD5、感知哈希（dhash）；
- 会用 `URLFilter` 圈定抓取范围，理解线程安全与规则热生效；
- 理解"礼貌抓取"：暂停/停止、请求抖动、UA 轮换。

## 3.1 BFS 递归爬取：`core/crawler.py`

`RecursiveCrawlerThread`（QThread）是项目文本递归爬取的心脏：

```
frontier = [起始URL]                      # 当前层
while frontier and depth <= max_depth:
    next_level = []
    for url in frontier:
        page = fetch(url)                 # 抓取
        save(page)                        # 存储（入库/落盘）
        for link in extract_links(page):  # 发现
            if in_scope(link) and not seen(link):
                next_level.append(link)
    frontier = next_level                 # 逐层推进 = BFS
```

关键实现点：

- **分层推进**：`current_level_urls` / `next_level_urls` 两个容器交替，深度自然可控；
- **双去重**：会话内 `set`（快）+ 数据库 `url_hash`（跨重启），SHA-256 指纹；
- **可暂停/停止**：`threading.Event` 做开关；对阻塞中的 socket 请求，用 `socket.shutdown()` 强制中断——比干等超时优雅得多；
- **礼貌抓取**：随机 UA（`utils/user_agents.py`）+ 可选代理 + 请求间隔抖动；
- **流式读取 + 停止检查点**：大页面下载过程中也能响应停止。

## 3.2 去重：爬虫的必修课

| 层次 | 手段 | 位置 | 解决什么 |
|---|---|---|---|
| URL 层 | 归一化后 SHA-256 指纹（会话集 + DB） | `core/crawler.py`、`core/media/models.py` | `?a=1&b=2` vs `?b=2&a=1`、末尾 `/`、大小写主机名、默认端口、`#fragment` 等"同一页" |
| 内容层（精确） | MD5（Digest / Content-MD5） | `core/downloader.py` | 不同 URL 同一文件 |
| 内容层（近似） | dhash 感知哈希 + 汉明距离 | `core/media/probe.py` | 压缩/加水印/改尺寸的"同一张图" |

**URL 归一化**是性价比最高的一课：`normalize_url` / `url_fingerprint`（`core/media/models.py`）值得逐行读——它定义了"什么算同一个 URL"。

**感知哈希**概念：把图片缩放成固定小尺寸、算灰度均值生成 64 位指纹，两张图汉明距离小即"视觉相同"。用于过滤重复图、占位图。

## 3.3 URL 过滤：`core/filter.py`

`URLFilter` 用两级判定圈定范围：

1. **白名单**（域名/路径前缀）：只抓这些；
2. **屏蔽正则**：命中即拒。

工程要点：`threading.Lock` 保证多线程下规则读写安全；规则修改**实时生效**（热更新）；`should_fetch()`（判定）与 `explain()`（解释为什么拒）分离——后者是调试抓取范围问题的利器；过滤统计与样本供界面展示。

**延伸 `core/security.py`**：广告/钓鱼识别与正则生成。识别追踪参数、广告联盟域名、品牌仿冒（同形字符归一化 + 编辑距离 ≤1 + 品牌词 + 诱饵词）、短链/高危 TLD/IP 直连。最有教学价值的是它把命中案例**归纳成"覆盖同类"的通用正则**再交给 `URLFilter` 屏蔽——"从个案到规则"的泛化思维。

## 3.4 礼貌抓取三件套

| 手段 | 位置 | 说明 |
|---|---|---|
| UA 池轮换 | `utils/user_agents.py` | 线程安全随机 UA，支持增删/重置 |
| 请求延迟抖动 | `core/crawler.py` | 间隔 = 基准 + 随机抖动，避免固定节奏被识别 |
| 代理支持 | `config.py` / fetcher | 可选；代理 IP 与时区/地理位置要一致（阶段 07） |

## 3.5 源码精读清单

| 文件 | 重点看什么 |
|---|---|
| `core/crawler.py` | BFS 双层队列；双去重；`threading.Event` 暂停/停止；socket shutdown 中断；流式读取检查点 |
| `core/media/models.py` | `normalize_url` / `url_fingerprint` 的归一化规则逐条 |
| `core/filter.py` | 白名单→屏蔽正则两级判定；`Lock`；`should_fetch`/`explain` 分离 |
| `core/security.py` | 广告/钓鱼特征归纳成通用正则的过程 |
| `utils/user_agents.py` | `UserAgentPool` 的线程安全实现 |

## 3.6 动手练习

1. **50 行迷你 BFS 爬虫**：只用 `requests` + `core.parser.extract_links`，写一个带深度限制（2 层）、URL 归一化去重、`time.sleep` 抖动的命令行爬虫，抓一个小站点，打印每层 URL 数；
2. **去重验证**：构造同一页面的 5 种写法（`?b=2&a=1` / `?a=1&b=2` / 末尾 `/` / `#锚点` / 大写主机名），用 `url_fingerprint` 验证它们指纹相同；
3. **过滤实验**：用 `URLFilter` 配置"只抓 example.com、屏蔽 `\.jpg$`"，对一组混合 URL 批量判定，并用 `explain()` 输出每个被拒 URL 的原因；
4. **中断实验**：给练习 1 的爬虫加"按回车暂停/继续"（`threading.Event` 思路即可），观察暂停后不再发新请求；
5. **泛化练习**：仿照 `security.py` 的思路，给 3 个广告 URL 手工归纳出一条通用屏蔽正则，加入 `URLFilter` 验证能覆盖它们。

## 3.7 验收标准

- [ ] 能默写 BFS 爬虫骨架（队列、深度、去重、存储四要素齐全）；
- [ ] 能解释"会话集 + DB"双去重各自的存在意义；
- [ ] 能说出 URL 归一化至少 5 条规则；
- [ ] 能区分 MD5 去重与 dhash 去重的适用场景；
- [ ] 练习 1/2/3 全部通过。
