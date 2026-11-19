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
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
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


@dataclass(slots=True)
class _CacheEntry:
    """一次抓取结果的缓存条目（按源集合共享，只读使用）。"""

    expires_at: float
    items: list[FeedItem]
    sources: list[SourceReport]
    fetched_at: float


class RSSNewsProvider:
    """基于 RSS/Atom 的实现（默认提供方）。

    带**进程内 TTL 缓存**：一次简报生成里模型通常要搜 4~8 次，而每次
    `search()` 都会把 10 个源全部重新下载一遍——实测单次要 1.3~3.2 秒，
    且同一批源的响应在几分钟内不会有变化。缓存把「第 2 次以后的搜索」
    从「每秒级网络等待」降到「毫秒级字典查询」，同时减少对源站的重复请求
    （不少源对高频抓取会返回 403）。

    缓存键是「源集合 + 单源条数上限」，因此换个源子集搜索不会互相污染；
    TTL 默认 `NEWS_FEED_CACHE_TTL=300` 秒，设 0 即彻底关闭缓存。
    """

    name = "rss"

    def __init__(self, *, timeout: float | None = None, per_feed_limit: int | None = None) -> None:
        self.timeout = timeout if timeout is not None else settings.NEWS_FETCH_TIMEOUT
        self.per_feed_limit = per_feed_limit if per_feed_limit is not None else settings.NEWS_MAX_ITEMS_PER_FEED
        self._cache: dict[tuple, _CacheEntry] = {}
        self._cache_lock = threading.Lock()
        # 每种源集合一把锁：并发跑多个用户时，先到的抓取、后到的等锁后直接命中缓存，
        # 避免「两个用户同时把 10 个源抓一遍」。
        self._key_locks: dict[tuple, threading.Lock] = {}

    # ---------------------------------------------------------- 缓存

    @property
    def cache_ttl(self) -> int:
        try:
            return max(0, int(settings.NEWS_FEED_CACHE_TTL))
        except (TypeError, ValueError):
            return 0

    @property
    def deadline(self) -> float:
        """一轮抓取的总预算（秒），至少 1 秒，防止配置成 0 之后等价于「立即全失败」。"""
        try:
            return max(1.0, float(settings.NEWS_SEARCH_DEADLINE))
        except (TypeError, ValueError):
            return 8.0

    def clear_cache(self) -> None:
        """清空缓存（测试用；也可在运维接口里做成「立即刷新」）。"""
        with self._cache_lock:
            self._cache.clear()

    def _cache_key(self, feeds: list[FeedSource]) -> tuple:
        return (tuple(feed.id for feed in feeds), self.per_feed_limit)

    def _get_cached(self, key: tuple) -> _CacheEntry | None:
        ttl = self.cache_ttl
        if ttl <= 0:
            return None
        with self._cache_lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            if entry.expires_at <= time.monotonic():
                self._cache.pop(key, None)
                return None
            return entry

    def _store(self, key: tuple, items: list[FeedItem], sources: list[SourceReport]) -> None:
        ttl = self.cache_ttl
        if ttl <= 0:
            return
        now = time.monotonic()
        with self._cache_lock:
            self._cache[key] = _CacheEntry(
                expires_at=now + ttl,
                items=items,
                sources=sources,
                fetched_at=now,
            )

    def _lock_for(self, key: tuple) -> threading.Lock:
        with self._cache_lock:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._key_locks[key] = lock
            return lock

    # ---------------------------------------------------------- 检索

    def search(self, sources: list[str] | None = None) -> ProviderResult:
        feeds = resolve_feeds(sources)
        if not feeds:
            return ProviderResult()

        key = self._cache_key(feeds)
        entry = self._get_cached(key)
        if entry is not None:
            age = int(time.monotonic() - entry.fetched_at)
            logger.info(
                "RSS 搜索命中缓存（%d 个源/%d 条，%ds 前抓取，TTL %ds）",
                len(entry.sources),
                len(entry.items),
                age,
                self.cache_ttl,
            )
            # 复制列表：调用方可能对返回值做 extend/append，不能污染缓存
            return ProviderResult(items=list(entry.items), sources=list(entry.sources))

        # 只有一个线程真正去抓；等锁的线程拿到锁后走双检，直接命中缓存
        with self._lock_for(key):
            entry = self._get_cached(key)
            if entry is not None:
                return ProviderResult(items=list(entry.items), sources=list(entry.sources))

            result = self._fetch_all(feeds)
            self._store(key, result.items, result.sources)
            return result

    def _fetch_all(self, feeds: list[FeedSource]) -> ProviderResult:
        """真实抓取全部源（并发），单个源失败只记在 SourceReport 里。

        整批有**总时间预算**（`NEWS_SEARCH_DEADLINE`，默认 8 秒）：超过预算还没回来的源
        直接按失败计入报告，而不是继续等。原因很实际——`httpx.Timeout` 限制的是
        「每个阶段」，响应体一点点往外吐就能绕过它，实测有源跑到 13 秒；
        一批 10 个源里只要混进一个这样的，整次搜索就白等十几秒，而模型还会再搜 5 次。
        """
        result = ProviderResult()
        deadline = self.deadline

        executor = ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(feeds)))
        futures = {executor.submit(self._fetch_one, feed): feed for feed in feeds}
        done, pending = wait(futures, timeout=deadline)

        outcomes: list[tuple[FeedSource, SourceReport, list[FeedItem]]] = []
        try:
            for future in done:
                feed = futures[future]
                try:
                    outcomes.append(future.result())
                except Exception as exc:  # noqa: BLE001 - 单源异常不能让整批失败
                    outcomes.append(
                        (feed, SourceReport(feed.id, feed.name, ok=False, error=f"抓取异常：{exc}"), [])
                    )

            for future in pending:
                feed = futures[future]
                future.cancel()  # 未开始的任务直接取消；已在跑的线程无法强杀，只能放弃等待
                outcomes.append(
                    (
                        feed,
                        SourceReport(
                            feed.id,
                            feed.name,
                            ok=False,
                            error=f"超过批次总预算 {deadline:g}s 仍未返回（该源过慢），本轮跳过",
                        ),
                        [],
                    )
                )
        finally:
            # wait=False：不等那些还在跑的慢源，否则又被拖回「等到齐」的老路上
            executor.shutdown(wait=False, cancel_futures=True)

        # 报告顺序与 feeds 保持一致，日志和前端看起来才稳定
        order = {feed.id: index for index, feed in enumerate(feeds)}
        outcomes.sort(key=lambda item: order.get(item[0].id, 0))

        for _feed, report, items in outcomes:
            result.sources.append(report)
            if report.ok:
                result.items.extend(items)

        slow = [report for report in result.sources if not report.ok and "总预算" in (report.error or "")]
        logger.info(
            "RSS 搜索完成：%d/%d 个源可用，共 %d 条%s",
            len(result.ok_sources),
            len(result.sources),
            len(result.items),
            f"（{len(slow)} 个源超 {deadline:g}s 预算被跳过）" if slow else "",
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
