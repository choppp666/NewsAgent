"""任务执行记录数据访问层。

这里最关键的是 `claim()`：它把「领取执行权」做成一条**条件 UPDATE**。

```sql
UPDATE generation_tasks
   SET status='running', attempts=attempts+1, started_at=:now, heartbeat_at=:now
 WHERE id=:id
   AND (status IN ('pending','failed','skipped')
        OR (status='running' AND heartbeat_at < :stale_before))
```

- 受影响行数 = 1 → 抢到了，可以跑 Agent；
- 受影响行数 = 0 → 别人正在跑（或已经成功），本次直接放弃。

这条语句在 SQLite 里是原子的（写操作串行化），所以即使两个进程、两个线程
同时领取同一条任务，也只有一个能成功。不需要额外的分布式锁。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session

from app.models.base import local_now
from app.models.task import GenerationTask, TaskStatus


def get(db: Session, task_id: int) -> GenerationTask | None:
    """按主键查询任务。"""
    return db.get(GenerationTask, task_id)


def get_by_user_date(db: Session, user_id: int, briefing_date: date) -> GenerationTask | None:
    """查询某用户某天的任务（唯一键查询）。"""
    stmt = (
        select(GenerationTask)
        .where(GenerationTask.user_id == user_id, GenerationTask.briefing_date == briefing_date)
        .limit(1)
    )
    return db.scalars(stmt).first()


def list_by_user(
    db: Session,
    user_id: int,
    *,
    limit: int = 20,
    offset: int = 0,
    status: str | None = None,
) -> tuple[Sequence[GenerationTask], int]:
    """按日期倒序分页列出某用户的任务。"""
    conditions = [GenerationTask.user_id == user_id]
    if status:
        conditions.append(GenerationTask.status == status)

    base = select(GenerationTask).where(*conditions)
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    stmt = (
        base.order_by(GenerationTask.briefing_date.desc(), GenerationTask.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return db.scalars(stmt).all(), int(total)


def list_for_date(db: Session, briefing_date: date, *, statuses: Sequence[str] | None = None) -> list[GenerationTask]:
    """列出某一天的全部任务（启动补跑 / 定时任务预检用）。"""
    conditions = [GenerationTask.briefing_date == briefing_date]
    if statuses:
        conditions.append(GenerationTask.status.in_(list(statuses)))
    stmt = select(GenerationTask).where(*conditions).order_by(GenerationTask.user_id.asc())
    return list(db.scalars(stmt).all())


def create(db: Session, **fields: object) -> GenerationTask:
    """新建任务（未提交）。唯一键冲突由调用方捕获。"""
    task = GenerationTask(**fields)
    db.add(task)
    db.flush()
    return task


def claim(db: Session, task_id: int, *, stale_before: datetime | None = None) -> bool:
    """原子领取执行权。

    Args:
        db: 会话（本函数会 commit，让状态对其它进程立即可见）。
        task_id: 任务 ID。
        stale_before: 心跳早于该时间的 running 任务视为僵尸任务，允许重新领取。

    Returns:
        True 表示领取成功（本次由调用方执行），False 表示无需执行。
    """
    now = local_now()
    claimable = [
        GenerationTask.status.in_(
            [TaskStatus.PENDING.value, TaskStatus.FAILED.value, TaskStatus.SKIPPED.value]
        )
    ]
    if stale_before is not None:
        claimable.append(
            and_(
                GenerationTask.status == TaskStatus.RUNNING.value,
                or_(
                    GenerationTask.heartbeat_at.is_(None),
                    GenerationTask.heartbeat_at < stale_before,
                ),
            )
        )

    stmt = (
        update(GenerationTask)
        .where(GenerationTask.id == task_id, or_(*claimable))
        .values(
            status=TaskStatus.RUNNING.value,
            attempts=GenerationTask.attempts + 1,
            started_at=now,
            heartbeat_at=now,
            finished_at=None,
            error_message=None,
            warnings=[],
        )
    )
    result = db.execute(stmt)
    db.commit()
    return bool(result.rowcount)


def heartbeat(db: Session, task_id: int) -> None:
    """刷新心跳，避免长任务被误判为僵尸。"""
    db.execute(
        update(GenerationTask)
        .where(GenerationTask.id == task_id)
        .values(heartbeat_at=local_now())
    )
    db.commit()


def reset_to_pending(db: Session, task: GenerationTask, *, trigger_type: str) -> None:
    """把已有任务重置为待执行（手动重新生成 / 失败重执行）。

    只重置状态字段，不删除历史记录——「同一天被重跑过几次」本身是有价值的信息
    （attempts 会继续累加）。
    """
    task.status = TaskStatus.PENDING.value
    task.trigger_type = trigger_type
    task.error_message = None
    task.warnings = []
    task.finished_at = None
    db.commit()


def mark_success(
    db: Session,
    task: GenerationTask,
    *,
    briefing_id: int,
    run_id: str | None,
    warnings: Sequence[str] | None = None,
) -> None:
    """标记任务成功。"""
    task.status = TaskStatus.SUCCESS.value
    task.briefing_id = briefing_id
    task.run_id = run_id
    task.warnings = list(warnings or [])
    task.error_message = None
    task.finished_at = local_now()
    task.heartbeat_at = task.finished_at
    db.commit()


def mark_failed(
    db: Session,
    task: GenerationTask,
    *,
    error_message: str,
    warnings: Sequence[str] | None = None,
    run_id: str | None = None,
) -> None:
    """标记任务失败（可重新执行）。"""
    task.status = TaskStatus.FAILED.value
    task.error_message = error_message[:2000]
    task.warnings = list(warnings or [])
    if run_id:
        task.run_id = run_id
    task.finished_at = local_now()
    task.heartbeat_at = task.finished_at
    db.commit()


def mark_skipped(db: Session, task: GenerationTask, *, reason: str) -> None:
    """标记任务跳过（不视为错误，不参与自动重试）。"""
    task.status = TaskStatus.SKIPPED.value
    task.error_message = reason[:2000]
    task.warnings = []
    task.finished_at = local_now()
    task.heartbeat_at = task.finished_at
    db.commit()


def count_by_status(db: Session, *, user_id: int | None = None) -> dict[str, int]:
    """按状态统计任务数（概览卡片用）。"""
    stmt = select(GenerationTask.status, func.count()).group_by(GenerationTask.status)
    if user_id is not None:
        stmt = stmt.where(GenerationTask.user_id == user_id)
    return {status: int(count) for status, count in db.execute(stmt).all()}


def latest_for_user(db: Session, user_id: int) -> GenerationTask | None:
    """某用户最近一条任务（按简报日期倒序）。"""
    stmt = (
        select(GenerationTask)
        .where(GenerationTask.user_id == user_id)
        .order_by(GenerationTask.briefing_date.desc(), GenerationTask.id.desc())
        .limit(1)
    )
    return db.scalars(stmt).first()


def list_stale_running(db: Session, stale_before: datetime) -> list[GenerationTask]:
    """列出心跳过期的 running 任务（运维排查 / 启动恢复用）。"""
    stmt = select(GenerationTask).where(
        GenerationTask.status == TaskStatus.RUNNING.value,
        or_(GenerationTask.heartbeat_at.is_(None), GenerationTask.heartbeat_at < stale_before),
    )
    return list(db.scalars(stmt).all())
