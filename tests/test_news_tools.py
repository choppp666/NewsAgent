"""新闻工具的独立测试。

和第二阶段一样，直接调注册中心的 `execute()`，不经过 LLM：
这样看到的是「工具本身的行为」，网络被 `news_network` 夹具换成固定数据，
数据库用临时库，因此结果完全可重复。

覆盖的异常场景（对应第三阶段验收要求）：
- RSS 源访问失败（全部失败 / 部分失败）
- 新闻重复（同 URL 带参数、同标题转载）
- 正文无法获取（HTTP 错误 / 非 HTML / 页面无正文 / 本机地址）
- 当天没有相关新闻
- 简报结构不完整、链接凭空出现
"""

from __future__ import annotations

import sys
from collections.abc import Generator
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent import tool_registry  # noqa: E402
from app.agent.context import ToolContext  # noqa: E402
from app.core.exceptions import NotFoundError  # noqa: E402
from app.models.briefing import NewsBriefing  # noqa: E402
from app.schemas.subscription import SubscriptionUpsert  # noqa: E402
from app.schemas.user import UserCreate  # noqa: E402
from app.services import subscription_service, user_service  # noqa: E402


@pytest.fixture()
def ctx(engine: Engine, tmp_path: Path) -> ToolContext:
    """临时工作区 + 指向临时库的会话工厂。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    return ToolContext(workspace=workspace, db_session_factory=factory)


@pytest.fixture()
def seeded_user(engine: Engine) -> int:
    """建一个带订阅偏好的用户（A：AI Agent / MCP）。"""
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as db:
        user = user_service.create_user(
            db, UserCreate(nickname="林工", identity="AI Agent 应用开发工程师")
        )
        subscription_service.upsert_subscription(
            db,
            user.id,
            SubscriptionUpsert(
                topics=["AI Agent", "MCP"],
                keywords=["工具调用"],
                exclude_keywords=["融资"],
            ),
        )
        return user.id


def run_tool(name: str, arguments: dict, ctx: ToolContext):
    return tool_registry.execute(name, arguments, ctx)


def _briefing_markdown(url: str, *, source: str = "量子位", title: str = "OpenAI 开放 MCP 支持") -> str:
    """构造一份结构完整、链接可溯源的简报。"""
    return f"""# 每日 AI 新闻简报

- 简报日期：2026-11-18
- 用户关注方向：AI Agent、MCP
- 今日新闻数量：1

## 今日重点新闻

### {title}
- 新闻摘要：OpenAI 在 API 层支持 MCP，第三方工具可按统一描述接入。
- 核心事件：官方文档给出 Python 与 TypeScript 示例。
- 重要性分析：对 Agent 工具生态的标准化有直接影响。
- 新闻来源：{source}
- 原文链接：{url}
- 发布时间：2026-11-18 10:00

## 行业动态总结

工具协议标准化是本周最值得关注的变化，多个来源都提到接入成本下降；
这一段还包含足够的描述性文字，用于满足简报的最少字数要求。

## 今日值得关注

