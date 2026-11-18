"""RSS 源目录。

一个源一条记录，包含 ID、展示名、地址、语种与定位。
`id` 是给 LLM 用的稳定标识（`search_news(sources=["qbitai"])`），
中文名只用于展示，不参与匹配——模型可能把名字写得不完全一致。

为什么把源清单放代码里而不是数据库：
源清单属于「部署配置」，改动频率低、需要跟代码一起评审（可用性、版权、语种）；
用户级个性化应该体现在「关注哪些话题」，而不是「订阅哪些源」。
需要覆盖时用环境变量 `NEWS_FEEDS=qbitai,openai` 即可，无需改代码。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import settings


@dataclass(frozen=True, slots=True)
class FeedSource:
    """一个 RSS/Atom 订阅源。"""

    id: str
    name: str
    url: str
    lang: str  # zh / en
    category: str  # 展示用分类，便于模型按领域偏好选择
    note: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "name": self.name,
            "lang": self.lang,
            "category": self.category,
            "note": self.note,
        }


# 默认源集合：中文 AI/科技 + 海外一线 AI 官方与媒体。
# 选取原则：公开可抓、更新频繁、正文可解析（2026-11 实测可用）。
FEED_CATALOG: tuple[FeedSource, ...] = (
    FeedSource("qbitai", "量子位", "https://www.qbitai.com/feed", "zh", "AI 资讯", "中文 AI 垂直媒体，更新最快"),
    FeedSource("leiphone", "雷锋网", "https://www.leiphone.com/feed", "zh", "AI/科技", "中文科技媒体"),
    FeedSource("geekpark", "极客公园", "https://www.geekpark.net/rss", "zh", "科技产品", "中文科技媒体"),
    FeedSource("ithome", "IT之家", "https://www.ithome.com/rss/", "zh", "科技综合", "综合科技快讯，AI 相关条目较多"),
    FeedSource("openai", "OpenAI News", "https://openai.com/news/rss.xml", "en", "大模型厂商", "OpenAI 官方动态"),
    FeedSource("googleai", "Google AI Blog", "https://blog.google/technology/ai/rss/", "en", "大模型厂商", "Google AI 官方博客"),
    FeedSource("huggingface", "Hugging Face Blog", "https://huggingface.co/blog/feed.xml", "en", "开源生态", "开源模型与工具"),
    FeedSource("techcrunch-ai", "TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/", "en", "AI 商业", "海外 AI 行业新闻"),
    FeedSource("simonw", "Simon Willison", "https://simonwillison.net/atom/everything/", "en", "Agent/工程实践", "LLM 工具与 Agent 实践"),
    FeedSource("marktechpost", "MarkTechPost", "https://www.marktechpost.com/feed/", "en", "AI 研究", "论文与模型解读"),
)

FEEDS_BY_ID: dict[str, FeedSource] = {feed.id: feed for feed in FEED_CATALOG}


def _parse_feed_ids(raw: str) -> list[str]:
    """把逗号分隔的源 ID 解析为列表（同时兼容中文名 → ID）。"""
    ids: list[str] = []
    for token in (raw or "").replace("，", ",").split(","):
        value = token.strip()
        if not value:
            continue
        lowered = value.lower()
        if lowered in FEEDS_BY_ID:
            ids.append(lowered)
            continue
        # 允许直接写中文名：模型/用户更可能写「量子位」而不是「qbitai」
        for feed in FEED_CATALOG:
            if feed.name == value:
                ids.append(feed.id)
                break
    return ids


def resolve_feeds(ids: list[str] | None = None) -> list[FeedSource]:
    """把源 ID 列表解析为源对象列表。

    优先级：显式入参 → 环境变量 NEWS_FEEDS → 内置默认集合。
    非法 ID 会被忽略（不抛错）——LLM 偶尔会编一个不存在的源名，
    让它拿到「剩下的可用源」比直接失败更有帮助。
    """
    if ids:
        resolved = [FEEDS_BY_ID[item.lower()] for item in ids if item.lower() in FEEDS_BY_ID]
        if resolved:
            return resolved

    configured = _parse_feed_ids(settings.NEWS_FEEDS)
    if configured:
        return [FEEDS_BY_ID[item] for item in configured]

    return list(FEED_CATALOG)


def list_feed_ids() -> list[str]:
    """当前生效的源 ID（用于工具描述与文档）。"""
    return [feed.id for feed in resolve_feeds()]
