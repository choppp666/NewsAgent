"""新闻搜索提供方（Provider）抽象与 RSS 实现。

为什么要抽象一个 Provider：
第三阶段明确要求「优先实现可用的 RSS 方案，同时为后续接入其他搜索服务预留扩展接口」。
把「搜索」收敛成一个协议后，换成 NewsAPI / 博查 / Tavily / 模型内置 Web Search
只需要新增一个实现类并注册，`search_news` 工具和 Agent 主循环都不用改。

约定：Provider **只负责取回原始条目**（标题、链接、时间、摘要、来源），
不做关键词过滤、不做去重、不做相关度判断——那些是所有 Provider 共用的下游逻辑，
放在工具层统一处理，避免每个实现各写一套。
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from app.core.config import settings
from app.news import http as news_http
from app.news.feeds import FeedSource, resolve_feeds
from app.news.rss import FeedItem, FeedParseError, parse_feed

logger = logging.getLogger(__name__)

# 并发抓取的线程数：源数量一般 < 15，6 路并发足够把总耗时压到单源水平
_MAX_WORKERS = 6


@dataclass(slots=True)
class SourceReport:
    """单个源的抓取结果（无论成功失败都会有一条，便于排查）。"""

    source_id: str
    name: str
    ok: bool
    item_count: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.name,
            "source_id": self.source_id,
            "ok": self.ok,
            "items": self.item_count,
            "error": self.error,
        }


@dataclass(slots=True)
class ProviderResult:
    """一次搜索的原始结果。"""

    items: list[FeedItem] = field(default_factory=list)
    sources: list[SourceReport] = field(default_factory=list)

    @property
    def failed_sources(self) -> list[SourceReport]:
        return [report for report in self.sources if not report.ok]

    @property
    def ok_sources(self) -> list[SourceReport]:
        return [report for report in self.sources if report.ok]


@runtime_checkable
class NewsProvider(Protocol):
    """新闻搜索提供方协议。"""

    name: str

    def search(self, sources: list[str] | None = None) -> ProviderResult:
        """按源列表取回原始条目。

        Args:
            sources: 源 ID 列表；None 表示使用默认源集合。

        Returns:
            ProviderResult。**不抛异常**：单个源失败记录在 SourceReport 里，
            全部失败时 items 为空、sources 全为失败状态，由上层决定如何表述。
        """
        ...


class RSSNewsProvider:
    """基于 RSS/Atom 的实现（默认提供方）。"""

    name = "rss"

    def __init__(self, *, timeout: float | None = None, per_feed_limit: int | None = None) -> None:
        self.timeout = timeout if timeout is not None else settings.NEWS_FETCH_TIMEOUT
        self.per_feed_limit = per_feed_limit if per_feed_limit is not None else settings.NEWS_MAX_ITEMS_PER_FEED

    def search(self, sources: list[str] | None = None) -> ProviderResult:
        feeds = resolve_feeds(sources)
        result = ProviderResult()

        if not feeds:
            return result

        with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(feeds))) as pool:
            outcomes = list(pool.map(self._fetch_one, feeds))

        for feed, report, items in outcomes:
            result.sources.append(report)
            if report.ok:
                result.items.extend(items)

        logger.info(
            "RSS 搜索完成：%d/%d 个源可用，共 %d 条",
            len(result.ok_sources),
            len(result.sources),
            len(result.items),
        )
        return result

    def _fetch_one(self, feed: FeedSource) -> tuple[FeedSource, SourceReport, list[FeedItem]]:
        """抓取并解析单个源；任何异常都转成 SourceReport。"""
        outcome: news_http.FetchOutcome = news_http.fetch_text(
            feed.url,
            timeout=self.timeout,
            max_bytes=settings.NEWS_HTTP_MAX_BYTES,
            accept=news_http.RSS_ACCEPT,
        )
        if not outcome.ok:
            return feed, SourceReport(feed.id, feed.name, ok=False, error=outcome.error or "抓取失败"), []

        if outcome.truncated:
            # 截断的 XML 一定解析失败，提前给出明确原因，避免当成「格式错误」
            return (
                feed,
                SourceReport(
                    feed.id,
                    feed.name,
                    ok=False,
                    error=f"响应超过 {settings.NEWS_HTTP_MAX_BYTES} 字节被截断，无法解析",
                ),
                [],
            )

        try:
            items = parse_feed(outcome.text, feed, limit=self.per_feed_limit)
        except FeedParseError as exc:
            return feed, SourceReport(feed.id, feed.name, ok=False, error=str(exc)), []

        return feed, SourceReport(feed.id, feed.name, ok=True, item_count=len(items)), items


# ---------------------------------------------------------------- 注册表

_PROVIDERS: dict[str, NewsProvider] = {"rss": RSSNewsProvider()}


def register_provider(provider: NewsProvider) -> None:
    """注册一个新的搜索提供方（扩展点）。

    示例——接入模型内置 Web Search：

        class WebSearchProvider:
            name = "websearch"
            def search(self, sources=None) -> ProviderResult:
                ...

        register_provider(WebSearchProvider())
    """
    if not getattr(provider, "name", ""):
        raise ValueError("Provider 必须有 name 属性")
    _PROVIDERS[provider.name] = provider


def get_provider(name: str = "rss") -> NewsProvider:
    """按名称取提供方，未知名称回退到 RSS（保证工具不会因为参数写错而失败）。"""
    provider = _PROVIDERS.get((name or "rss").lower())
    if provider is None:
        logger.warning("未知的新闻提供方 %r，已回退到 rss", name)
        return _PROVIDERS["rss"]
    return provider


def list_providers() -> list[str]:
    return list(_PROVIDERS)
