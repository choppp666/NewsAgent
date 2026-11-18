"""第四阶段：接口与整条链路测试。

这里做两件事：
1. **接口契约**：概览 / 触发 / 任务列表 / 任务详情 / 推送开关 / 调度状态，
   字段齐全、幂等语义正确；
2. **整条链路**：手动触发 → 任务入库 → Agent 真实跑一遍（离线策略 + 假网络）
   → 简报入库 → 推送记录 → 前端可读的列表与详情。

第 2 项是第四阶段的「总验收」：它证明第四阶段没有另写一套新闻逻辑，
而是把第三阶段的 Agent 能力包在任务状态机里跑通了。
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Generator
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import settings  # noqa: E402
from app.services.generation_service import execute_task  # noqa: E402
from app.services.generation_worker import generation_worker  # noqa: E402


# ---------------------------------------------------------------- 夹具


@pytest.fixture()
def stage4_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    news_network,
    session_factory,
) -> Generator[Path, None, None]:
    """临时工作区 + 离线新闻网络 + mock 模型 + 无 SMTP。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setattr(settings, "AGENT_WORKSPACE", str(workspace))
    monkeypatch.setattr(settings, "LLM_API_KEY", None)
    monkeypatch.setattr(settings, "AGENT_MODE", "mock")
    monkeypatch.setattr(settings, "SMTP_HOST", None)
    monkeypatch.setattr(settings, "SMTP_USER", None)
    monkeypatch.setattr(settings, "SMTP_SENDER", None)
    monkeypatch.setattr(settings, "PUSH_BASE_URL", "http://127.0.0.1:8000")
    yield workspace


