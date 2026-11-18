"""第四阶段：任务状态机与幂等控制测试。

这一组用例验证的是「不会重复生成」这件事本身：
- 同一用户同一天只有一条任务（数据库唯一键）；
- 执行权靠条件 UPDATE 抢占，两个执行者不会同时跑；
- 失败可以重新执行，成功不会被定时任务重复执行（除非显式 force）；
- 进程崩溃留下的 running 任务能被重新领取（心跳过期）；
- 第三阶段的历史简报会被补建成任务记录。
"""

from __future__ import annotations

import sys
from collections.abc import Generator
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.crud import task as task_crud  # noqa: E402
from app.models.base import local_now  # noqa: E402
from app.models.task import GenerationTask, TaskStatus  # noqa: E402
from app.services import briefing_service, task_service  # noqa: E402


def _create_user_with_prefs(client: TestClient, nickname: str = "小林") -> int:
    created = client.post("/api/v1/users", json={"nickname": nickname, "identity": "后端开发实习生"})
    assert created.status_code == 201
    user_id = created.json()["data"]["id"]
    saved = client.put(
        f"/api/v1/users/{user_id}/preferences",
        json={"topics": ["AI Agent"], "keywords": ["MCP"], "exclude_keywords": []},
    )
    assert saved.status_code == 200
    return user_id


@pytest.fixture()
def db(session_factory) -> Generator:
    """直接操作数据库的会话（后台任务用的是另一个会话，读到的是同一份数据）。"""
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


def test_same_user_same_day_only_one_task(client: TestClient, db, session_factory) -> None:
    """重复触发只登记一条任务（唯一键 + 状态机复用）。"""
    user_id = _create_user_with_prefs(client)
    day = date.today()

    first = task_service.get_or_create(db, user_id=user_id, briefing_date=day, trigger_type="manual")
    second = task_service.get_or_create(db, user_id=user_id, briefing_date=day, trigger_type="scheduled")

    assert first.action == "created"
    assert second.task.id == first.task.id
    assert second.action in ("resumed", "already_running")

    total = db.scalar(
        select(func.count()).select_from(GenerationTask).where(GenerationTask.user_id == user_id)
    )
    assert total == 1


def test_claim_is_exclusive(client: TestClient, db) -> None:
    """条件 UPDATE 保证同一任务只有一个执行者。"""
    user_id = _create_user_with_prefs(client)
    trigger = task_service.get_or_create(db, user_id=user_id, briefing_date=date.today())

    assert task_service.claim(db, trigger.task.id) is not None
    # 第二次领取必然失败：状态已经是 running 且心跳是刚刚刷新的
    assert task_service.claim(db, trigger.task.id) is None

    db.expire_all()
    task = task_crud.get(db, trigger.task.id)
    assert task.status == TaskStatus.RUNNING.value
    assert task.attempts == 1


def test_failed_task_can_be_retried(client: TestClient, db) -> None:
    """失败可以重新执行，attempts 累加，但不会新增行。"""
    user_id = _create_user_with_prefs(client)
    trigger = task_service.get_or_create(db, user_id=user_id, briefing_date=date.today())
    task_service.claim(db, trigger.task.id)

    db.expire_all()
    task = task_crud.get(db, trigger.task.id)
    task_service.fail(db, task, message="模拟失败")

    db.expire_all()
    again = task_service.get_or_create(db, user_id=user_id, briefing_date=date.today(), trigger_type="retry")
    assert again.action == "resumed"
    assert again.task.id == trigger.task.id

    assert task_service.claim(db, again.task.id) is not None
    db.expire_all()
    assert task_crud.get(db, again.task.id).attempts == 2


def test_success_blocks_scheduled_rerun(client: TestClient, db) -> None:
    """已成功：定时任务跳过；手动 force 才覆盖重跑。"""
    user_id = _create_user_with_prefs(client)
    day = date.today()

    trigger = task_service.get_or_create(db, user_id=user_id, briefing_date=day)
    task_service.claim(db, trigger.task.id)
    db.expire_all()
    task = task_crud.get(db, trigger.task.id)

    briefing, _ = briefing_service.upsert_briefing(
        db,
        user_id=user_id,
        briefing_date=day,
        title="每日 AI 新闻简报",
        content="# 每日 AI 新闻简报\n\n正文",
        news_count=3,
    )
    task_service.succeed(db, task, briefing=briefing, run_id="run-1")

    skipped = task_service.get_or_create(db, user_id=user_id, briefing_date=day, trigger_type="scheduled")
    assert skipped.action == "already_generated"
    assert skipped.needs_run is False

    forced = task_service.get_or_create(db, user_id=user_id, briefing_date=day, force=True)
    assert forced.action == "regenerated"
    assert forced.needs_run is True


