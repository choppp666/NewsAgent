"""新闻能力层的单元测试（RSS 解析 / 时间过滤 / 去重 / 正文抽取 / 简报校验）。

这些用例全部离线：不碰网络、不碰数据库，只验证纯逻辑。
一条新闻从「Feed 里的一行 XML」走到「简报里的一条」，中间每一层都能单独定位问题。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.news.article import extract_article  # noqa: E402
from app.news.briefing import extract_links, parse_briefing  # noqa: E402
from app.news.dedupe import NewsDeduplicator, canonical_url, title_similarity  # noqa: E402
from app.news.feeds import FEED_CATALOG, FeedSource, resolve_feeds  # noqa: E402
from app.news.filter import (  # noqa: E402
    DateRangeError,
    build_query,
    find_excluded,
    match_item,
    parse_date_range,
)
from app.news.rss import FeedParseError, clean_html, parse_datetime, parse_feed  # noqa: E402

SOURCE = FeedSource("test", "测试源", "https://example.com/feed", "zh", "测试")


# ---------------------------------------------------------------- RSS 解析


def test_parse_rss_2_0() -> None:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
      <channel>
        <title>测试频道</title>
        <item>
          <title>OpenAI 发布新模型</title>
          <link>https://example.com/a.html</link>
          <description><![CDATA[<p>这是<b>摘要</b>内容</p>]]></description>
          <pubDate>Wed, 18 Nov 2026 09:41:55 +0000</pubDate>
          <category>资讯</category>
          <category>大模型</category>
        </item>
        <item>
          <title>没有链接的条目应被跳过</title>
          <description>xxx</description>
        </item>
      </channel>
    </rss>"""

    items = parse_feed(xml, SOURCE)

    assert len(items) == 1
    item = items[0]
    assert item.title == "OpenAI 发布新模型"
    assert item.link == "https://example.com/a.html"
    assert "这是摘要内容" in item.summary
    assert item.categories == ["资讯", "大模型"]
    assert item.published_at is not None
    assert item.source_name == "测试源"


def test_parse_atom_feed() -> None:
    xml = """<?xml version="1.0" encoding="utf-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <title>Atom 源</title>
      <entry>
        <title>Agent 工程实践</title>
        <link rel="alternate" href="https://example.com/atom-post" />
        <published>2026-11-18T10:00:00Z</published>
        <summary>Atom 的 summary 字段</summary>
      </entry>
    </feed>"""

    items = parse_feed(xml, SOURCE)

    assert len(items) == 1
    assert items[0].link == "https://example.com/atom-post"
    assert items[0].published_at is not None


def test_parse_rdf_feed() -> None:
    xml = """<?xml version="1.0" encoding="utf-8"?>
    <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
             xmlns="http://purl.org/rss/1.0/"
             xmlns:dc="http://purl.org/dc/elements/1.1/">
      <item rdf:about="https://example.com/rdf-post">
        <title>RDF 格式的条目</title>
        <link>https://example.com/rdf-post</link>
        <dc:date>2026-11-18T08:30:00+08:00</dc:date>
        <description>RDF 描述</description>
      </item>
    </rdf:RDF>"""

    items = parse_feed(xml, SOURCE)

    assert len(items) == 1
    assert items[0].published_at is not None
    assert items[0].published_at.hour == 8


def test_parse_feed_rejects_html_page() -> None:
    """RSS 地址失效时会重定向到网页，必须明确报错而不是静默返回空。"""
    with pytest.raises(FeedParseError, match="HTML"):
        parse_feed("<!DOCTYPE html><html><body>404</body></html>", SOURCE)


def test_parse_feed_rejects_broken_xml() -> None:
    with pytest.raises(FeedParseError, match="XML 解析失败"):
        parse_feed("<rss><channel><item><title>截断", SOURCE)


def test_parse_datetime_normalizes_timezone() -> None:
    """Feed 的 UTC 时间必须转成本地时间，否则「今天」的判定会错一天。"""
    raw = "Wed, 18 Nov 2026 09:41:55 +0000"
    expected = datetime.fromtimestamp(parsedate_to_datetime(raw).timestamp()).replace(microsecond=0)

    parsed = parse_datetime(raw)

    assert parsed == expected
    assert parsed.tzinfo is None


