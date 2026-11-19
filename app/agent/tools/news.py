"""新闻业务工具：get_user_preferences / search_news / fetch_article / save_briefing。

和第二阶段的文件工具完全同构——同样是
`@tool_registry.tool` 装饰器 + Pydantic 参数模型 + `(args, ctx) -> ToolResult`，
因此它们和 list_dir / read_file / bash **共用同一个注册表和同一个 Agent 主循环**，
主循环不需要为「新闻」写任何分支。

四个工具的分工（也是给模型的语义边界）：

| 工具 | 解决的问题 | 不做什么 |
| --- | --- | --- |
| get_user_preferences | 用户到底关心什么 | 不替模型决定搜什么词 |
| search_news | 今天有哪些候选新闻 | 不判断「重要不重要」 |
| fetch_article | 这条新闻到底讲了什么 | 抓不到就认输，绝不编 |
| save_briefing | 一次交付：校验 + 入库 + 导出 Markdown | 不改写模型写的内容 |

三条硬约束（都在代码里兜住，不靠模型自觉）：
1. 简报里的链接必须来自本轮真实检索/抓取（`save_briefing` 校验白名单）；
2. 简报结构不完整直接拒收并回传缺失项（`parse_briefing`）；
3. 同一用户同一天只保留一份（数据库唯一键 + upsert）。
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field

from app.agent.context import ToolContext
from app.agent.registry import tool_registry
from app.agent.result import ERR_INVALID_ARGUMENTS, ERR_TOOL_ERROR, ToolResult
from app.agent.safety import SafetyViolation
from app.core.config import settings
from app.core.exceptions import AppError
from app.news.article import extract_article
from app.news.briefing import parse_briefing
from app.news.feeds import list_feed_ids
from app.news.dedupe import canonical_url
from app.news.filter import DateRangeError, build_query, find_excluded, match_item, parse_date_range
from app.news import http as news_http
from app.news.providers import get_provider
from app.news.rss import clean_html
from app.services import briefing_service

logger = logging.getLogger(__name__)

ERR_NEWS_SOURCE = "news_source_unavailable"
ERR_NEWS_FETCH = "article_fetch_failed"
ERR_BRIEFING_INVALID = "briefing_invalid"
ERR_BRIEFING_UNVERIFIED = "briefing_unverified_links"


def _fmt_time(moment: datetime | None) -> str:
    return moment.strftime("%Y-%m-%d %H:%M") if moment else ""


def _fmt_date(day: date) -> str:
    return day.isoformat()


# ---------------------------------------------------------------- 参数模型


class GetPreferencesArgs(BaseModel):
    """get_user_preferences 的参数。"""

    user_id: int = Field(ge=1, description="要读取订阅偏好的用户 ID")


class SearchNewsArgs(BaseModel):
    """search_news 的参数。"""

    keyword: str = Field(
        default="",
        max_length=80,
        description=(
            "搜索关键词，可以是一个概念、产品名或短语，例如 'AI Agent'、'MCP 协议'、'大模型 编程'。"
            "支持中英文；多个词用空格分隔时，命中过半即算相关。"
            "**留空表示「浏览模式」**：不按关键词筛选，直接按发布时间倒序列出时间窗内的全部候选"
            "（只给标题/来源/时间/链接），用于先看清今天有什么、再决定读哪几条。"
        ),
    )
    date_range: str = Field(
        default="last_24h",
        description=(
            "时间范围：last_24h（过去 24 小时，默认）/ today（今天 0 点之后）/ yesterday / 3d / 7d / "
            "2026-11-18（某一天）/ 2026-11-17:2026-11-18（区间）。"
            "默认用 last_24h 是因为资讯站按「滚动 24 小时」更新，"
            "而简报多在早上生成——那时「今天 0 点之后」几乎没有内容。"
        ),
    )
    exclude_keywords: list[str] = Field(
        default_factory=list,
        description="排除关键词，标题/摘要命中的新闻会被直接过滤（通常来自用户订阅里的排除项）",
    )
    sources: list[str] = Field(
        default_factory=list,
        description=f"限定检索的源 ID，留空表示全部；可用：{', '.join(list_feed_ids())}",
    )
    provider: str | None = Field(
        default=None,
        description=(
            "搜索提供方名称，留空使用服务端配置（默认 rss）。"
            "一般不需要填写，仅在服务端另外注册了搜索服务（如 websearch / newsapi）时才用得上。"
        ),
    )
    max_items: int | None = Field(
        default=None,
        ge=1,
        le=60,
        description=(
            "最多返回多少条候选新闻。留空时：关键词模式返回 12 条，"
            f"浏览模式返回 {settings.NEWS_BROWSE_MAX_ITEMS} 条（浏览要的是全景，条数更宽）"
        ),
    )
    only_new: bool = Field(
        default=True,
        description="是否只返回本轮之前没出现过的新闻（用于多次搜索时自动去重）；设为 false 可回看已出现过的条目",
    )
    include_undated: bool = Field(
        default=False,
        description="是否包含发布时间无法确认的条目（默认排除，避免把旧闻当成今日新闻）",
    )


class FetchArticleArgs(BaseModel):
    """fetch_article 的参数。"""

    url: str = Field(min_length=8, max_length=2000, description="新闻详情页的原始链接（必须来自 search_news 的返回）")
    max_chars: int = Field(
        default=settings.NEWS_ARTICLE_MAX_CHARS,
        ge=400,
        le=20000,
        description="返回正文的最大字符数，超出会截断",
    )


class SaveBriefingArgs(BaseModel):
    """save_briefing 的参数。"""

    user_id: int = Field(ge=1, description="简报所属用户 ID")
    content: str = Field(min_length=200, max_length=60000, description="简报 Markdown 全文")
    title: str | None = Field(default=None, max_length=200, description="简报标题，留空则取正文一级标题")
    briefing_date: str | None = Field(
        default=None,
        description="简报日期 YYYY-MM-DD，留空则取正文中的日期，再留空则取今天",
    )
    news_count: int | None = Field(default=None, ge=0, le=100, description="简报收录的新闻条数，留空则自动统计")
    file_path: str | None = Field(
        default=None,
        max_length=255,
        description=(
            "Markdown 要导出到工作区内的相对路径，留空则自动用 briefings/<日期>-user<用户ID>.md。"
            "本工具会自己落盘，调用方不需要再调 write_file 写一遍同样的内容。"
        ),
    )


# ---------------------------------------------------------------- 工具：读取偏好


@tool_registry.tool(
    description=(
        "读取指定用户的订阅偏好（身份/职业、关注话题、关键词、排除关键词）。"
        "生成个性化简报前应先调用它拿到真实的偏好原文，而不是依赖任务描述里的转述。"
    )
)
def get_user_preferences(args: GetPreferencesArgs, ctx: ToolContext) -> ToolResult:
    """从 SQLite 读取用户订阅偏好。

    Args:
        args: 用户 ID。
        ctx: 执行上下文（提供数据库会话工厂）。

    Returns:
        ToolResult，data 为画像字段；未配置偏好时三个列表为空并附提示。
    """
    with ctx.db_session() as db:
        try:
            profile = briefing_service.get_user_profile(db, args.user_id)
        except AppError as exc:
            return ToolResult.failure(tool="get_user_preferences", code=str(exc.code), message=exc.message)

    payload = profile.to_dict()
    if not profile.focus:
        payload["hint"] = "该用户还没有设置关注话题与关键词，可按「泛 AI 行业新闻」处理，或提醒用户先完善订阅偏好。"

    summary = (
        f"用户 {profile.nickname}（{profile.identity}）关注："
        f"{'、'.join(profile.focus) if profile.focus else '未设置'}"
    )
    if profile.exclude_keywords:
        summary += f"；排除：{'、'.join(profile.exclude_keywords)}"

    return ToolResult.success(tool="get_user_preferences", summary=summary, data=payload)


# ---------------------------------------------------------------- 工具：搜索新闻


@tool_registry.tool(
    description=(
        "检索 AI 新闻候选（聚合多个 RSS 源），自动过滤时间范围外、命中排除词与重复条目，"
        "返回标题、来源、发布时间、摘要与原始链接。两种用法："
        "① 不传 keyword（推荐先做一次）＝浏览模式：按发布时间倒序列出时间窗内全部候选，"
        "用来一次看清「今天有什么」，再挑几条读正文；"
        "② 传 keyword＝关键词模式：只返回相关条目，一次一个关键词，用于在清单里定向补充。"
        "返回里的 `pool_window` 是时间窗内全站候选总数（不受关键词影响）："
        "若它已接近本轮累计返回条数，说明换同义词也搜不出新东西，应基于现有候选收尾。"
    )
)
def search_news(args: SearchNewsArgs, ctx: ToolContext) -> ToolResult:
    """检索新闻候选（关键词模式 / 浏览模式）。

    两种模式共用同一套「抓取 → 时间过滤 → 排除词 → 去重 → 登记候选池」流程，
    区别只有一个：是否按关键词做相关度筛选。

    Args:
        args: 关键词（留空 = 浏览模式）、时间范围、排除词、源限定、条数上限等。
        ctx: 执行上下文（其 news_state 承载跨调用去重）。

    Returns:
        ToolResult，data.items 为候选新闻（含 news_id）。
    """
    try:
        window = parse_date_range(args.date_range)
    except DateRangeError as exc:
        return ToolResult.failure(tool="search_news", code=ERR_INVALID_ARGUMENTS, message=str(exc))

    # 浏览模式：不带关键词 → 不做相关度筛选，直接按时间倒序列出时间窗内全部候选。
    # 这是为了替代「反复换关键词试」：一次调用就把今天有什么摆在模型面前，
    # 让它挑几条读原文，而不是搜十几次才凑齐素材。
    raw_keyword = (args.keyword or "").strip()
    browse = not raw_keyword
    query = None if browse else build_query(raw_keyword)
    if query is not None and not query.tokens:
        return ToolResult.failure(tool="search_news", code=ERR_INVALID_ARGUMENTS, message="关键词不能为空")

    # provider 由配置/参数决定：默认 rss，注册了新搜索服务后改这一处即可切换，
    # 工具签名、Agent 主循环与 prompt 都不用动。
    provider = get_provider(args.provider or settings.NEWS_PROVIDER)
    result = provider.search(args.sources or None)

    if not result.sources:
        return ToolResult.failure(
            tool="search_news",
            code=ERR_NEWS_SOURCE,
            message="没有可用的新闻源，请检查 NEWS_FEEDS 配置或网络",
        )

    if not result.ok_sources:
        errors = "；".join(f"{report.name}: {report.error}" for report in result.failed_sources[:5])
        return ToolResult.failure(
            tool="search_news",
            code=ERR_NEWS_SOURCE,
            message=f"全部 {len(result.sources)} 个新闻源都抓取失败（{errors}）。请如实告知用户「今日新闻源不可用」，不要编造新闻。",
            data={"sources": [report.to_dict() for report in result.sources]},
        )

    state = ctx.news_state
    duplicates: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    # 时间窗内「全站候选」的去重集合（不受关键词/排除词影响）：
    # 这是给模型判断「还要不要再换词搜」的依据
    window_urls: set[str] = set()
    out_of_range = 0
    undated = 0
    unmatched = 0

    for item in result.items:
        if item.published_at is None:
            undated += 1
            if not args.include_undated:
                continue

        if item.published_at is not None and not window.contains(item.published_at):
            out_of_range += 1
            continue

        window_urls.add(canonical_url(item.link))

        combined = f"{item.title} {item.summary} {' '.join(item.categories)}"
        hit_exclude = find_excluded(combined, args.exclude_keywords)
        if hit_exclude:
            excluded.append({"title": item.title, "url": item.link, "reason": f"命中排除词「{hit_exclude}」"})
            continue

        if browse:
            # 浏览模式的角色是「目录」，不是「正文」：只给一眼能判断的字段。
            # 这里刻意不带摘要——60 条 × 120 字的摘要在上下文里会被反复重发，
            # 实测把一次生成的耗时从 60 秒推高到 74 秒；标题本身通常已够筛，
            # 需要细节时再 fetch_article 或按关键词补搜。
            candidates.append(
                {
                    "title": item.title,
                    "url": item.link,
                    "source": item.source_name,
                    "source_id": item.source_id,
                    "published": _fmt_time(item.published_at),
                    "published_unknown": item.published_at is None,
                }
            )
            continue

        match = match_item(query, title=item.title, summary=item.summary, categories=item.categories)
        if not match.ok:
            unmatched += 1
            continue

        candidates.append(
            {
                "title": item.title,
                "url": item.link,
                "source": item.source_name,
                "source_id": item.source_id,
                "published": _fmt_time(item.published_at),
                "published_unknown": item.published_at is None,
                "summary": clean_html(item.summary, max_chars=220),
                "categories": item.categories,
                "match_score": match.score,
                "matched_terms": match.matched_tokens,
            }
        )

    # 排序：关键词模式按「相关度 → 时间」，浏览模式纯按时间倒序（最新在前）
    if browse:
        candidates.sort(key=lambda entry: entry["published"] or "", reverse=True)
    else:
        candidates.sort(key=lambda entry: (entry["match_score"], entry["published"] or ""), reverse=True)

    # 浏览模式一次给「全景」，但必须有硬上限：清单里的每一条都会被后续每一轮重新发送，
    # 60 条标题看着不多，累计到十几轮就是十几万字符。模型要求更多时也不放大（分源浏览更划算）；
    # 关键词模式维持 12 条，避免一次塞太多不相干的条目。
    if browse:
        limit = min(args.max_items or settings.NEWS_BROWSE_MAX_ITEMS, settings.NEWS_BROWSE_MAX_ITEMS)
    else:
        limit = args.max_items or settings.NEWS_SEARCH_MAX_ITEMS

    selected: list[dict[str, Any]] = []
    for entry in candidates:
        if len(selected) >= limit:
            break

        if args.only_new:
            duplicate = state.deduper.check(entry["title"], entry["url"], source=entry["source"])
            if duplicate is not None:
                duplicates.append(duplicate.to_dict())
                continue

        news_id = state.register_item(entry)
        if news_id is None:
            # 本轮之前已登记过（例如 only_new=false 时回看）——复用原编号并标注
            existing = state.item_by_url(entry["url"])
            if existing is None:
                continue
            entry["news_id"] = existing["news_id"]
            entry["already_returned"] = True
            selected.append(entry)
            continue

        entry["news_id"] = news_id
        selected.append(entry)

    truncated = len(candidates) > len(selected)

    stats = {
        "mode": "browse" if browse else "keyword",
        "feeds_ok": len(result.ok_sources),
        "feeds_failed": len(result.failed_sources),
        "scanned": len(result.items),
        "matched": len(selected),
        # 时间窗内全站候选规模（去重）+ 本轮累计已返回多少：模型据此判断「够不够」，
        # 不然它会一直换关键词试，试到 20 多次才发现今天根本没有更多新闻
        "pool_window": len(window_urls),
        "returned_so_far": len(state.items),
        "duplicates_removed": len(duplicates),
        "excluded_by_keyword": len(excluded),
        "out_of_date_range": out_of_range,
        "undated": undated,
        "unmatched": unmatched,
    }

    if browse:
        summary_parts = [
            f"浏览（{window.label}，不按关键词筛选）：{len(result.ok_sources)}/{len(result.sources)} 个源可用，"
            f"扫描 {len(result.items)} 条，返回最新的 {len(selected)} 条"
        ]
    else:
        summary_parts = [
            f"搜索「{raw_keyword}」（{window.label}）：{len(result.ok_sources)}/{len(result.sources)} 个源可用，"
            f"扫描 {len(result.items)} 条，返回 {len(selected)} 条"
        ]
    details: list[str] = []
    if duplicates:
        details.append(f"去重 {len(duplicates)} 条")
    if excluded:
        details.append(f"排除词过滤 {len(excluded)} 条")
    if out_of_range:
        details.append(f"非时间范围 {out_of_range} 条")
    if undated:
        details.append(f"无发布时间 {undated} 条")
    if details:
        summary_parts.append("，".join(details))
    if truncated:
        rest = len(candidates) - len(selected)
        if browse:
            summary_parts.append(
                f"另有 {rest} 条未返回（清单有上限，避免塞满上下文）——"
                "需要更多就用 sources 参数分源浏览，或按关键词补搜"
            )
        else:
            summary_parts.append(f"另有 {rest} 条因条数上限未返回")

    # 给模型一个「该不该收手」的硬数字：窗口内全站就这么些条，
    # 继续换同义词也不会冒出新新闻
    summary_parts.append(
        f"窗口内全站候选 {len(window_urls)} 条（去重后），本轮累计已返回 {len(state.items)} 条"
    )

    if not selected:
        if browse:
            summary_parts.append("时间窗内没有可用候选——可放宽时间范围（如 3d）或补充关键词")
        else:
            summary_parts.append("没有符合条件的新新闻——可换关键词/同义说法、放宽时间范围或调整排除词")

    if result.failed_sources:
        summary_parts.append(
            f"（{len(result.failed_sources)} 个源抓取失败：{'、'.join(r.name for r in result.failed_sources)}）"
        )

    payload = {
        "mode": "browse" if browse else "keyword",
        "keyword": raw_keyword or None,
        "date_range": window.to_dict(),
        "provider": provider.name,
        "items": selected,
        "stats": stats,
        "duplicates": duplicates[:10],
        "excluded": excluded[:10],
        "source_reports": [report.to_dict() for report in result.sources],
        "truncated": truncated,
    }
    if browse:
        # 清单只给标题：明确告诉模型「要细节请读正文或补搜」，避免它以为这就是全部信息
        payload["note"] = (
            "浏览模式只返回标题/来源/时间/链接，不含摘要。"
            "挑中哪几条请用 fetch_article 读正文；清单里没有的方向再用关键词补搜。"
        )

    state.record_search(
        {
            "keyword": raw_keyword or "（浏览模式）",
            "date_range": window.label,
            "returned": len(selected),
            "scanned": len(result.items),
        }
    )

    return ToolResult.success(
        tool="search_news",
        summary="；".join(summary_parts),
        data=payload,
        provider=provider.name,
    )


# ---------------------------------------------------------------- 工具：抓取正文


@tool_registry.tool(
    description=(
        "抓取指定新闻链接的详情页，提取标题、来源、发布时间与正文。"
        "链接打不开或解析不出正文时会明确失败，此时**必须**如实说明「正文未获取」，"
        "禁止凭标题推测内容。正文较长时可用 max_chars 控制返回长度。"
    )
)
def fetch_article(args: FetchArticleArgs, ctx: ToolContext) -> ToolResult:
    """抓取新闻正文。

    Args:
        args: 文章链接与最大字符数。
        ctx: 执行上下文。

    Returns:
        ToolResult，data.content 为正文/摘要，data.content_kind 标注完整程度。
    """
    # 同一次运行里重复读同一篇：直接复用已抓到的正文。
    # 实测模型会为「确认一下」把同一篇读两三遍，每次都是 1~3 秒的纯等待；
    # 正文在这一轮内不会变，重复下载没有任何信息增量。
    cached = ctx.news_state.cached_fetch(args.url)
    if cached is not None:
        title = cached.get("title") or "无标题"
        return ToolResult.success(
            tool="fetch_article",
            summary=(
                f"《{title}》本轮已抓取过，正文就是上一次返回的那份，"
                "请直接使用，不要为同一个 URL 再调用 fetch_article（未重复下载）"
            ),
            data={**cached, "cached": True},
        )

    outcome = news_http.fetch_text(
        args.url,
        timeout=settings.NEWS_FETCH_TIMEOUT,
        max_bytes=settings.NEWS_HTTP_MAX_BYTES,
        accept=news_http.DEFAULT_ACCEPT,
    )

    if not outcome.ok:
        return ToolResult.failure(
            tool="fetch_article",
            code=ERR_NEWS_FETCH,
            message=(
                f"无法获取该链接内容（{outcome.error}）。请勿编造文章内容，"
                "可改读同一条新闻的其他来源，或在简报中标注「原文无法访问」。"
            ),
            data={
                "url": args.url,
                "status_code": outcome.status_code,
                "error": outcome.error,
                "content_available": False,
            },
        )

    content_type = (outcome.content_type or "").lower()
    if content_type and not any(marker in content_type for marker in ("html", "xml", "text", "json")):
        return ToolResult.failure(
            tool="fetch_article",
            code=ERR_NEWS_FETCH,
            message=f"该链接返回的是非文本内容（{content_type}），无法提取正文",
            data={"url": args.url, "content_type": content_type, "content_available": False},
        )

    article = extract_article(outcome.text, outcome.final_url or args.url, max_chars=args.max_chars)

    if article.content_kind == "none" or not article.content.strip():
        return ToolResult.failure(
            tool="fetch_article",
            code=ERR_NEWS_FETCH,
            message=(
                "网页能打开，但没有解析到可用的正文或摘要（可能是图片页、登录墙或纯脚本渲染页面）。"
                "请勿推测内容，可换来源或调整搜索。"
            ),
            data={
                "url": args.url,
                "final_url": outcome.final_url,
                "title": article.title,
                "content_available": False,
                "warnings": article.warnings,
            },
        )

    # 正文也登记到候选池：它同样是「本轮真实检索到的链接」，允许写进简报
    state = ctx.news_state
    if not state.is_known(args.url):
        state.register_item(
            {
                "title": article.title or "(无标题)",
                "url": outcome.final_url or args.url,
                "source": article.site_name or "",
                "published": _fmt_time(article.published_at),
                "summary": article.description[:200],
                "from_fetch": True,
            }
        )

    kind_label = {"full": "完整正文", "excerpt": "页面摘录", "summary": "站点摘要"}.get(article.content_kind, "未知")
    warnings = list(article.warnings)
    if article.truncated:
        warnings.append(f"正文超过 {args.max_chars} 字符，已截断")

    payload = {
        "url": outcome.final_url or args.url,
        "requested_url": args.url,
        "title": article.title,
        "source": article.site_name,
        "author": article.author,
        "published": _fmt_time(article.published_at),
        "published_raw": article.published_raw,
        "published_unknown": article.published_at is None,
        "content": article.content,
        "content_kind": article.content_kind,
        "word_count": article.word_count,
        "description": article.description,
        "truncated": article.truncated,
        "warnings": warnings,
        "status_code": outcome.status_code,
    }

    state.remember_fetch(
        args.url,
        {
            "url": payload["url"],
            "title": article.title,
            "content_kind": article.content_kind,
            "word_count": article.word_count,
        },
    )
    # 连同正文一起缓存：下一次对同一 URL 的调用直接命中，不再发网络请求
    state.remember_payload(args.url, payload)

    summary = (
        f"已获取《{article.title or '无标题'}》（{article.site_name or '来源未知'}，"
        f"{kind_label}，约 {article.word_count} 字）"
    )
    if article.published_at is None:
        summary += "；发布时间未获取到"
    if warnings:
        summary += f"；注意：{warnings[0]}"

    return ToolResult.success(tool="fetch_article", summary=summary, data=payload)


# ---------------------------------------------------------------- 工具：保存简报


@tool_registry.tool(
    description=(
        "把写好的新闻简报一次性交付：结构校验 → 登记到数据库 → 导出 Markdown 文件到工作区。"
        "同一用户同一天只保留一份，重复提交会覆盖旧版本。"
        "提交前会校验结构完整性与链接可信度：缺少必需章节、条目缺链接、"
        "或出现本轮检索之外凭空出现的链接时会被拒收，并在返回中说明缺什么。"
        "**只需调用本工具一次，不要再用 write_file 写同一份内容。**"
    )
)
def save_briefing(args: SaveBriefingArgs, ctx: ToolContext) -> ToolResult:
    """校验、入库并导出 Markdown。

    为什么要自己落盘（而不是让模型先 write_file 再 save_briefing）：
    两次调用意味着模型要**把同一份 6000 字的正文完整输出两遍**，
    实测这两步占了单次生成约 70% 的耗时（23s + 13s，其中绝大部分是
    重复的 out-token 生成，而不是磁盘 IO）。合并成一次调用后，
    产物完全一致，但少了一整轮大文本生成。

    Args:
        args: 用户 ID、Markdown 全文、可选标题/日期/条数/导出路径。
        ctx: 执行上下文（提供数据库会话、候选链接白名单与工作区）。

    Returns:
        ToolResult，data 含 briefing_id、是否新建、条数、导出路径与质量问题清单。
    """
    report = parse_briefing(args.content)

    if not report.ok:
        return ToolResult.failure(
            tool="save_briefing",
            code=ERR_BRIEFING_INVALID,
            message=f"简报未通过结构校验，请补齐后重新提交：{report.to_error_message()}",
            data=report.to_dict(),
        )

    # 链接白名单：只允许本轮真实检索/抓取到的 URL
    unknown_links = [link for link in report.links if not ctx.news_state.is_known(link)]
    if unknown_links:
        allowed = ctx.news_state.known_urls()[:20]
        return ToolResult.failure(
            tool="save_briefing",
            code=ERR_BRIEFING_UNVERIFIED,
            message=(
                f"简报中有 {len(unknown_links)} 个链接不在本轮检索结果内，可能是编造或转述错误："
                f"{'、'.join(unknown_links[:3])}。请只使用 search_news / fetch_article 返回过的链接。"
            ),
            data={"unknown_links": unknown_links[:10], "allowed_urls": allowed},
        )

    briefing_date = _resolve_date(args.briefing_date or report.briefing_date, ctx)
    if briefing_date is None:
        return ToolResult.failure(
            tool="save_briefing",
            code=ERR_INVALID_ARGUMENTS,
            message=f"无法识别简报日期：{args.briefing_date!r}，请用 YYYY-MM-DD 格式",
        )

    title = (args.title or report.title or f"{_fmt_date(briefing_date)} AI 新闻简报").strip()
    news_count = args.news_count if args.news_count is not None else report.news_count
    if report.declared_count is not None and args.news_count is None and report.declared_count != report.news_count:
        # 头部自报条数与实际条目数不一致：以实际条目为准，并提示模型核对
        news_count = report.news_count

    user_id = args.user_id or ctx.user_id
    if user_id is None:
        return ToolResult.failure(tool="save_briefing", code=ERR_INVALID_ARGUMENTS, message="缺少用户 ID")

    file_path = args.file_path or briefing_service.briefing_relative_path(user_id, briefing_date)

    with ctx.db_session() as db:
        try:
            profile = briefing_service.get_user_profile(db, user_id)
        except AppError as exc:
            return ToolResult.failure(tool="save_briefing", code=str(exc.code), message=exc.message)

        try:
            briefing, created = briefing_service.upsert_briefing(
                db,
                user_id=user_id,
                briefing_date=briefing_date,
                title=title,
                content=args.content,
                news_count=news_count,
                focus_topics=profile.focus,
                file_path=file_path,
                run_id=ctx.news_state.run_id,
            )
        except AppError as exc:
            return ToolResult.failure(tool="save_briefing", code=str(exc.code), message=exc.message)
        except Exception as exc:  # noqa: BLE001 - 兜住唯一键冲突等数据库层异常
            logger.exception("简报入库失败")
            return ToolResult.failure(
                tool="save_briefing",
                code=ERR_TOOL_ERROR,
                message=f"简报入库失败：{type(exc).__name__}: {exc}",
            )

    warnings: list[str] = []
    if report.declared_count is not None and report.declared_count != report.news_count:
        warnings.append(
            f"头部写的条数（{report.declared_count}）与实际条目数（{report.news_count}）不一致，已按实际条目入库"
        )
    if report.items_without_link:
        warnings.append(f"{len(report.items_without_link)} 条新闻缺链接")

    # 入库成功后再导出 Markdown。落盘失败不回滚入库——数据已经在库里，
    # 文件只是副本，把失败降级为 warning 让模型/调用方知情即可。
    exported, export_error = _export_briefing_markdown(args.content, file_path, ctx)
    if export_error:
        warnings.append(f"Markdown 导出失败：{export_error}")

    summary = (
        f"{'新建' if created else '覆盖'}简报 #{briefing.id}（{_fmt_date(briefing_date)}，"
        f"{news_count} 条，用户 {user_id}）"
    )
    if exported:
        summary += f"，已导出 {file_path}"
    else:
        summary += "，Markdown 未导出"

    return ToolResult.success(
        tool="save_briefing",
        summary=summary,
        data={
            "briefing_id": briefing.id,
            "created": created,
            "user_id": user_id,
            "briefing_date": _fmt_date(briefing_date),
            "title": title,
            "news_count": news_count,
            "file_path": file_path,
            "file_exported": exported,
            "link_count": len(report.links),
            "quality": report.to_dict(),
            "warnings": warnings,
        },
    )


def _export_briefing_markdown(
    content: str, file_path: str, ctx: ToolContext
) -> tuple[bool, str | None]:
    """把简报正文写到工作区内的 Markdown 文件。

    Args:
        content: Markdown 全文。
        file_path: 工作区内的相对路径。
        ctx: 执行上下文（提供工作区与写入上限）。

    Returns:
        (是否写入成功, 失败原因)；成功时失败原因为 None。
    """
    try:
        target = ctx.resolve(file_path)
    except SafetyViolation as exc:
        return False, exc.message

    if target.is_dir():
        return False, f"{file_path} 是一个目录"

    payload = content.encode("utf-8")
    if len(payload) > ctx.write_max_bytes:
        return False, f"内容 {len(payload)} 字节超过单次写入上限 {ctx.write_max_bytes} 字节"

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
    except OSError as exc:
        logger.warning("简报 Markdown 导出失败：%s", exc)
        return False, f"{type(exc).__name__}: {exc}"

    return True, None


def _resolve_date(raw: str | None, ctx: ToolContext) -> date | None:
    """把各种日期写法归一化成 date。"""
    text = (raw or "").strip()
    if not text:
        return date.today()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    # 「2026-11-18 的简报」这类写法：从文本里抠日期
    match = re.search(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})", text)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    return None