- 关注 MCP 服务端实现与鉴权方案。
"""


# ---------------------------------------------------------------- 偏好在工具里


def test_get_user_preferences_reads_sqlite(ctx: ToolContext, seeded_user: int) -> None:
    result = run_tool("get_user_preferences", {"user_id": seeded_user}, ctx)

    assert result.ok
    assert result.data["topics"] == ["AI Agent", "MCP"]
    assert result.data["keywords"] == ["工具调用"]
    assert result.data["exclude_keywords"] == ["融资"]
    assert result.data["focus"] == ["AI Agent", "MCP", "工具调用"]
    assert "MCP" in result.summary


def test_get_user_preferences_missing_user(ctx: ToolContext) -> None:
    result = run_tool("get_user_preferences", {"user_id": 999}, ctx)

    assert not result.ok
    assert result.error is not None and result.error.code == "404"


# ---------------------------------------------------------------- 搜索


def test_search_news_applies_all_filters(ctx: ToolContext, news_network) -> None:
    result = run_tool(
        "search_news",
        {"keyword": "AI Agent", "date_range": "today", "exclude_keywords": ["融资"], "max_items": 10},
        ctx,
    )

    assert result.ok, result.summary
    stats = result.data["stats"]
    items = result.data["items"]

    # 命中的条目必须带齐五要素（标题/来源/时间/摘要/链接）
    assert items
    for item in items:
        assert item["news_id"].startswith("n")
        assert item["title"] and item["url"] and item["source"]
        assert item["summary"] is not None

    # 排除词生效
    assert stats["excluded_by_keyword"] >= 1
    assert all("融资" not in item["title"] for item in items)
    # 昨天的旧闻被时间过滤掉
    assert stats["out_of_date_range"] >= 1
    # 同一标题的转载被去重
    assert stats["duplicates_removed"] >= 1
    # 无发布时间默认排除，但会统计出来
    assert stats["undated"] >= 1
    # 有源不可用（其余源返回 403），不能因此整体失败
    assert stats["feeds_failed"] >= 1 and stats["feeds_ok"] >= 1


def test_search_news_empty_result_is_honest(ctx: ToolContext, news_network) -> None:
    """当天没有相关新闻时返回 0 条，并提示换策略，而不是凑数。"""
    result = run_tool("search_news", {"keyword": "量子计算容错", "date_range": "today"}, ctx)

    assert result.ok
    assert result.data["items"] == []
    assert "没有符合条件" in result.summary


def test_search_news_second_call_returns_only_new(ctx: ToolContext, news_network) -> None:
    """跨调用去重：同一个关键词搜两次，第二次不应重复返回同一批新闻。"""
    first = run_tool("search_news", {"keyword": "AI Agent", "date_range": "today"}, ctx)
    second = run_tool("search_news", {"keyword": "Agent", "date_range": "today"}, ctx)

    first_urls = {item["url"] for item in first.data["items"]}
    second_urls = {item["url"] for item in second.data["items"]}

    assert first_urls
    assert not (first_urls & second_urls), "第二次搜索不应再次返回已出现过的新闻"
    assert ctx.news_state.searches[1]["keyword"] == "Agent"


def test_search_news_can_include_undated(ctx: ToolContext, news_network) -> None:
    result = run_tool(
        "search_news",
        {"keyword": "MCP", "date_range": "today", "include_undated": True},
        ctx,
    )

    titles = [item["title"] for item in result.data["items"]]
    assert any("没有时间戳" in title for title in titles)
    assert any(item["published_unknown"] for item in result.data["items"])


def test_search_news_all_sources_down(ctx: ToolContext, monkeypatch: pytest.MonkeyPatch) -> None:
    """所有源都挂掉时必须明确失败，并提醒模型不要编造新闻。"""
    from app.news import http as news_http

    def always_fail(url: str, **kwargs):
        return news_http.FetchOutcome(
            ok=False, url=url, final_url=url, status_code=0, text="",
            content_type="", size=0, error="连接被重置",
        )

    monkeypatch.setattr(news_http, "fetch_text", always_fail)

    result = run_tool("search_news", {"keyword": "AI Agent", "date_range": "today"}, ctx)

    assert not result.ok
    assert result.error is not None
    assert result.error.code == "news_source_unavailable"
    assert "不要编造" in result.error.message
    assert result.data["sources"]


def test_search_news_rejects_bad_date_range(ctx: ToolContext) -> None:
    result = run_tool("search_news", {"keyword": "AI", "date_range": "上个月"}, ctx)

    assert not result.ok
    assert result.error is not None and result.error.code == "invalid_arguments"
    assert "today" in result.error.message


# ---------------------------------------------------------------- 抓正文


def test_fetch_article_extracts_content(ctx: ToolContext, news_network) -> None:
    url = "https://www.qbitai.com/2026/11/mcp-openai.html"
    _feeds, _articles, meta = news_network

    result = run_tool("fetch_article", {"url": url}, ctx)

    assert result.ok, result.summary
    data = result.data
    assert data["title"].startswith("OpenAI 开放 MCP")
    assert data["source"] == "量子位"
    # 夹具里的时间是相对「现在」生成的，这里必须跟着用同一天的字符串，
    # 而不是写死某一天（写死的话第二天跑就必然失败）。
    assert data["published"].startswith(meta["today"].strftime("%Y-%m-%d"))
    assert data["content_kind"] == "full"
    assert "MCP" in data["content"]
    # 导航与版权文字不能混进正文
    assert "版权所有" not in data["content"]
    # 抓到的链接也进入候选池，允许写进简报
    assert ctx.news_state.is_known(url)
    assert ctx.news_state.fetches


def test_fetch_article_handles_http_error(ctx: ToolContext, news_network) -> None:
    result = run_tool("fetch_article", {"url": "https://www.ithome.com/ai/copilot-repo.html"}, ctx)

    assert not result.ok
    assert result.error is not None and result.error.code == "article_fetch_failed"
    assert result.data["content_available"] is False
    assert "请勿编造" in result.error.message


def test_fetch_article_rejects_non_html(ctx: ToolContext, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.news import http as news_http

    monkeypatch.setattr(
        news_http,
        "fetch_text",
        lambda url, **kwargs: news_http.FetchOutcome(
            ok=True, url=url, final_url=url, status_code=200, text="%PDF-1.7",
            content_type="application/pdf", size=8,
        ),
    )

    result = run_tool("fetch_article", {"url": "https://example.com/a.pdf"}, ctx)

    assert not result.ok
    assert "非文本内容" in result.error.message


def test_fetch_article_handles_page_without_content(ctx: ToolContext, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.news import http as news_http

    monkeypatch.setattr(
        news_http,
        "fetch_text",
        lambda url, **kwargs: news_http.FetchOutcome(
            ok=True, url=url, final_url=url, status_code=200,
            text="<html><body><div id='app'></div></body></html>",
            content_type="text/html", size=40,
        ),
    )

    result = run_tool("fetch_article", {"url": "https://example.com/empty"}, ctx)

    assert not result.ok
    assert result.data["content_available"] is False
    assert "请勿推测" in result.error.message


def test_fetch_article_blocks_localhost(ctx: ToolContext) -> None:
    """LLM 给的 URL 属于不可信输入，内网地址必须挡掉。"""
    result = run_tool("fetch_article", {"url": "http://127.0.0.1:8000/api/health"}, ctx)

    assert not result.ok
    assert "内网" in result.error.message


# ---------------------------------------------------------------- 简报入库


def test_save_briefing_creates_then_overwrites(ctx: ToolContext, seeded_user: int, news_network, engine: Engine) -> None:
    search = run_tool("search_news", {"keyword": "MCP", "date_range": "today"}, ctx)
    url = search.data["items"][0]["url"]
    content = _briefing_markdown(url)

    first = run_tool(
        "save_briefing",
        {"user_id": seeded_user, "content": content, "briefing_date": "2026-11-18"},
        ctx,
    )
    assert first.ok, first.summary
    assert first.data["created"] is True
    assert first.data["news_count"] == 1

    second = run_tool(
        "save_briefing",
        {"user_id": seeded_user, "content": content.replace("OpenAI 开放 MCP 支持", "OpenAI 开放 MCP 支持（更新）"),
         "briefing_date": "2026-11-18"},
        ctx,
    )
    assert second.ok
    assert second.data["created"] is False
    assert second.data["briefing_id"] == first.data["briefing_id"]

    factory = sessionmaker(bind=engine)
    with factory() as db:
        total = db.scalar(select(func.count()).select_from(NewsBriefing))
    assert total == 1, "同一用户同一天只能有一条简报"


def test_save_briefing_exports_markdown_itself(
    ctx: ToolContext, seeded_user: int, news_network
) -> None:
    """交付一步到位：入库的同时把 Markdown 写进工作区，调用方不必再 write_file。

    这条行为是性能优化的一部分：让模型把 6000 字正文只生成一次。
    """
    search = run_tool("search_news", {"keyword": "MCP", "date_range": "today"}, ctx)
    url = search.data["items"][0]["url"]
    content = _briefing_markdown(url)

    result = run_tool("save_briefing", {"user_id": seeded_user, "content": content}, ctx)

    assert result.ok, result.summary
    assert result.data["file_exported"] is True
    assert result.data["warnings"] == []

    exported = ctx.workspace / result.data["file_path"]
    assert exported.is_file()
    assert exported.read_text(encoding="utf-8") == content
    assert result.data["file_path"] in result.summary


def test_save_briefing_export_outside_workspace_degrades(
    ctx: ToolContext, seeded_user: int, news_network
) -> None:
    """导出路径越界时只降级为 warning —— 已经入库的数据不能因此视为失败。"""
    search = run_tool("search_news", {"keyword": "MCP", "date_range": "today"}, ctx)
    url = search.data["items"][0]["url"]

    result = run_tool(
        "save_briefing",
        {"user_id": seeded_user, "content": _briefing_markdown(url), "file_path": "../escape.md"},
        ctx,
    )

    assert result.ok, result.summary
    assert result.data["file_exported"] is False
    assert any("导出失败" in warning for warning in result.data["warnings"])
    assert not (ctx.workspace.parent / "escape.md").exists()


def test_save_briefing_rejects_incomplete_structure(ctx: ToolContext, seeded_user: int, news_network) -> None:
    search = run_tool("search_news", {"keyword": "MCP", "date_range": "today"}, ctx)
    url = search.data["items"][0]["url"]
    content = _briefing_markdown(url).replace("## 行业动态总结", "## 随便写写")

    result = run_tool("save_briefing", {"user_id": seeded_user, "content": content}, ctx)

    assert not result.ok
    assert result.error is not None and result.error.code == "briefing_invalid"
    assert "行业动态总结" in result.error.message
    assert result.data["missing_sections"]


def test_save_briefing_rejects_fabricated_links(ctx: ToolContext, seeded_user: int, news_network) -> None:
    """模型没搜到就编一个链接 —— 必须被白名单拦下。"""
    search = run_tool("search_news", {"keyword": "MCP", "date_range": "today"}, ctx)
    real_url = search.data["items"][0]["url"]
    fake_url = "https://www.qbitai.com/2026/11/this-is-made-up.html"

    result = run_tool(
        "save_briefing",
        {"user_id": seeded_user, "content": _briefing_markdown(fake_url)},
        ctx,
    )

    assert not result.ok
    assert result.error is not None and result.error.code == "briefing_unverified_links"
    assert fake_url in result.error.message
    # 必须给出可用链接，模型才知道怎么改
    assert real_url in result.data["allowed_urls"]


def test_save_briefing_requires_existing_user(ctx: ToolContext, news_network) -> None:
    search = run_tool("search_news", {"keyword": "MCP", "date_range": "today"}, ctx)
    url = search.data["items"][0]["url"]

    result = run_tool("save_briefing", {"user_id": 999, "content": _briefing_markdown(url)}, ctx)

    assert not result.ok
    assert result.error is not None and result.error.code == "404"


def test_save_briefing_runs_without_user_service_error(ctx: ToolContext) -> None:
    """环境异常（用户服务抛错）也要转成 ToolResult，而不是把异常抛出线程。"""
    result = run_tool("save_briefing", {"user_id": 1, "content": "# 空"}, ctx)

    # 内容太短 → 结构校验先失败
    assert not result.ok
    assert result.error is not None

    assert isinstance(NotFoundError("x"), Exception)


# ---------------------------------------------------------------- Provider 扩展点


def _register_fake_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """往注册表塞一个假的搜索服务，用于验证「换搜索服务不用改工具」。"""
    from datetime import datetime, timedelta

    from app.news import providers as providers_module
    from app.news.providers import ProviderResult, SourceReport
    from app.news.rss import FeedItem

    class FakeProvider:
        name = "fake"

        def search(self, sources=None):  # noqa: ANN001, ANN201 - 协议实现
            return ProviderResult(
                items=[
                    FeedItem(
                        title="AI Agent 平台新增工具调用编排能力",
                        link="https://example.com/fake-agent-news",
                        summary="示例摘要：面向 AI Agent 的工具调用编排。",
                        # 取窗口内的时间："today" 的上界是解析时刻，用 now() 会差几微秒落到窗口外
                        published_at=datetime.now() - timedelta(minutes=30),
                        published_raw="",
                        source_id="fake",
                        source_name="Fake News",
                    )
                ],
                sources=[SourceReport("fake", "Fake News", ok=True, item_count=1)],
            )

    monkeypatch.setitem(providers_module._PROVIDERS, "fake", FakeProvider())


def test_search_news_uses_registered_provider(ctx: ToolContext, monkeypatch: pytest.MonkeyPatch) -> None:
    """注册新搜索服务后，search_news 真的会走它——provider 不是装饰性参数。"""
    _register_fake_provider(monkeypatch)

    result = run_tool(
        "search_news",
        {"keyword": "AI Agent", "date_range": "today", "provider": "fake"},
        ctx,
    )

    assert result.ok
    assert result.data["provider"] == "fake"
    assert result.data["items"][0]["url"] == "https://example.com/fake-agent-news"
    assert result.meta["provider"] == "fake"


def test_search_news_provider_comes_from_settings(ctx: ToolContext, monkeypatch: pytest.MonkeyPatch) -> None:
    """不传 provider 时读 NEWS_PROVIDER 配置，便于整站切换搜索后端。"""
    from app.agent.tools import news as news_tools

    _register_fake_provider(monkeypatch)
    monkeypatch.setattr(news_tools.settings, "NEWS_PROVIDER", "fake")

    result = run_tool("search_news", {"keyword": "AI Agent", "date_range": "today"}, ctx)

    assert result.ok
    assert result.data["provider"] == "fake"


def test_search_news_unknown_provider_falls_back_to_rss(ctx: ToolContext, news_network) -> None:
    """配了一个不存在的 provider 名：回退到 rss，而不是让整个搜索失败。"""
    result = run_tool(
        "search_news",
        {"keyword": "AI Agent", "date_range": "today", "provider": "no-such-provider"},
        ctx,
    )

    assert result.ok
    assert result.data["provider"] == "rss"
    assert result.data["items"]


# ---------------------------------------------------------------- 时间窗与候选池规模


def test_search_defaults_to_last_24h_window(ctx: ToolContext, news_network) -> None:
    """默认窗口是「过去 24 小时」，不是「今天 0 点之后」。

    原因：资讯站按滚动 24 小时更新，简报多在早上 08:00 生成——那个时点
    「今天 0:00 之后」几乎没有内容，只挑当天会漏掉昨天下午的高价值报道，
    模型只能反复换关键词硬试（实测一轮搜了 19~29 次）。
    """
    from datetime import datetime

    result = run_tool("search_news", {"keyword": "Agent"}, ctx)  # 不传 date_range

    assert result.ok, result.summary
    window = result.data["date_range"]
    assert window["label"] == "最近 24 小时", "默认窗口必须是过去 24 小时"
    start = datetime.fromisoformat(window["start"])
    end = datetime.fromisoformat(window["end"])
    # 起点应约等于 24 小时前（允许几分钟误差：夹具与断言之间会有耗时）
    assert 23.9 * 3600 <= (end - start).total_seconds() <= 24.1 * 3600


def test_search_reports_pool_size_for_stopping_decision(ctx: ToolContext, news_network) -> None:
    """返回里要带「时间窗内全站候选数」和「本轮累计返回数」，模型据此判断该不该收手。"""
    result = run_tool("search_news", {"keyword": "MCP"}, ctx)

    assert result.ok, result.summary
    stats = result.data["stats"]
    assert stats["pool_window"] > 0
    assert stats["pool_window"] >= stats["matched"], "全站候选数不应小于本次命中数"
    assert stats["returned_so_far"] == len(ctx.news_state.items)
    assert "窗口内全站候选" in result.summary


# ---------------------------------------------------------------- 浏览模式


def test_browse_mode_returns_full_list_without_keyword(ctx: ToolContext, news_network) -> None:
    """不传 keyword = 浏览模式：一次给出时间窗内全部候选（这才是省轮次的关键）。

    背景：关键词模式一次只覆盖一个概念，模型要靠十几次搜索才凑齐素材；
    浏览模式把「今天有什么」一次性摆在它面前，让它挑着读，而不是穷举试词。
    """
    result = run_tool("search_news", {}, ctx)

    assert result.ok, result.summary
    assert result.data["mode"] == "browse"
    assert result.data["keyword"] is None
    assert result.data["stats"]["mode"] == "browse"

    items = result.data["items"]
    assert len(items) >= 5, "浏览模式应返回时间窗内全部候选，而不是只挑相关的几条"
    # 浏览模式不做相关度筛选：融资、推理成本这类「关键词搜不到」的条目也应出现
    titles = " ".join(item["title"] for item in items)
    assert "融资" in titles
    assert "推理成本" in titles
    # 按发布时间倒序（最新在前）
    published = [item["published"] for item in items if item["published"]]
    assert published == sorted(published, reverse=True)
    # 浏览模式是「目录」不是「正文」：不带摘要与相关度字段，
    # 否则 40 条摘要会被后续每一轮重新发送，把一次生成拖慢十几秒
    assert all("match_score" not in item for item in items)
    assert all("summary" not in item for item in items)
    assert "只返回标题" in result.data["note"]


def test_browse_mode_returns_more_than_keyword_mode(engine: Engine, tmp_path, news_network) -> None:
    """同一批数据：浏览模式条数明显多于单个关键词，证明「一次拿全」确实省调用。"""
    from app.core.config import settings

    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def make_ctx(name: str) -> ToolContext:
        workspace = tmp_path / name
        workspace.mkdir()
        return ToolContext(workspace=workspace, db_session_factory=factory)

    browse = run_tool("search_news", {}, make_ctx("browse"))
    keyword = run_tool("search_news", {"keyword": "MCP"}, make_ctx("keyword"))

    assert browse.ok and keyword.ok
    assert len(browse.data["items"]) > len(keyword.data["items"])
    # 浏览模式默认上限比关键词模式宽（12）
    assert settings.NEWS_BROWSE_MAX_ITEMS > settings.NEWS_SEARCH_MAX_ITEMS


def test_browse_mode_respects_its_own_limit(monkeypatch, ctx: ToolContext, news_network) -> None:
    """浏览模式的条数上限由 NEWS_BROWSE_MAX_ITEMS 控制（不能无限塞，否则爆上下文）。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "NEWS_BROWSE_MAX_ITEMS", 3)
    result = run_tool("search_news", {}, ctx)

    assert result.ok, result.summary
    assert len(result.data["items"]) == 3
    assert "未返回" in result.summary
    # 清单有上限时要告诉模型「怎么拿到剩下的」，否则它会重复浏览同一个窗口
    assert "分源浏览" in result.summary


# ---------------------------------------------------------------- 正文抓取缓存


def test_fetch_article_reuses_cache_within_run(monkeypatch, ctx: ToolContext, news_network) -> None:
    """同一次运行里重复读同一篇：第二次直接命中缓存，不再下载。

    实测模型会为「再确认一下」把同一篇读两三遍，每次 1~3 秒纯等待；
    正文在一轮内不会变，重复下载没有信息增量。
    """
    from app.news import http as news_http

    calls: list[str] = []
    real_fetch = news_http.fetch_text

    def counting_fetch(url: str, **kwargs):
        calls.append(url)
        return real_fetch(url, **kwargs)

    monkeypatch.setattr(news_http, "fetch_text", counting_fetch)

    url = "https://www.qbitai.com/2026/11/mcp-openai.html"
    first = run_tool("fetch_article", {"url": url}, ctx)
    second = run_tool("fetch_article", {"url": url}, ctx)

    assert first.ok and second.ok, second.summary
    assert len(calls) == 1, f"第二次不应再发网络请求，实际请求了 {len(calls)} 次"
    assert not first.data.get("cached")
    assert second.data["cached"] is True
    # 复用必须给到同样的正文，不能只剩标题
    assert second.data["content"] == first.data["content"]
