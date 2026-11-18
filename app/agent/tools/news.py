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
| save_briefing | 把写好的简报入库并校验 | 不改写模型写的内容 |

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
from app.core.config import settings
from app.core.exceptions import AppError
from app.news.article import extract_article
from app.news.briefing import parse_briefing
from app.news.feeds import list_feed_ids
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
        min_length=1,
        max_length=80,
        description=(
            "搜索关键词，可以是一个概念、产品名或短语，例如 'AI Agent'、'MCP 协议'、'大模型 编程'。"
            "支持中英文；多个词用空格分隔时，命中过半即算相关。"
        ),
    )
    date_range: str = Field(
        default="today",
        description=(
            "时间范围：today（今天，默认）/ yesterday / last_24h / 3d / 7d / "
            "2026-11-18（某一天）/ 2026-11-17:2026-11-18（区间）"
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
    max_items: int = Field(
        default=12,
        ge=1,
        le=30,
        description="最多返回多少条候选新闻（已按相关度与时间排序）",
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
        default=None, max_length=255, description="已导出的 Markdown 文件在工作区内的相对路径（可选）"
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
        "按关键词和时间范围搜索 AI 新闻（聚合多个 RSS 源）。返回标题、来源、发布时间、摘要与原始链接，"
        "并自动过滤时间范围外、命中排除词以及重复的条目。一次搜索只覆盖一个关键词，"
        "多个主题应分多次搜索；找不到相关内容时如实返回 0 条。"
    )
)
def search_news(args: SearchNewsArgs, ctx: ToolContext) -> ToolResult:
    """检索新闻候选。

    Args:
        args: 关键词、时间范围、排除词、源限定、条数上限等。
        ctx: 执行上下文（其 news_state 承载跨调用去重）。

    Returns:
        ToolResult，data.items 为按相关度排序的候选新闻（含 news_id）。
    """
    try:
        window = parse_date_range(args.date_range)
    except DateRangeError as exc:
        return ToolResult.failure(tool="search_news", code=ERR_INVALID_ARGUMENTS, message=str(exc))

    query = build_query(args.keyword)
    if not query.tokens:
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

        combined = f"{item.title} {item.summary} {' '.join(item.categories)}"
        hit_exclude = find_excluded(combined, args.exclude_keywords)
        if hit_exclude:
            excluded.append({"title": item.title, "url": item.link, "reason": f"命中排除词「{hit_exclude}」"})
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

    # 先按「相关度 → 时间」排序，再做去重：这样重复条目里留下的是信息更全/更相关的那条
    candidates.sort(key=lambda entry: (entry["match_score"], entry["published"] or ""), reverse=True)

    selected: list[dict[str, Any]] = []
    for entry in candidates:
        if len(selected) >= args.max_items:
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
        "feeds_ok": len(result.ok_sources),
        "feeds_failed": len(result.failed_sources),
        "scanned": len(result.items),
        "matched": len(selected),
        "duplicates_removed": len(duplicates),
        "excluded_by_keyword": len(excluded),
        "out_of_date_range": out_of_range,
        "undated": undated,
        "unmatched": unmatched,
    }

    summary_parts = [
        f"搜索「{args.keyword}」（{window.label}）：{len(result.ok_sources)}/{len(result.sources)} 个源可用，"
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
        summary_parts.append(f"另有 {len(candidates) - len(selected)} 条因条数上限未返回")

    if not selected:
        summary_parts.append("没有符合条件的新新闻——可换关键词/同义说法、放宽时间范围或调整排除词")

    if result.failed_sources:
        summary_parts.append(
            f"（{len(result.failed_sources)} 个源抓取失败：{'、'.join(r.name for r in result.failed_sources)}）"
        )

    payload = {
        "keyword": args.keyword,
        "date_range": window.to_dict(),
        "provider": provider.name,
        "items": selected,
        "stats": stats,
        "duplicates": duplicates[:10],
        "excluded": excluded[:10],
        "source_reports": [report.to_dict() for report in result.sources],
        "truncated": truncated,
    }

    state.record_search(
        {
            "keyword": args.keyword,
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
        "把写好的新闻简报登记到数据库（同一用户同一天只保留一份，重复提交会覆盖旧版本）。"
        "提交前会校验结构完整性与链接可信度：缺少必需章节、条目缺链接、"
        "或出现本轮检索之外凭空出现的链接时会被拒收，并在返回中说明缺什么。"
    )
)
def save_briefing(args: SaveBriefingArgs, ctx: ToolContext) -> ToolResult:
    """校验并保存简报。

    Args:
        args: 用户 ID、Markdown 全文、可选标题/日期/条数/文件路径。
        ctx: 执行上下文（提供数据库会话与候选链接白名单）。

    Returns:
        ToolResult，data 含 briefing_id、是否新建、条数与质量问题清单。
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

    summary = (
        f"{'新建' if created else '覆盖'}简报 #{briefing.id}（{_fmt_date(briefing_date)}，"
        f"{news_count} 条，用户 {user_id}）"
    )

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
            "link_count": len(report.links),
            "quality": report.to_dict(),
            "warnings": warnings,
        },
    )


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
