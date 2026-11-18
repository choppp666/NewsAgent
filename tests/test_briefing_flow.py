"""简报生成链路测试（HTTP → Agent 循环 → 新闻工具 → 数据库）。

这是第三阶段的主验收用例：
1. 两组不同偏好的用户，生成的简报内容必须**确实不同**；
2. 轨迹里必须能看出 Agent 多步自主执行（读偏好 → 搜索 → 读原文 → 写文件 → 入库）；
3. 当天没有相关新闻时，不许编造；
4. 同一用户同一天重复生成只保留一份。

LLM 用 mock（离线策略），网络用 `news_network` 夹具替换。
换成真实模型时，这些断言依然成立——因为它们验证的是**链路行为**，
而不是某一次具体的措辞。
"""

from __future__ import annotations

import sys
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import settings  # noqa: E402


def _create_user(client: TestClient, nickname: str, identity: str, topics: list[str],
                 keywords: list[str], exclude: list[str]) -> int:
    """建用户并保存订阅偏好，返回用户 ID。"""
    created = client.post("/api/v1/users", json={"nickname": nickname, "identity": identity})
    assert created.status_code == 201
    user_id = created.json()["data"]["id"]

    saved = client.put(
        f"/api/v1/users/{user_id}/preferences",
        json={"topics": topics, "keywords": keywords, "exclude_keywords": exclude},
    )
    assert saved.status_code == 200, saved.text
    return user_id


@pytest.fixture()
def briefing_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, news_network) -> Generator[Path, None, None]:
    """临时工作区 + 离线网络 + 强制 mock 模型。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setattr(settings, "AGENT_WORKSPACE", str(workspace))
    monkeypatch.setattr(settings, "LLM_API_KEY", None)
    yield workspace


@pytest.fixture()
def user_a(client: TestClient) -> int:
    return _create_user(
        client,
        "林工",
        "AI Agent 应用开发工程师，做 MCP 工具调用与多智能体编排",
        topics=["AI Agent", "MCP"],
        keywords=["工具调用"],
        exclude=["融资"],
    )


@pytest.fixture()
def user_b(client: TestClient) -> int:
    return _create_user(
        client,
        "周工",
        "后端开发工程师，关注大模型能力与 AI 编程工具落地",
        topics=["大模型", "AI 编程"],
        keywords=["代码生成"],
        exclude=["融资"],
    )


def _generate(client: TestClient, user_id: int, **extra) -> dict:
    response = client.post(
        "/api/agent/briefings",
        json={"user_id": user_id, "llm_mode": "mock", **extra},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_two_users_get_different_briefings(
    client: TestClient, briefing_env: Path, user_a: int, user_b: int
) -> None:
    """核心验收：不同订阅偏好 → 不同内容，且都能落文件 + 入库。"""
    data_a = _generate(client, user_a)
    data_b = _generate(client, user_b)

    assert data_a["briefing_saved"] is True
    assert data_b["briefing_saved"] is True

    content_a = data_a["briefing"]["content"]
    content_b = data_b["briefing"]["content"]

    # 用户 A 的简报围绕 Agent / MCP
    assert "MCP" in content_a
    assert "Agent" in content_a
    # 用户 B 的简报围绕大模型 / AI 编程
    assert ("大模型" in content_b) or ("编程" in content_b)

    assert content_a != content_b
    assert data_a["briefing"]["id"] != data_b["briefing"]["id"]

    # 文件真实落盘
    files = list((briefing_env / "briefings").glob("*.md"))
    assert len(files) == 2
    assert all("[原文链接]" not in path.read_text(encoding="utf-8") for path in files)


def test_agent_trace_shows_autonomous_multi_step(
    client: TestClient, briefing_env: Path, user_a: int
) -> None:
    """轨迹要能看出：读偏好 → 多次搜索 → 读原文 → 写文件 → 入库。"""
    data = _generate(client, user_a)
    tools = [call["tool"] for call in data["run"]["tool_calls"]]

    assert tools[0] == "get_user_preferences"
    assert tools.count("search_news") >= 2  # 至少两个关注方向各搜一次
    assert "write_file" in tools
    assert "save_briefing" in tools
    assert data["run"]["steps"] >= 5
    assert data["run"]["status"] == "completed"

    # 搜索关键词应当来自用户偏好，而不是写死的常量
    searched = [call["arguments"]["keyword"] for call in data["run"]["tool_calls"] if call["tool"] == "search_news"]
    assert any("Agent" in keyword or "MCP" in keyword for keyword in searched)
    assert data["searches"], "运行摘要里应记录每次搜索"


def test_same_day_regeneration_keeps_single_row(
    client: TestClient, briefing_env: Path, user_a: int
) -> None:
    first = _generate(client, user_a, briefing_date="2026-11-18")
    second = _generate(client, user_a, briefing_date="2026-11-18")

    assert first["briefing"]["id"] == second["briefing"]["id"]

    listed = client.get("/api/v1/briefings", params={"user_id": user_a}).json()["data"]
    assert listed["total"] == 1
    assert listed["items"][0]["briefing_date"] == "2026-11-18"


def test_briefing_list_and_detail(client: TestClient, briefing_env: Path, user_a: int) -> None:
    created = _generate(client, user_a, briefing_date="2026-11-18")
    briefing_id = created["briefing"]["id"]

    listed = client.get("/api/v1/briefings", params={"user_id": user_a}).json()["data"]
    assert listed["total"] == 1
    item = listed["items"][0]
    assert "content" not in item  # 列表不返回全文
    assert item["news_count"] >= 1
    assert item["focus_topics"]

    detail = client.get(f"/api/v1/briefings/{briefing_id}").json()["data"]
    assert detail["id"] == briefing_id
    assert detail["run_id"] == created["run"]["run_id"]
    assert "# 每日 AI 新闻简报" in detail["content"]


def test_generate_without_preferences_returns_400(client: TestClient, briefing_env: Path) -> None:
    created = client.post("/api/v1/users", json={"nickname": "空偏好", "identity": "学生"})
    user_id = created.json()["data"]["id"]

    response = client.post("/api/agent/briefings", json={"user_id": user_id, "llm_mode": "mock"})

    assert response.status_code == 400
    assert "订阅偏好" in response.json()["message"]


def test_generate_for_unknown_user_returns_404(client: TestClient, briefing_env: Path) -> None:
    response = client.post("/api/agent/briefings", json={"user_id": 12345, "llm_mode": "mock"})

    assert response.status_code == 404


def test_no_relevant_news_does_not_fabricate(client: TestClient, briefing_env: Path) -> None:
    """主题完全无新闻时，不写文件、不入库，并如实说明。"""
    user_id = _create_user(
        client, "冷门用户", "量子计算研究者", topics=["量子退火", "拓扑量子比特"], keywords=[], exclude=[]
    )

    data = _generate(client, user_id)

    assert data["briefing_saved"] is False
    assert data["briefing"] is None
    assert data["run"]["status"] == "completed"
    assert data["news_candidates"] == 0
    answer = data["run"]["answer"]
    assert "没有" in answer or "未" in answer
    assert not list((briefing_env / "briefings").glob("*.md"))


def test_openai_mode_without_key_returns_400(client: TestClient, briefing_env: Path, user_a: int) -> None:
    response = client.post(
        "/api/agent/briefings", json={"user_id": user_a, "llm_mode": "openai"}
    )

    assert response.status_code == 400
    assert "LLM_API_KEY" in response.json()["message"]