@pytest.fixture()
def queued(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """记录 worker 收到的任务，不真的执行（需要时由测试自己 await execute_task）。"""
    submitted: list[int] = []
    monkeypatch.setattr(generation_worker, "submit", lambda task_id: (submitted.append(task_id) or "queued"))
    return submitted


def _create_user(client: TestClient, nickname: str = "小林", email: str | None = "reader@example.com") -> int:
    response = client.post(
        "/api/v1/users",
        json={"nickname": nickname, "identity": "后端开发实习生，关注 AI Agent", "email": email},
    )
    assert response.status_code == 201
    return response.json()["data"]["id"]


def _set_prefs(client: TestClient, user_id: int, topics: list[str], keywords: list[str] | None = None) -> None:
    response = client.put(
        f"/api/v1/users/{user_id}/preferences",
        json={"topics": topics, "keywords": keywords or [], "exclude_keywords": ["融资"]},
    )
    assert response.status_code == 200, response.text


# ---------------------------------------------------------------- 概览


def test_dashboard_empty_state(client: TestClient, stage4_env: Path) -> None:
    user_id = _create_user(client)

    response = client.get(f"/api/v1/users/{user_id}/dashboard")
    assert response.status_code == 200
    data = response.json()["data"]

    assert data["user"]["id"] == user_id
    assert data["user"]["push_enabled"] is True
    assert data["preferences"] is None
    assert data["latest_briefing"] is None
    assert data["latest_task"] is None
    assert data["latest_push"] is None
    assert data["running_task"] is None
    assert data["stats"]["total_tasks"] == 0
    # 调度器状态是首页「下次自动生成时间」的来源
    assert data["scheduler"] is not None
    assert data["scheduler"]["time"] == (
        f"{settings.scheduler_time_parts[0]:02d}:{settings.scheduler_time_parts[1]:02d}"
    )


def test_dashboard_unknown_user_returns_404(client: TestClient, stage4_env: Path) -> None:
    response = client.get("/api/v1/users/9999/dashboard")
    assert response.status_code == 404


def test_dashboard_shows_preferences(client: TestClient, stage4_env: Path) -> None:
    user_id = _create_user(client)
    _set_prefs(client, user_id, ["AI Agent", "MCP"], ["工具调用"])

    data = client.get(f"/api/v1/users/{user_id}/dashboard").json()["data"]

    assert data["preferences"]["topics"] == ["AI Agent", "MCP"]
    assert data["preferences"]["keywords"] == ["工具调用"]
    assert data["preferences"]["exclude_keywords"] == ["融资"]


# ---------------------------------------------------------------- 手动触发


def test_trigger_without_preferences_returns_400(client: TestClient, stage4_env: Path, queued: list[int]) -> None:
    user_id = _create_user(client)

    response = client.post(f"/api/v1/users/{user_id}/briefings/trigger", json={})

    assert response.status_code == 400
    assert "订阅偏好" in response.json()["message"]
    assert queued == []


def test_trigger_returns_pending_task(client: TestClient, stage4_env: Path, queued: list[int]) -> None:
    user_id = _create_user(client)
    _set_prefs(client, user_id, ["AI Agent"])

    response = client.post(f"/api/v1/users/{user_id}/briefings/trigger", json={})

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["action"] == "created"
    assert data["task"]["status"] == "pending"
    assert data["task"]["trigger_type"] == "manual"
    assert data["task"]["briefing"] is None
    assert data["task"]["push"] is None
    assert queued == [data["task"]["id"]]


def test_double_click_does_not_create_two_tasks(
    client: TestClient, stage4_env: Path, queued: list[int]
) -> None:
    """验收要求：避免用户重复点击造成重复任务。"""
    user_id = _create_user(client)
    _set_prefs(client, user_id, ["AI Agent"])

    first = client.post(f"/api/v1/users/{user_id}/briefings/trigger", json={}).json()["data"]
    second = client.post(f"/api/v1/users/{user_id}/briefings/trigger", json={}).json()["data"]

    assert first["task"]["id"] == second["task"]["id"]
    listed = client.get("/api/v1/tasks", params={"user_id": user_id}).json()["data"]
    assert listed["total"] == 1


def test_trigger_unknown_user_returns_404(client: TestClient, stage4_env: Path) -> None:
    response = client.post("/api/v1/users/4321/briefings/trigger", json={})
    assert response.status_code == 404


# ---------------------------------------------------------------- 整条链路


def test_manual_trigger_full_chain(client: TestClient, stage4_env: Path, queued: list[int]) -> None:
    """触发 → 后台执行 → 简报入库 → 推送记录 → 列表/详情可读。"""
    user_id = _create_user(client)
    _set_prefs(client, user_id, ["AI Agent", "MCP"], ["工具调用"])

    trigger = client.post(f"/api/v1/users/{user_id}/briefings/trigger", json={}).json()["data"]
    task_id = trigger["task"]["id"]

    # 模拟 worker 真正执行（与生产同一条 execute_task 路径）
    result = asyncio.run(execute_task(task_id))
    assert result.ok is True, result.message

    detail = client.get(f"/api/v1/tasks/{task_id}").json()["data"]
    task = detail["task"]
    assert task["status"] == "success"
    assert task["briefing"] is not None
    assert task["briefing"]["news_count"] >= 1
    assert task["push"] is not None
    # 未配置 SMTP → 推送记为 skipped，但生成结论不受影响
    assert task["push"]["status"] == "skipped"
    assert "SMTP" in (task["push"]["last_error"] or "")

    # 简报全文可读，且是 Markdown
    assert detail["briefing"] is not None
    assert "# 每日 AI 新闻简报" in detail["briefing"]["content"]
    assert "http" in detail["briefing"]["content"]

    # 文件也真的落地了
    files = list((stage4_env / "briefings").glob("*.md"))
    assert len(files) == 1

    # 首页概览同步更新
    dashboard = client.get(f"/api/v1/users/{user_id}/dashboard").json()["data"]
    assert dashboard["latest_briefing"]["id"] == detail["briefing"]["id"]
    assert dashboard["latest_task"]["status"] == "success"
    assert dashboard["latest_push"]["status"] == "skipped"
    assert dashboard["stats"]["total_briefings"] == 1
    assert dashboard["running_task"] is None

    # 历史列表：日期 / 标题 / 条数 / 生成状态 / 推送状态五项齐全
    listed = client.get("/api/v1/tasks", params={"user_id": user_id}).json()["data"]
    assert listed["total"] == 1
    item = listed["items"][0]
    assert item["briefing_date"]
    assert item["briefing"]["title"]
    assert item["briefing"]["news_count"] >= 1
    assert item["status"] == "success"
    assert item["push"]["status"] == "skipped"


def test_second_trigger_after_success_is_idempotent(
    client: TestClient, stage4_env: Path, queued: list[int]
) -> None:
    """已生成后再次点击：不重新排队，除非显式 force。"""
    user_id = _create_user(client)
    _set_prefs(client, user_id, ["AI Agent"])

    task_id = client.post(f"/api/v1/users/{user_id}/briefings/trigger", json={}).json()["data"]["task"]["id"]
    asyncio.run(execute_task(task_id))
    assert len(queued) == 1

    again = client.post(f"/api/v1/users/{user_id}/briefings/trigger", json={}).json()["data"]
    assert again["action"] == "already_generated"
    assert again["task"]["status"] == "success"
    assert len(queued) == 1  # 没有再次入队

    forced = client.post(
        f"/api/v1/users/{user_id}/briefings/trigger", json={"force": True}
    ).json()["data"]
    assert forced["action"] == "regenerated"
    assert forced["task"]["id"] == task_id
    assert len(queued) == 2


def test_regenerate_keeps_single_briefing(
    client: TestClient, stage4_env: Path, queued: list[int]
) -> None:
    """重新生成覆盖当天简报，不会产生第二条简报。"""
    user_id = _create_user(client)
    _set_prefs(client, user_id, ["AI Agent"])

    first_task = client.post(f"/api/v1/users/{user_id}/briefings/trigger", json={}).json()["data"]["task"]["id"]
    asyncio.run(execute_task(first_task))
    first = client.get(f"/api/v1/tasks/{first_task}").json()["data"]["briefing"]["id"]

    client.post(f"/api/v1/users/{user_id}/briefings/trigger", json={"force": True})
    asyncio.run(execute_task(first_task))
    second = client.get(f"/api/v1/tasks/{first_task}").json()["data"]["briefing"]["id"]

    assert first == second
    listed = client.get("/api/v1/briefings", params={"user_id": user_id}).json()["data"]
    assert listed["total"] == 1


def test_failed_task_can_be_retried_via_api(
    client: TestClient, stage4_env: Path, queued: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """生成失败 → 任务标记 failed → 重试接口能重新排队 → 再跑成功。"""
    user_id = _create_user(client)
    _set_prefs(client, user_id, ["AI Agent"])

    task_id = client.post(f"/api/v1/users/{user_id}/briefings/trigger", json={}).json()["data"]["task"]["id"]

    # 让第一次执行失败（模拟模型/网络异常）
    from app.services import generation_service

    original = generation_service.run_briefing

    async def boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("模拟 LLM 超时")

    monkeypatch.setattr(generation_service, "run_briefing", boom)
    result = asyncio.run(execute_task(task_id))
    assert result.status == "failed"

    detail = client.get(f"/api/v1/tasks/{task_id}").json()["data"]
    assert detail["task"]["status"] == "failed"
    assert "LLM 超时" in (detail["task"]["error_message"] or "")

    # 修复后重试
    monkeypatch.setattr(generation_service, "run_briefing", original)
    retried = client.post(f"/api/v1/tasks/{task_id}/retry")
    assert retried.status_code == 200, retried.text
    assert retried.json()["data"]["action"] == "resumed"

    result = asyncio.run(execute_task(task_id))
    assert result.ok is True

    detail = client.get(f"/api/v1/tasks/{task_id}").json()["data"]
    assert detail["task"]["status"] == "success"
    assert detail["task"]["error_message"] is None
    assert detail["task"]["attempts"] == 2


def test_skip_when_no_news_found(client: TestClient, stage4_env: Path, queued: list[int]) -> None:
    """当天没有相关新闻：任务记为 failed 并说明原因，不写假简报。

    这与第三阶段的语义一致（「宁可少，不可编」），区别只是现在会留下一条
    可追溯的任务记录，前端能告诉用户「今天确实没搜到」而不是一片空白。
    """
    user_id = _create_user(client)
    _set_prefs(client, user_id, ["量子退火", "拓扑量子比特"])

    task_id = client.post(f"/api/v1/users/{user_id}/briefings/trigger", json={}).json()["data"]["task"]["id"]
    result = asyncio.run(execute_task(task_id))

    assert result.ok is False
    detail = client.get(f"/api/v1/tasks/{task_id}").json()["data"]
    assert detail["task"]["status"] == "failed"
    assert detail["briefing"] is None
    assert detail["task"]["error_message"]


# ---------------------------------------------------------------- 推送与调度接口


def test_push_settings_toggle(client: TestClient, stage4_env: Path) -> None:
    user_id = _create_user(client)

    initial = client.get(f"/api/v1/users/{user_id}/push-settings").json()["data"]
    assert initial["push_enabled"] is True
    assert initial["smtp_configured"] is False  # 测试环境未配置 SMTP

    off = client.put(f"/api/v1/users/{user_id}/push-settings", json={"push_enabled": False})
    assert off.status_code == 200
    assert off.json()["data"]["push_enabled"] is False

    # 用户信息接口也要同步
    assert client.get(f"/api/v1/users/{user_id}").json()["data"]["push_enabled"] is False

    on = client.put(f"/api/v1/users/{user_id}/push-settings", json={"push_enabled": True})
    assert on.json()["data"]["push_enabled"] is True


def test_push_records_endpoint(client: TestClient, stage4_env: Path, queued: list[int]) -> None:
    user_id = _create_user(client)
    _set_prefs(client, user_id, ["AI Agent"])
    task_id = client.post(f"/api/v1/users/{user_id}/briefings/trigger", json={}).json()["data"]["task"]["id"]
    asyncio.run(execute_task(task_id))

    records = client.get(f"/api/v1/users/{user_id}/push-records").json()["data"]
    assert records["total"] == 1
    assert records["items"][0]["status"] == "skipped"


def test_scheduler_endpoint(client: TestClient, stage4_env: Path) -> None:
    data = client.get("/api/v1/scheduler").json()["data"]
    assert "enabled" in data
    assert data["time"]
    assert data["timezone"]
    assert data["max_concurrency"] >= 1


def test_scheduler_run_now_dispatches(client: TestClient, stage4_env: Path, queued: list[int]) -> None:
    user_id = _create_user(client)
    _set_prefs(client, user_id, ["AI Agent"])

    response = client.post("/api/v1/scheduler/run-now")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["submitted"] == 1

    listed = client.get("/api/v1/tasks", params={"user_id": user_id}).json()["data"]
    assert listed["items"][0]["trigger_type"] == "scheduled"


def test_scheduler_recover_endpoint(client: TestClient, stage4_env: Path, queued: list[int], session_factory) -> None:
    """运维接口：立即回收僵尸任务（与后台作业同一份逻辑）。"""
    from datetime import date as _date

    from app.crud import task as task_crud
    from app.models.base import local_now
    from app.services import task_service

    user_id = _create_user(client)
    _set_prefs(client, user_id, ["AI Agent"])

    with session_factory() as db:
        trigger = task_service.get_or_create(db, user_id=user_id, briefing_date=_date.today())
        task_service.claim(db, trigger.task.id)
        db.expire_all()
        task = task_crud.get(db, trigger.task.id)
        task.heartbeat_at = local_now() - timedelta(minutes=120)
        db.commit()
        task_id = task.id

    response = client.post("/api/v1/scheduler/recover")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["reaped"] == 1
    assert data["resubmitted"] == 1
    assert task_id in queued
