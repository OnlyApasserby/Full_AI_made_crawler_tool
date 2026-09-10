"""站点适配器插件目录（第三层"通用化"：处理长尾站点）。

放置约定
--------
在本目录新增任意 ``.py`` 文件即可被自动加载（``registry.load_plugins`` 扫描），
**无需修改任何既有代码**。两种导出方式任选其一：

1. 显式声明（推荐，顺序可控）::

    from core.extractor.base import BaseExtractor

    class MySiteExtractor(BaseExtractor):
        name = "site:mysite"
        priority = 10                      # 站点适配器用 10，优先于通用提取器

        def can_handle(self, url, ctx):
            return 0.95 if "mysite.com" in url else 0.0

        def extract(self, ctx):
            ...                            # yield MediaItem

    EXTRACTORS = [MySiteExtractor]

2. 隐式发现：模块内定义任意 :class:`BaseExtractor` 子类即可被收集。

实现建议
--------
- 需要登录态/签名的站点：用 ``ctx.network_events`` 取浏览器实际请求过的地址与请求头
- 需要解密/拼接的流媒体：产出 ``kind="hls"`` 的 ``MediaItem`` 并写入 ``extra``，
  由后续 ``core/media/hls.py``（可选功能）负责合并
- 只能 HTTP 接口取数据的站点：用 ``ctx.json_data``（``fetch_json`` 的结果）
"""

__all__ = []
