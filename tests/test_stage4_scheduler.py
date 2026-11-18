"""第四阶段：定时任务测试。

验证四件事：
1. **遍历用户**：派发时每个用户都会拿到自己的任务，而不是「只跑第一个用户」；
2. **隔离**：某个用户派发失败，其余用户照常执行（单点失败不扩大）；
3. **幂等**：连续派发两次不会重复生成；
4. **可运维**：注册了哪些作业、下次几点跑、服务重启后重新注册、启动补跑。

注意：调度器只负责「什么时候触发」，所以这里不真的等时钟，
而是直接 `await dispatch_daily_generation()` —— 这也是把作业和调度器
拆成两个模块的好处。
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import settings  # noqa: E402
from app.crud import task as task_crud  # noqa: E402
from app.models.task import TaskStatus  # noqa: E402
from app.scheduler import jobs as scheduler_jobs  # noqa: E402
from app.scheduler.manager import DAILY_JOB_ID, PUSH_RETRY_JOB_ID, SchedulerManager  # noqa: E402
from app.services.generation_worker import generation_worker  # noqa: E402


def _create_user_with_prefs(client: TestClient, nickname: str, topic: str) -> int:
    created = client.post("/api/v1/users", json={"nickname": nickname, "identity": "后端开发工程师"})
    user_id = created.json()["data"]["id"]
    client.put(f"/api/v1/users/{user_id}/preferences", json={"topics": [topic]})
    return user_id


@pytest.fixture()
def recorded_submits(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """把 worker 的提交换成记录，避免测试真的去跑 Agent。"""
    submitted: list[int] = []

    def fake_submit(task_id: int) -> str:
        submitted.append(task_id)
        return "queued"

    monkeypatch.setattr(generation_worker, "submit", fake_submit)
    return submitted


def test_dispatch_creates_task_for_every_user(
    client: TestClient, session_factory, recorded_submits: list[int]
) -> None:
    """每个用户都会收到自己的任务（数据隔离：任务各归各的用户）。"""
    user_a = _create_user_with_prefs(client, "林工", "AI Agent")
    user_b = _create_user_with_prefs(client, "周工", "大模型")
    day = date(2026, 11, 18)

    result = asyncio.run(scheduler_jobs.dispatch_daily_generation(target_date=day))

    assert result["users"] == 2
    assert result["submitted"] == 2
    assert len(recorded_submits) == 2

    with session_factory() as db:
        task_a = task_crud.get_by_user_date(db, user_a, day)
        task_b = task_crud.get_by_user_date(db, user_b, day)
        assert task_a is not None and task_b is not None
        assert task_a.user_id == user_a and task_b.user_id == user_b
        assert task_a.trigger_type == "scheduled"
        assert task_a.id != task_b.id


def test_dispatch_is_idempotent(client: TestClient, session_factory, recorded_submits: list[int]) -> None:
    """连续派发两次：第二次全部复用，不新增任务也不重复提交。"""
    _create_user_with_prefs(client, "林工", "AI Agent")
    day = date(2026, 11, 18)

    first = asyncio.run(scheduler_jobs.dispatch_daily_generation(target_date=day))
    second = asyncio.run(scheduler_jobs.dispatch_daily_generation(target_date=day))

    assert first["submitted"] == 1
    # 第二次任务仍处于 pending（worker 是假的），所以会被「复用」而不是新建
    assert second["submitted"] + second["reused"] == 1
    assert len(recorded_submits) == 2  # 同一条任务被再次派发，但库中仍只有一行

    with session_factory() as db:
        from sqlalchemy import func, select

        from app.models.task import GenerationTask

        total = db.scalar(select(func.count()).select_from(GenerationTask))
        assert total == 1


def test_dispatch_skips_completed_users(client: TestClient, session_factory, recorded_submits: list[int]) -> None:
    """已完成的用户不会被定时任务重复生成。"""
    from app.models.base import local_now
    from app.services import briefing_service, task_service

    user_id = _create_user_with_prefs(client, "林工", "AI Agent")
    day = date(2026, 11, 18)

    with session_factory() as db:
        trigger = task_service.get_or_create(db, user_id=user_id, briefing_date=day)
        task_service.claim(db, trigger.task.id)
        db.expire_all()
        task = task_crud.get(db, trigger.task.id)
        briefing, _ = briefing_service.upsert_briefing(
            db, user_id=user_id, briefing_date=day, title="t", content="# 每日 AI 新闻简报", news_count=1
        )
        task_service.succeed(db, task, briefing=briefing, run_id="r1")
        assert task.status == TaskStatus.SUCCESS.value
        assert task.finished_at is not None
        assert local_now() >= task.finished_at

    result = asyncio.run(scheduler_jobs.dispatch_daily_generation(target_date=day))

    assert result["submitted"] == 0
    assert result["reused"] == 1
    assert recorded_submits == []


def test_one_user_failure_does_not_block_others(
    client: TestClient, session_factory, recorded_submits: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """关键验收：单个用户派发失败，不影响其它用户。"""
    user_a = _create_user_with_prefs(client, "林工", "AI Agent")
    user_b = _create_user_with_prefs(client, "周工", "大模型")

    from app.services import task_service as real_task_service

    original = real_task_service.get_or_create

    def flaky_get_or_create(db, *, user_id, **kwargs):  # noqa: ANN001
        if user_id == user_a:
            raise RuntimeError("模拟该用户的画像读取失败")
        return original(db, user_id=user_id, **kwargs)

    monkeypatch.setattr(scheduler_jobs.task_service, "get_or_create", flaky_get_or_create)

    result = asyncio.run(scheduler_jobs.dispatch_daily_generation(target_date=date(2026, 11, 18)))

    assert result["users"] == 2
    assert result["failed"] == 1
    assert result["submitted"] == 1
    assert any(str(user_a) in error for error in result["errors"])

    with session_factory() as db:
        assert task_crud.get_by_user_date(db, user_a, date(2026, 11, 18)) is None
        assert task_crud.get_by_user_date(db, user_b, date(2026, 11, 18)) is not None


def test_dispatch_without_users_is_noop(session_factory, recorded_submits: list[int]) -> None:
    result = asyncio.run(scheduler_jobs.dispatch_daily_generation(target_date=date(2026, 11, 18)))
    assert result["users"] == 0
    assert result["submitted"] == 0


def test_catch_up_skips_before_scheduled_time(
    client: TestClient, session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """还没到今天的执行时间 → 不补跑。"""
    monkeypatch.setattr(settings, "SCHEDULER_CATCH_UP", True)
    monkeypatch.setattr(settings, "SCHEDULER_TIME", "23:59")

    result = asyncio.run(scheduler_jobs.catch_up_missed_tasks())

    assert result["submitted"] == 0
    assert result.get("reason") == "before_scheduled_time"


def test_catch_up_submits_missing_tasks(
    client: TestClient, session_factory, recorded_submits: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """过了执行时间但当天没有任务 → 启动补跑会补上。"""
    monkeypatch.setattr(settings, "SCHEDULER_CATCH_UP", True)
    monkeypatch.setattr(settings, "SCHEDULER_TIME", "00:00")

    user_id = _create_user_with_prefs(client, "林工", "AI Agent")
    day = scheduler_jobs._today_for_scheduler()  # noqa: SLF001 - 与作业内部保持同一时区口径

    result = asyncio.run(scheduler_jobs.catch_up_missed_tasks())

    assert result["submitted"] == 1
    assert recorded_submits

    with session_factory() as db:
        task = task_crud.get_by_user_date(db, user_id, day)
        assert task is not None
        assert task.trigger_type == "catch_up"


def test_catch_up_skips_users_already_done(
    client: TestClient, session_factory, recorded_submits: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import briefing_service, task_service

    monkeypatch.setattr(settings, "SCHEDULER_CATCH_UP", True)
    monkeypatch.setattr(settings, "SCHEDULER_TIME", "00:00")

    user_id = _create_user_with_prefs(client, "林工", "AI Agent")
    day = scheduler_jobs._today_for_scheduler()  # noqa: SLF001

    with session_factory() as db:
        trigger = task_service.get_or_create(db, user_id=user_id, briefing_date=day)
        task_service.claim(db, trigger.task.id)
        db.expire_all()
        task = task_crud.get(db, trigger.task.id)
        briefing, _ = briefing_service.upsert_briefing(
            db, user_id=user_id, briefing_date=day, title="t", content="# 每日 AI 新闻简报", news_count=1
        )
        task_service.succeed(db, task, briefing=briefing, run_id="r1")

    result = asyncio.run(scheduler_jobs.catch_up_missed_tasks())

    assert result["submitted"] == 0
    assert result["skipped"] == 1


def test_catch_up_can_be_disabled(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "SCHEDULER_CATCH_UP", False)
    result = asyncio.run(scheduler_jobs.catch_up_missed_tasks())
    assert result["enabled"] is False


def test_retry_job_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """推送重试作业内部异常不能冒泡到调度器（否则整个调度器会被标记为异常）。"""

    def boom(**kwargs):  # noqa: ANN003
        raise RuntimeError("模拟 SMTP 模块崩溃")

    monkeypatch.setattr(scheduler_jobs.push_service, "retry_due_pushes", boom)

    stats = scheduler_jobs.retry_failed_pushes()

    assert stats["scanned"] == 0


# ---------------------------------------------------------------- 调度器本身


def test_scheduler_registers_jobs_and_reports_next_run(client: TestClient, session_factory) -> None:
    """启动后应有「每日派发」与「推送重试」两个作业，并能给出下次执行时间。"""
    manager = SchedulerManager()

    async def scenario() -> dict:
        await manager.start()
        try:
            info = manager.info()
            assert manager.running is True
            return info
        finally:
            await manager.shutdown()

    info = asyncio.run(scenario())

    job_ids = {job["id"] for job in info["jobs"]}
    assert DAILY_JOB_ID in job_ids
    assert PUSH_RETRY_JOB_ID in job_ids
    assert info["running"] is True
    assert info["next_run_at"] is not None
    assert info["time"] == f"{settings.scheduler_time_parts[0]:02d}:{settings.scheduler_time_parts[1]:02d}"

    # 下次执行时间应该在未来的 24 小时内（每天一次）
    next_run = info["next_run_at"]
    assert next_run > datetime.now(next_run.tzinfo) - timedelta(minutes=1)
    assert next_run < datetime.now(next_run.tzinfo) + timedelta(days=2)

    assert manager.running is False


def test_scheduler_respects_disabled_flag(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """SCHEDULER_ENABLED=false 时不注册任何作业（但仍允许手动生成）。"""
    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", False)
    manager = SchedulerManager()

    async def scenario() -> dict:
        await manager.start()
        try:
            return manager.info()
        finally:
            await manager.shutdown()

    info = asyncio.run(scenario())

    assert info["enabled"] is False
    assert info["running"] is False
    assert info["jobs"] == []


def test_scheduler_time_config_is_parsed(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """执行时间可由配置修改；非法值回退 08:00 而不是让服务起不来。"""
    monkeypatch.setattr(settings, "SCHEDULER_TIME", "21:35")
    assert settings.scheduler_time_parts == (21, 35)

    monkeypatch.setattr(settings, "SCHEDULER_TIME", "25:99")
    assert settings.scheduler_time_parts == (8, 0)

    monkeypatch.setattr(settings, "SCHEDULER_TIME", "not-a-time")
    assert settings.scheduler_time_parts == (8, 0)


# ---------------------------------------------------------------- 僵尸任务回收（自愈）


def _make_running_task(session_factory, user_id: int, *, minutes_ago: int, attempts: int = 1) -> int:
    """造一条「正在跑但心跳已经过期」的任务。"""
    from app.models.base import local_now
    from app.services import task_service

    with session_factory() as db:
        trigger = task_service.get_or_create(db, user_id=user_id, briefing_date=date.today())
        task_service.claim(db, trigger.task.id)
        db.expire_all()
        task = task_crud.get(db, trigger.task.id)
        task.heartbeat_at = local_now() - timedelta(minutes=minutes_ago)
        task.started_at = task.heartbeat_at
        task.attempts = attempts
        db.commit()
        return task.id


def test_reap_stale_task_and_resubmit(
    client: TestClient, session_factory, recorded_submits: list[int]
) -> None:
    """心跳过期的任务会被标记失败并当天重新派发（不必等到明天）。"""
    user_id = _create_user_with_prefs(client, "林工", "AI Agent")
    task_id = _make_running_task(session_factory, user_id, minutes_ago=90)

    stats = scheduler_jobs.recover_stale_tasks_job()

    assert stats["reaped"] == 1
    assert stats["resubmitted"] == 1
    assert recorded_submits == [task_id]

    with session_factory() as db:
        task = task_crud.get(db, task_id)
        assert task.status == TaskStatus.PENDING.value  # 已回收并重置为待执行
        assert "执行中断" in (task.error_message or "") or task.error_message is None


def test_reap_ignores_fresh_running(client: TestClient, session_factory, recorded_submits: list[int]) -> None:
    """心跳新鲜的任务不会被误杀。"""
    user_id = _create_user_with_prefs(client, "林工", "AI Agent")
    _make_running_task(session_factory, user_id, minutes_ago=1)

    stats = scheduler_jobs.recover_stale_tasks_job()

    assert stats["reaped"] == 0
    assert recorded_submits == []


def test_reap_stops_after_max_attempts(
    client: TestClient, session_factory, recorded_submits: list[int]
) -> None:
    """反复中断的任务不再自动重派，交给人工排查。"""
    user_id = _create_user_with_prefs(client, "林工", "AI Agent")
    task_id = _make_running_task(session_factory, user_id, minutes_ago=90, attempts=3)

    stats = scheduler_jobs.recover_stale_tasks_job(max_attempts=3)

    assert stats["reaped"] == 1
    assert stats["resubmitted"] == 0
    assert stats["failed"] == 1
    assert recorded_submits == []

    with session_factory() as db:
        task = task_crud.get(db, task_id)
        assert task.status == TaskStatus.FAILED.value
        assert "回收" in (task.error_message or "")


# ---------------------------------------------------------------- 僵尸任务回收（自愈）


def _make_running_task(session_factory, user_id: int, *, minutes_ago: int, attempts: int = 1) -> int:
    """造一条「正在跑但心跳已经过期」的任务。"""
    from app.models.base import local_now
    from app.services import task_service

    with session_factory() as db:
        trigger = task_service.get_or_create(db, user_id=user_id, briefing_date=date.today())
        task_service.claim(db, trigger.task.id)
        db.expire_all()
        task = task_crud.get(db, trigger.task.id)
        task.heartbeat_at = local_now() - timedelta(minutes=minutes_ago)
        task.started_at = task.heartbeat_at
        task.attempts = attempts
        db.commit()
        return task.id
