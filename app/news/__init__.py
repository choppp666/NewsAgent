"""新闻能力层（第三阶段）。

分四块，彼此单向依赖，便于单测：

    feeds.py     源目录（配置）
    http.py      抓取（网络 + 限额 + SSRF 防护）
    rss.py       Feed 解析 → FeedItem
    article.py   文章页解析 → Article
    dedupe.py    去重（URL 规范化 + 标题相似度）
    filter.py    时间窗口 / 关键词匹配 / 打分
    providers.py 搜索提供方抽象（RSS 实现 + 扩展注册点）
    state.py     一次运行内的候选池（跨调用去重 + 链接白名单）
    briefing.py  简报结构校验

上层的 `app/agent/tools/news.py` 只负责把这些能力包装成工具，
不重复实现任何逻辑。
"""

from app.news.article import Article, extract_article
from app.news.briefing import BriefingReport, parse_briefing
from app.news.dedupe import NewsDeduplicator, canonical_url, title_similarity
from app.news.feeds import FEED_CATALOG, FeedSource, resolve_feeds
from app.news.filter import DateRangeError, DateWindow, build_query, match_item, parse_date_range
from app.news.http import FetchOutcome, fetch_text
from app.news.providers import NewsProvider, ProviderResult, get_provider, register_provider
from app.news.rss import FeedItem, FeedParseError, parse_feed
from app.news.state import NewsRunState

__all__ = [
    "Article",
    "BriefingReport",
    "DateRangeError",
    "DateWindow",
    "FEED_CATALOG",
    "FeedItem",
    "FeedParseError",
    "FeedSource",
    "FetchOutcome",
    "NewsDeduplicator",
    "NewsProvider",
    "NewsRunState",
    "ProviderResult",
    "build_query",
    "canonical_url",
    "extract_article",
    "fetch_text",
    "get_provider",
    "match_item",
    "parse_briefing",
    "parse_date_range",
    "parse_feed",
    "register_provider",
    "resolve_feeds",
    "title_similarity",
]