def test_stale_running_task_can_be_reclaimed(client: TestClient, db) -> None:
    """进程崩溃遗留的 running 任务（心跳过期）可以被重新领取。"""
    user_id = _create_user_with_prefs(client)
    day = date.today()

    trigger = task_service.get_or_create(db, user_id=user_id, briefing_date=day)
    task_service.claim(db, trigger.task.id)

    # 手工把心跳改成 2 小时前，模拟「进程在跑的时候被杀掉」
    db.expire_all()
    task = task_crud.get(db, trigger.task.id)
    task.heartbeat_at = local_now() - timedelta(hours=2)
    task.started_at = task.heartbeat_at
    db.commit()

    again = task_service.get_or_create(db, user_id=user_id, briefing_date=day, trigger_type="catch_up")
    assert again.action == "resumed"
    assert task_service.claim(db, again.task.id) is not None


def test_fresh_running_task_is_not_rerun(client: TestClient, db) -> None:
    """心跳新鲜时不允许重复触发（避免同一用户并发跑两次）。"""
    user_id = _create_user_with_prefs(client)
    trigger = task_service.get_or_create(db, user_id=user_id, briefing_date=date.today())
    task_service.claim(db, trigger.task.id)

    again = task_service.get_or_create(db, user_id=user_id, briefing_date=date.today(), force=True)
    assert again.action == "already_running"
    assert again.needs_run is False


def test_backfill_creates_task_for_legacy_briefing(client: TestClient, db, session_factory) -> None:
    """第三阶段的简报（没有任务记录）在升级后被补成 success 任务。"""
    user_id = _create_user_with_prefs(client)
    legacy_day = date(2026, 11, 1)

    briefing_service.upsert_briefing(
        db,
        user_id=user_id,
        briefing_date=legacy_day,
        title="旧简报",
        content="# 每日 AI 新闻简报\n\n历史内容",
        news_count=2,
    )

    created = task_service.backfill_from_briefings(db)
    assert created == 1

    task = task_crud.get_by_user_date(db, user_id, legacy_day)
    assert task is not None
    assert task.status == TaskStatus.SUCCESS.value
    assert task.briefing_id is not None

    # 幂等：再跑一次不会重复补
    assert task_service.backfill_from_briefings(db) == 0


def test_backfill_skips_days_with_task(client: TestClient, db) -> None:
    """已经有任务记录的日期不会被补建覆盖。"""
    user_id = _create_user_with_prefs(client)
    day = date.today()

    trigger = task_service.get_or_create(db, user_id=user_id, briefing_date=day)
    briefing_service.upsert_briefing(
        db,
        user_id=user_id,
        briefing_date=day,
        title="今天的简报",
        content="# 每日 AI 新闻简报\n\n今天",
        news_count=1,
    )

    assert task_service.backfill_from_briefings(db) == 0
    db.expire_all()
    task = task_crud.get(db, trigger.task.id)
    assert task.status == TaskStatus.PENDING.value  # 未被改写成 success


def test_unknown_user_raises_404(client: TestClient, db) -> None:
    from app.core.exceptions import NotFoundError

    with pytest.raises(NotFoundError):
        task_service.get_or_create(db, user_id=99999, briefing_date=date.today())


def test_task_unique_constraint_in_db(db, client: TestClient) -> None:
    """直接插两条同用户同日任务 → 数据库层拒绝（应用层之外的最终保障）。"""
    from sqlalchemy.exc import IntegrityError

    user_id = _create_user_with_prefs(client)
    day = date(2026, 12, 1)

    task_crud.create(db, user_id=user_id, briefing_date=day, trigger_type="manual")
    db.commit()

    with pytest.raises(IntegrityError):
        task_crud.create(db, user_id=user_id, briefing_date=day, trigger_type="scheduled")
        db.commit()
    db.rollback()
