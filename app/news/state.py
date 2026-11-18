"""一次 Agent 运行内的新闻状态。

为什么需要它：
1. **跨调用去重**：「搜 MCP」和「搜 Model Context Protocol」是两次工具调用，
   如果各自去重，同一批新闻会重复出现，模型会高估当天信息量；
2. **链接白名单**：简报最终要写「原文链接」，必须能校验这个链接
   到底是不是本轮真的检索到的——这是「不允许编造链接」的技术兜底；
3. **候选编号**：给模型一个短 id（n1、n2……），比让它复述长 URL 可靠得多。

状态随 `ToolContext` 一起传入工具，工具本身仍然是 `(args, ctx) -> ToolResult`
的纯函数风格——所有可变性都收敛在这一个对象上，便于测试与追踪。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings
from app.news.dedupe import NewsDeduplicator, canonical_url


@dataclass(slots=True)
class NewsRunState:
    """本轮运行见过的所有新闻、抓取记录与白名单链接。"""

    user_id: int | None = None
    # 本次运行的 run_id，写入简报便于回溯轨迹（由 AgentRunner / 接口注入）
    run_id: str | None = None
    # 去重阈值可配置，测试里可以调低以验证近似标题命中
    deduper: NewsDeduplicator = field(
        default_factory=lambda: NewsDeduplicator(threshold=settings.NEWS_TITLE_SIMILARITY)
    )
    # news_id -> 条目（含 title/url/source/published/summary/score）
    items: dict[str, dict[str, Any]] = field(default_factory=dict)
    # canonical_url -> news_id
    index: dict[str, str] = field(default_factory=dict)
    # canonical_url -> 抓取结果摘要
    fetches: dict[str, dict[str, Any]] = field(default_factory=dict)
    # 每次 search_news 的调用记录，用于最终汇报「搜了什么、拿到多少」
    searches: list[dict[str, Any]] = field(default_factory=list)
    _seq: int = 0

    # ------------------------------------------------------------ 候选管理

    def next_id(self) -> str:
        self._seq += 1
        return f"n{self._seq}"

    def is_known(self, url: str) -> bool:
        """该 URL 是否本轮检索到过（简报链接白名单的依据）。"""
        return canonical_url(url) in self.index

    def known_urls(self) -> list[str]:
        """本轮检索到的全部原始链接（保持登记顺序）。"""
        return [item["url"] for item in self.items.values()]

    def item_by_url(self, url: str) -> dict[str, Any] | None:
        news_id = self.index.get(canonical_url(url))
        return self.items.get(news_id) if news_id else None

    def register_item(self, item: dict[str, Any]) -> str | None:
        """登记一条候选新闻，返回其 news_id（已存在则返回 None）。"""
        key = canonical_url(str(item.get("url") or ""))
        if not key or key in self.index:
            return None
        news_id = self.next_id()
        payload = {**item, "news_id": news_id}
        self.items[news_id] = payload
        self.index[key] = news_id
        return news_id

    # ------------------------------------------------------------ 抓取记录

    def remember_fetch(self, url: str, summary: dict[str, Any]) -> None:
        key = canonical_url(url)
        if key:
            self.fetches[key] = summary

    @property
    def fetched_urls(self) -> list[str]:
        return [str(payload.get("url") or "") for payload in self.fetches.values()]

    # ------------------------------------------------------------ 统计

    def record_search(self, payload: dict[str, Any]) -> None:
        self.searches.append(payload)

    def to_summary(self) -> dict[str, Any]:
        """给接口层/日志用的运行摘要。"""
        return {
            "user_id": self.user_id,
            "run_id": self.run_id,
            "candidates": len(self.items),
            "fetched": len(self.fetches),
            "searches": len(self.searches),
        }