def test_clean_html_strips_boilerplate() -> None:
    raw = "<p>正文内容</p><p>The post Something appeared first on TechCrunch.</p>"

    text = clean_html(raw)

    assert "正文内容" in text
    assert "appeared first on" not in text


# ---------------------------------------------------------------- 时间过滤


def test_date_range_variants() -> None:
    now = datetime(2026, 11, 18, 20, 0, 0)

    today = parse_date_range("today", now=now)
    assert today.start == datetime(2026, 11, 18, 0, 0)
    assert today.contains(datetime(2026, 11, 18, 9, 0))
    assert not today.contains(datetime(2026, 11, 17, 23, 59))

    yesterday = parse_date_range("yesterday", now=now)
    assert yesterday.contains(datetime(2026, 11, 17, 12, 0))
    assert not yesterday.contains(datetime(2026, 11, 18, 1, 0))

    three_days = parse_date_range("3d", now=now)
    assert three_days.contains(datetime(2026, 11, 16, 8, 0))
    assert not three_days.contains(datetime(2026, 11, 15, 8, 0))

    one_day = parse_date_range("2026-11-10", now=now)
    assert one_day.contains(datetime(2026, 11, 10, 23, 0))

    span = parse_date_range("2026-11-01:2026-11-05", now=now)
    assert span.contains(datetime(2026, 11, 3, 10, 0))
    assert not span.contains(datetime(2026, 11, 6, 10, 0))


def test_date_range_rejects_garbage() -> None:
    with pytest.raises(DateRangeError):
        parse_date_range("下个月")


def test_date_window_excludes_unknown_time() -> None:
    """发布时间未知的条目一律不算「今天」，但调用方会单独统计条数。"""
    window = parse_date_range("today", now=datetime(2026, 11, 18, 12, 0))

    assert window.contains(None) is False


# ---------------------------------------------------------------- 关键词


def test_keyword_english_word_boundary() -> None:
    query = build_query("AI")

    assert match_item(query, title="AI agents are everywhere").ok
    # 不能把 said / chain 里的 "ai" 当成命中
    assert not match_item(query, title="He said the chain was fine").ok


def test_keyword_matches_plural() -> None:
    query = build_query("agent")

    assert match_item(query, title="Agents that plan ahead").ok


def test_keyword_chinese_substring() -> None:
    query = build_query("大模型")

    assert match_item(query, title="国产大模型再降价").ok
    assert not match_item(query, title="显卡价格波动").ok


def test_multi_token_query_needs_half_hits() -> None:
    """「AI Agent」这类双词查询：命中一个就算相关，两个都不中才算无关。"""
    query = build_query("AI Agent")

    assert match_item(query, title="AI 芯片出货量创新高").ok
    assert match_item(query, title="Agent 编排框架对比").ok
    assert not match_item(query, title="咖啡机选购指南").ok


def test_exclude_keywords() -> None:
    assert find_excluded("某公司完成 B 轮融资", ["融资", "股价"]) == "融资"
    assert find_excluded("模型能力评测", ["融资"]) is None
    # 英文排除词同样走词边界，避免误伤
    assert find_excluded("An update on pricing", ["AI"]) is None


# ---------------------------------------------------------------- 去重


def test_canonical_url_normalizes_variants() -> None:
    base = canonical_url("https://www.qbitai.com/2026/11/510922.html")
    variants = [
        "http://qbitai.com/2026/11/510922.html/",
        "https://www.qbitai.com/2026/11/510922.html?utm_source=rss&utm_medium=feed",
        "https://www.qbitai.com/2026/11/510922.html#comments",
    ]

    for variant in variants:
        assert canonical_url(variant) == base


