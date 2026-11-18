"""Agent 接口测试（验收项 7-8：测试入口 + 调用日志可见）。

这些用例走完整的 HTTP 链路（TestClient → 路由 → AgentRunner → 工具），
但把工作区指向临时目录、并强制 Mock 模式，保证不依赖外部 API、不污染仓库。
"""

from __future__ import annotations

import sys
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import settings  # noqa: E402


@pytest.fixture()
def agent_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[Path, None, None]:
    """临时工作区 + 强制 Mock 模式（清空 API Key 走 auto 降级路径）。"""
    workspace = tmp_path / "ws"
    (workspace / "notes").mkdir(parents=True)
    (workspace / "notes" / "agent_design.md").write_text("# 设计\nAgent 循环要点\n", encoding="utf-8")
    (workspace / "notes" / "other.md").write_text("# 其它\n无关内容\n", encoding="utf-8")

    monkeypatch.setattr(settings, "AGENT_WORKSPACE", str(workspace))
    monkeypatch.setattr(settings, "AGENT_MODE", "auto")
    monkeypatch.setattr(settings, "LLM_API_KEY", None)
    yield workspace


def test_tools_endpoint_lists_all_tools(client: TestClient) -> None:
    response = client.get("/api/agent/tools")

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    names = {tool["name"] for tool in body["data"]["tools"]}
    assert body["data"]["total"] == len(names)
    # 文件工具（第二阶段）与新闻工具（第三阶段）由同一个注册中心调度
    assert {"list_dir", "read_file", "search_content", "write_file", "bash"} <= names
    assert {"get_user_preferences", "search_news", "fetch_article", "save_briefing"} <= names
    for tool in body["data"]["tools"]:
        assert tool["description"]
        assert tool["parameters"]["type"] == "object"
        assert tool["parameters"]["properties"]


def test_run_endpoint_executes_multi_step_task(client: TestClient, agent_workspace: Path) -> None:
    response = client.post(
        "/api/agent/run",
        json={"task": "在 workspace 目录里搜索包含 Agent 关键字的内容，并把结果整理成 report.md"},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "completed"
    assert data["llm_mode"] == "mock"
    assert data["tool_call_count"] >= 2
    assert data["steps"] >= 2
    assert data["answer"]
    # 轨迹要能看出「谁调用了什么、结果如何」
    first = data["tool_calls"][0]
    assert first["tool"]
    assert "ok" in first and "summary" in first
    # 产物必须落在临时工作区里
    assert (agent_workspace / "report.md").exists()


def test_run_records_are_queryable(client: TestClient, agent_workspace: Path) -> None:
    created = client.post("/api/agent/run", json={"task": "看看 workspace 目录里有哪些文件"}).json()["data"]
    run_id = created["run_id"]

    listed = client.get("/api/agent/runs").json()["data"]
    assert any(item["run_id"] == run_id for item in listed["items"])

    detail = client.get(f"/api/agent/runs/{run_id}").json()["data"]
    assert detail["run_id"] == run_id
    assert isinstance(detail["tool_calls"], list)


def test_run_record_not_found(client: TestClient) -> None:
    response = client.get("/api/agent/runs/not-exist-id")

    assert response.status_code == 404
    assert response.json()["code"] == 404


def test_openai_mode_without_key_returns_400(client: TestClient, agent_workspace: Path) -> None:
    response = client.post(
        "/api/agent/run",
        json={"task": "随便做点什么", "llm_mode": "openai"},
    )

    assert response.status_code == 400
    assert "LLM_API_KEY" in response.json()["message"]


def test_empty_task_is_rejected(client: TestClient, agent_workspace: Path) -> None:
    response = client.post("/api/agent/run", json={"task": ""})

    assert response.status_code == 422
    assert response.json()["code"] == 422


def test_max_steps_override_is_clamped(client: TestClient, agent_workspace: Path) -> None:
    """请求方传超过上限的轮数会被接口层拦下（校验 1-20）。"""
    response = client.post("/api/agent/run", json={"task": "看看目录", "max_steps": 999})

    assert response.status_code == 422
