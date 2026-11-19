"""pytest 公共夹具。

关键点：测试使用临时目录下的独立 SQLite 文件，
绝不会污染开发库 data/news_agent.db。
"""

from __future__ import annotations

import sys
from collections.abc import Generator
from datetime import datetime, timedelta
from html import escape
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

# 保证从项目根目录运行 `pytest` 时能 import app 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import models  # noqa: E402,F401  # 注册模型
from app.core.database import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def engine(tmp_path: Path) -> Generator[Engine, None, None]:
    """每个测试用例一个全新的临时数据库。"""
    db_path = tmp_path / "test.db"
    test_engine = create_engine(
        f"sqlite:///{db_path.as_posix()}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=test_engine)
    yield test_engine
    Base.metadata.drop_all(bind=test_engine)
    test_engine.dispose()


@pytest.fixture()
def client(engine: Engine) -> Generator[TestClient, None, None]:
    """覆盖 get_db 依赖，把请求打到临时数据库。

    注意：这里不用 with TestClient(app)，避免触发 lifespan 中的 init_db()
    在真实数据库上建表。
    """
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_get_db() -> Generator[Session, None, None]:
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    test_client = TestClient(app)
    try:
        yield test_client
    finally:
        app.dependency_overrides.clear()


@pytest.fixture()
def session_factory(engine: Engine, monkeypatch: pytest.MonkeyPatch):
    """把全局 `SessionLocal` 指向临时库。

    第四阶段的后台任务 / 推送 / 调度作业都用「自己开会话」的方式工作
    （`from app.core.database import SessionLocal`，函数内导入），
    如果不替换，测试会写到真实的 data/news_agent.db 里。
    这里 monkeypatch 模块属性，覆盖所有延迟导入点。
    """
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    import app.core.database as database_module

    monkeypatch.setattr(database_module, "SessionLocal", TestingSession)
    return TestingSession


@pytest.fixture()
def user_id(client: TestClient) -> int:
    """预置一个用户，返回其 ID。"""
    response = client.post(
        "/api/v1/users",
        json={"nickname": "小林", "identity": "后端开发实习生", "email": "xiaolin@example.com"},
    )
    assert response.status_code == 201
    return response.json()["data"]["id"]


# ---------------------------------------------------------------- 新闻测试夹具
#
# 新闻链路涉及网络，测试必须完全离线且可重复。做法是把唯一的外部依赖
# `app.news.http.fetch_text` 换成「按 URL 查表」的假实现：
# 命中的 URL 返回固定 XML/HTML，其余一律返回 403（顺便覆盖「部分源不可用」）。
# 这样 RSS 解析、时间过滤、去重、正文抽取这些**真正的逻辑**都被真实执行。

_QBITAI_FEED = "https://www.qbitai.com/feed"
_LEIPHONE_FEED = "https://www.leiphone.com/feed"
_ITHOME_FEED = "https://www.ithome.com/rss/"

_ARTICLE_URL_MCP = "https://www.qbitai.com/2026/11/mcp-openai.html"
_ARTICLE_URL_GRAPH = "https://www.leiphone.com/agent/langgraph-1.html"


def _rfc822(moment: datetime) -> str:
    """构造带本地时区偏移的 RFC822 时间字符串（模拟真实 Feed 的 UTC+8 写法）。"""
    return moment.strftime("%a, %d %b %Y %H:%M:%S +0800")


def _rss(channel_title: str, items: list[dict]) -> str:
    """拼一份最小可用的 RSS 2.0。"""
    body = []
    for item in items:
        categories = "".join(f"<category><![CDATA[{tag}]]></category>" for tag in item.get("categories", []))
        published = f"<pubDate>{_rfc822(item['published'])}</pubDate>" if item.get("published") else ""
        body.append(
            "<item>"
            f"<title><![CDATA[{item['title']}]]></title>"
            # link 里的 & 必须转义，否则整份 XML 非法（真实源也会这么写）
            f"<link>{escape(item['url'])}</link>"
            f"<description><![CDATA[{item.get('summary', '')}]]></description>"
            f"{published}{categories}"
            "</item>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<rss version="2.0"><channel><title>{channel_title}</title>'
        f'<link>https://example.com</link>{"".join(body)}</channel></rss>'
    )


_ARTICLE_HTML = """<!DOCTYPE html>
<html><head>
<meta property="og:title" content="{title}" />
<meta property="og:site_name" content="{site}" />
<meta property="og:description" content="{description}" />
<meta property="article:published_time" content="{published}" />
</head><body>
<nav>首页 资讯 标签 关于</nav>
<article>
<h1>{title}</h1>
<h2>核心变化</h2>
{paragraphs}
</article>
<footer>版权所有 转载请联系</footer>
<script>var tracking = 1;</script>
</body></html>"""


def build_news_fixtures() -> tuple[dict[str, str], dict[str, str], dict]:
    """构造「Feed URL → XML」「文章 URL → HTML」以及时间基准。

    所有时间都相对「现在」生成，避免测试隔天失效。
    """
    now = datetime.now().replace(microsecond=0)
    # 「今天的新闻」用 5 分钟前，保证任何时刻跑都落在今天窗口内（除非正好跨零点）
    today = now - timedelta(minutes=5)
    yesterday = now - timedelta(days=1, hours=1)

    feeds = {
        _QBITAI_FEED: _rss(
            "量子位",
            [
                {
                    "title": "OpenAI 开放 MCP 协议支持，Agent 工具生态再扩张",
                    "url": _ARTICLE_URL_MCP,
                    "summary": "OpenAI 宣布在 API 层支持 Model Context Protocol，第三方工具可以按统一描述接入。",
                    "published": today,
                    "categories": ["资讯", "MCP"],
                },
                {
                    "title": "某 AI 公司完成 3 亿元 B 轮融资",
                    "url": "https://www.qbitai.com/2026/11/funding.html",
                    "summary": "融资资金将用于具身智能机器人研发。",
                    "published": today,
                    "categories": ["融资"],
                },
                {
                    "title": "昨天的旧闻：Agent 框架横评",
                    "url": "https://www.qbitai.com/2026/11/old-review.html",
                    "summary": "昨天发布的横评文章，用于验证时间过滤。",
                    "published": yesterday,
                },
                {
                    # 与第一条同标题、不同链接（带追踪参数）→ 应被标题去重挡掉
                    "title": "OpenAI 开放 MCP 协议支持，Agent 工具生态再扩张",
                    "url": _ARTICLE_URL_MCP + "?utm_source=rss&utm_medium=feed",
                    "summary": "转载版本，内容相同。",
                    "published": today,
                },
                {
                    # 没有发布时间 → 默认被排除，但会统计进 stats.undated
                    "title": "没有时间戳的 MCP 传闻",
                    "url": "https://www.qbitai.com/2026/11/undated.html",
                    "summary": "这条没有 pubDate。",
                },
            ],
        ),
        _LEIPHONE_FEED: _rss(
            "雷锋网",
            [
                {
                    "title": "LangGraph 1.0 发布：多智能体编排进入工程化阶段",
                    "url": _ARTICLE_URL_GRAPH,
                    "summary": "官方称新版重构了状态机与检查点机制，支持更长的 Agent 任务链。",
                    "published": today,
                    "categories": ["AI", "Agent"],
                },
                {
                    "title": "大模型推理成本再降 50%，开发者实测",
                    "url": "https://www.leiphone.com/ai/inference-cost.html",
                    "summary": "多家云厂商下调推理价格，长上下文场景成本下降明显。",
                    "published": today,
                    "categories": ["大模型"],
                },
            ],
        ),
        _ITHOME_FEED: _rss(
            "IT之家",
            [
                {
                    "title": "GitHub Copilot 新增仓库级代码生成",
                    "url": "https://www.ithome.com/ai/copilot-repo.html",
                    "summary": "新功能可以跨文件理解仓库结构，生成更贴合项目的补丁。",
                    "published": today,
                    "categories": ["AI 编程"],
                },
                {
                    "title": "AI 编程工具横评：谁更适合后端项目",
                    "url": "https://www.ithome.com/ai/coding-tools.html",
                    "summary": "从补全准确率、上下文长度、私有化部署三个维度对比主流工具。",
                    "published": today,
                },
            ],
        ),
    }

    paragraphs_a = "".join(
        f"<p>第 {index} 段正文：OpenAI 在开发者文档中说明，MCP 让模型可以按统一协议调用外部工具，"
        f"并给出 Python 与 TypeScript 的接入示例，社区已有多个服务端实现。</p>"
        for index in range(1, 6)
    )
    paragraphs_b = "".join(
        f"<p>第 {index} 段正文：LangGraph 1.0 重构了状态机与检查点，长链路 Agent 任务可以中断后续跑，"
        f"官方给出了多智能体协作的示例代码。</p>"
        for index in range(1, 6)
    )
    articles = {
        _ARTICLE_URL_MCP: _ARTICLE_HTML.format(
            title="OpenAI 开放 MCP 协议支持，Agent 工具生态再扩张",
            site="量子位",
            description="MCP 成为工具接入的事实标准之一。",
            published=today.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
            paragraphs=paragraphs_a,
        ),
        _ARTICLE_URL_GRAPH: _ARTICLE_HTML.format(
            title="LangGraph 1.0 发布：多智能体编排进入工程化阶段",
            site="雷锋网",
            description="长链路 Agent 任务支持检查点恢复。",
            published=today.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
            paragraphs=paragraphs_b,
        ),
    }
    return feeds, articles, {"now": now, "today": today, "yesterday": yesterday}


@pytest.fixture()
def news_network(monkeypatch: pytest.MonkeyPatch):
    """把 HTTP 抓取换成离线查表实现，返回 (feeds, articles, 时间基准)。"""
    from app.news import http as news_http

    feeds, articles, meta = build_news_fixtures()

    def fake_fetch_text(url: str, *, timeout: float = 12.0, max_bytes: int = 3_000_000, accept: str = ""):
        if url in feeds:
            return news_http.FetchOutcome(
                ok=True, url=url, final_url=url, status_code=200, text=feeds[url],
                content_type="application/rss+xml; charset=utf-8", size=len(feeds[url]),
            )
        if url in articles:
            return news_http.FetchOutcome(
                ok=True, url=url, final_url=url, status_code=200, text=articles[url],
                content_type="text/html; charset=utf-8", size=len(articles[url]),
            )
        return news_http.FetchOutcome(
            ok=False, url=url, final_url=url, status_code=403, text="",
            content_type="text/html", size=0, error="HTTP 403",
        )

    monkeypatch.setattr(news_http, "fetch_text", fake_fetch_text)

    # Provider 带进程内 TTL 缓存，属「跨测试的全局状态」：不清掉的话，
    # 前一个用例的抓取结果会灌到下一个用例（例如「所有源都挂」的用例
    # 命中了上一轮成功的缓存，断言就永远失败）。
    from app.news.providers import get_provider

    provider = get_provider("rss")
    provider.clear_cache()
    yield feeds, articles, meta
    provider.clear_cache()