def test_title_similarity_and_deduper() -> None:
    assert title_similarity("OpenAI 发布 MCP 支持", "【快讯】OpenAI 发布 MCP 支持！") > 0.9
    assert title_similarity("量子位：模型降价", "英伟达财报超预期") < 0.4

    deduper = NewsDeduplicator(threshold=0.9)
    assert deduper.check("OpenAI 发布 MCP 支持", "https://a.com/1") is None

    same_url = deduper.check("另一个标题", "https://a.com/1?utm_source=x")
    assert same_url is not None and same_url.reason == "same_url"

    near = deduper.check("OpenAI 发布 MCP 支持！", "https://b.com/2")
    assert near is not None and near.reason == "near_title"

    assert deduper.check("完全不同的话题", "https://c.com/3") is None
    assert deduper.size == 2


# ---------------------------------------------------------------- 正文抽取


def test_extract_article_full_text() -> None:
    html = """<html><head>
    <meta property="og:title" content="测试文章标题" />
    <meta property="og:site_name" content="测试站" />
    <meta property="article:published_time" content="2026-11-18T10:00:00+08:00" />
    </head><body>
    <nav>首页 关于</nav>
    <article>
      <p>第一段正文，讲的是模型能力与工程实践之间的关系，足够长以便通过长度阈值；这里补充一些背景信息，
      说明这条新闻为什么值得关注，以及它对开发者的实际影响。过去半年里，同类工具在上下文长度、
      工具调用稳定性上的差异逐渐缩小，真正的分水岭变成了工程化能力：能否复现、能否观测、能否回滚。</p>
      <h2>小节标题</h2>
      <p>第二段正文，提到了具体的实现细节与数据，同样足够长以通过过滤；文中给出了两套接入方案，
      并对比了它们的上下文开销与延迟表现，最后给出迁移建议。作者还强调，协议层的统一并不等于
      生态的统一，真正的摩擦点在鉴权、配额与错误处理这些「不性感」的地方。</p>
      <p>责任编辑：张三</p>
    </article>
    <footer>版权所有</footer>
    </body></html>"""

    article = extract_article(html, "https://example.com/a")

    assert article.title == "测试文章标题"
    assert article.site_name == "测试站"
    assert article.published_at is not None
    assert article.content_kind == "full"
    assert "第一段正文" in article.content
    # 导航与版权行不能进正文
    assert "责任编辑" not in article.content
    assert "版权所有" not in article.content


def test_extract_article_falls_back_to_summary() -> None:
    """正文抓不到时只返回站点摘要，并明确标注 content_kind=summary。"""
    html = """<html><head>
    <meta property="og:title" content="只有摘要的页面" />
    <meta name="description" content="这是一段站点摘要，长度足够被当作摘要使用，但绝不是正文内容。" />
    </head><body><div id="app"></div></body></html>"""

    article = extract_article(html, "https://example.com/b")

    assert article.content_kind == "summary"
    assert "站点摘要" in article.content
    assert any("仅返回站点摘要" in warning for warning in article.warnings)


def test_extract_article_returns_nothing_for_empty_page() -> None:
    article = extract_article("<html><body><div></div></body></html>", "https://example.com/c")

    assert article.content_kind == "none"
    assert article.content == ""


# ---------------------------------------------------------------- 简报校验

GOOD_BRIEFING = """# 每日 AI 新闻简报

- 简报日期：2026-11-18
- 用户关注方向：AI Agent、MCP
- 今日新闻数量：1

## 今日重点新闻

### OpenAI 开放 MCP 支持
- 新闻摘要：OpenAI 在 API 层支持 MCP。
- 核心事件：官方文档给出接入示例。
- 重要性分析：对 Agent 工具生态影响直接。
- 新闻来源：量子位
- 原文链接：https://www.qbitai.com/2026/11/mcp-openai.html
- 发布时间：2026-11-18 10:00

## 行业动态总结

今天只有一条相关新闻，但指向工具生态标准化这一趋势。补充说明文字用于满足最少字数要求。

## 今日值得关注

- 关注 MCP 服务端实现的进展。
"""


def test_parse_briefing_accepts_complete_document() -> None:
    report = parse_briefing(GOOD_BRIEFING)

    assert report.ok, report.issues
    assert report.title == "每日 AI 新闻简报"
    assert report.news_count == 1
    assert report.declared_count == 1
    assert report.briefing_date == "2026-11-18"
    assert report.links == ["https://www.qbitai.com/2026/11/mcp-openai.html"]


def test_parse_briefing_detects_missing_sections() -> None:
    content = GOOD_BRIEFING.replace("## 行业动态总结", "## 其它").replace("## 今日值得关注", "## 附录")

    report = parse_briefing(content)

    assert not report.ok
    assert "行业动态总结" in report.missing_sections
    assert "今日值得关注" in report.missing_sections


def test_parse_briefing_detects_item_without_link() -> None:
    content = GOOD_BRIEFING.replace(
        "- 原文链接：https://www.qbitai.com/2026/11/mcp-openai.html", "- 原文链接：未获取到"
    )

    report = parse_briefing(content)

    assert not report.ok
    assert report.items_without_link
    assert "缺少" in report.to_error_message() or "链接" in report.to_error_message()


def test_parse_briefing_detects_truncated_output() -> None:
    """LLM 输出被截断时，字数和条目都会异常，必须在入库前拦住。"""
    report = parse_briefing("# 每日 AI 新闻简报\n\n- 简报日期：2026-11-18\n\n## 今日重点新闻\n\n### 一条还没写完的新闻")

    assert not report.ok
    assert report.char_count < 200
    assert any("过短" in issue for issue in report.issues)


def test_extract_links_supports_markdown_and_bare_urls() -> None:
    text = "见 [原文](https://a.com/1)，以及裸链接 https://b.com/2 。重复 https://a.com/1"

    links = extract_links(text)

    assert links == ["https://a.com/1", "https://b.com/2"]


# ---------------------------------------------------------------- 源目录


def test_resolve_feeds_falls_back_to_catalog() -> None:
    feeds = resolve_feeds(["qbitai", "不存在的源"])

    assert [feed.id for feed in feeds] == ["qbitai"]
    assert len(resolve_feeds(None)) == len(FEED_CATALOG)


# ---------------------------------------------------------------- 批量抓取预算


def test_search_skips_source_that_exceeds_batch_deadline(monkeypatch) -> None:
    """慢源不许拖住整批：超过总预算就按失败计入报告，而不是一直等到它回来。

    背景：`httpx.Timeout` 限制的是「每个阶段」，响应体慢慢吐就能绕过它
    （实测有源跑到 13 秒），一批 10 个源里混进一个，整次搜索就白等十几秒。
    """
    import time

    from app.core.config import settings
    from app.news.providers import RSSNewsProvider, SourceReport
    from app.news.rss import FeedItem

    monkeypatch.setattr(settings, "NEWS_SEARCH_DEADLINE", 0.5)
    monkeypatch.setattr(settings, "NEWS_FEED_CACHE_TTL", 0)

    provider = RSSNewsProvider()

    def fake_fetch(feed):
        if feed.id == "ithome":
            time.sleep(1.5)  # 远超 0.5s 预算
        item = FeedItem(
            title=f"{feed.name} 的一条新闻",
            link=f"https://example.com/{feed.id}/1",
            summary="摘要",
            published_at=None,
            published_raw="",
            source_id=feed.id,
            source_name=feed.name,
        )
        return feed, SourceReport(feed.id, feed.name, ok=True, item_count=1), [item]

    monkeypatch.setattr(provider, "_fetch_one", fake_fetch)

    started = time.perf_counter()
    result = provider.search(["qbitai", "ithome"])
    elapsed = time.perf_counter() - started

    assert elapsed < 1.4, f"应按预算返回，实际等了 {elapsed:.2f}s"
    # 报告顺序与请求的源顺序一致，便于日志与前端稳定展示
    assert [report.source_id for report in result.sources] == ["qbitai", "ithome"]
    fast, slow = result.sources
    assert fast.ok
    assert not slow.ok and "总预算" in (slow.error or "")
    # 快源的结果照常返回，不因为另一个源超时整体失败
    assert len(result.items) == 1
