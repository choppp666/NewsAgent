"""任务状态管理服务（第四阶段）。

这一层只关心「谁可以执行、执行结果怎么记」，**不关心简报怎么生成**——
生成逻辑在 `generation_service`，推送逻辑在 `push_service`。
分开的好处：任务调度代码不会长成一棵包含搜索、写作、发信的巨型函数。

三条触发路径（手动 / 定时 / 补跑）最终都调用 `get_or_create()`，
幂等规则集中在一处，不需要每条路径各写一遍判断：

| 已有状态 | 手动触发 | 定时触发 |
| --- | --- | --- |
| 不存在 | 新建 → created | 新建 → created |
| pending / failed / skipped | 重置 → resumed | 重置 → resumed |
| running（心跳新鲜） | already_running | already_running |
| running（心跳过期） | 视为崩溃遗留 → resumed | 同左 |
| success | force=true → regenerated；否则 already_generated | already_generated |

注意「重置为 pending」不等于「一定执行」：真正抢执行权的是 `crud.claim()` 的条件 UPDATE，
所以即使两个请求都拿到同一条 pending 任务，也只有一个能跑起来。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import NotFoundError
from app.crud import push as push_crud
from app.crud import task as task_crud
from app.crud import user as user_crud
from app.models.base import local_now
from app.models.briefing import NewsBriefing
from app.models.push import PushRecord
from app.models.task import GenerationTask, TaskStatus, TriggerType

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TriggerResult:
    """一次触发的结论。"""

    task: GenerationTask
    action: str

    @property
    def needs_run(self) -> bool:
        """是否需要真正执行（false 表示前端继续轮询已有任务即可）。"""
        return self.action in ("created", "resumed", "regenerated")


def stale_before() -> datetime:
    """心跳早于该时间点的 running 任务视为「进程崩溃遗留」。"""
    return local_now() - timedelta(minutes=max(1, settings.SCHEDULER_STALE_MINUTES))


def _is_stale(task: GenerationTask) -> bool:
    heartbeat = task.heartbeat_at or task.started_at or task.created_at
    if heartbeat is None:
        return True
    return heartbeat < stale_before()


def get_or_create(
    db: Session,
    *,
    user_id: int,
    briefing_date: date,
    trigger_type: TriggerType | str = TriggerType.MANUAL,
    force: bool = False,
) -> TriggerResult:
    """登记（或复用）一条「用户 + 日期」任务。

    Args:
        db: 会话（本函数内部提交事务）。
        user_id: 用户 ID。
        briefing_date: 任务归属日期。
        trigger_type: 触发来源，写入任务记录便于排查。
        force: 当天已生成时是否强制重跑。

    Returns:
        TriggerResult，`action` 取值见模块文档表。

    Raises:
        NotFoundError: 用户不存在。
    """
    if user_crud.get(db, user_id) is None:
        raise NotFoundError(f"用户不存在（id={user_id}）")

    trigger = trigger_type.value if isinstance(trigger_type, TriggerType) else str(trigger_type)
    existing = task_crud.get_by_user_date(db, user_id, briefing_date)

    if existing is None:
        try:
            task = task_crud.create(
                db,
                user_id=user_id,
                briefing_date=briefing_date,
                trigger_type=trigger,
                status=TaskStatus.PENDING.value,
                attempts=0,
                warnings=[],
            )
            db.commit()
            db.refresh(task)
            logger.info("登记生成任务: task_id=%s user_id=%s date=%s trigger=%s", task.id, user_id, briefing_date, trigger)
            return TriggerResult(task=task, action="created")
        except IntegrityError:
            # 并发插入：说明另一个请求刚刚建好了，回退后按「已有记录」处理
            db.rollback()
            existing = task_crud.get_by_user_date(db, user_id, briefing_date)
            if existing is None:  # pragma: no cover - 理论上不会发生
                raise
            logger.info("任务已存在（并发登记）: user_id=%s date=%s", user_id, briefing_date)

    # ---------- 已有记录：按状态决定是否可执行 ----------
    if existing.status == TaskStatus.RUNNING.value and not _is_stale(existing):
        logger.info(
            "任务正在执行，跳过重复触发: task_id=%s user_id=%s（心跳 %s）",
            existing.id,
            user_id,
            existing.heartbeat_at,
        )
        return TriggerResult(task=existing, action="already_running")

    if existing.status == TaskStatus.SUCCESS.value and not force:
        logger.info("当天已生成，跳过: task_id=%s user_id=%s date=%s", existing.id, user_id, briefing_date)
        return TriggerResult(task=existing, action="already_generated")

    action = "regenerated" if existing.status == TaskStatus.SUCCESS.value else "resumed"
    previous = existing.status
    task_crud.reset_to_pending(db, existing, trigger_type=trigger)
    db.refresh(existing)
    logger.info("复用任务 %s: task_id=%s user_id=%s status=%s->pending", action, existing.id, user_id, previous)
    return TriggerResult(task=existing, action=action)


def claim(db: Session, task_id: int) -> GenerationTask | None:
    """尝试领取执行权。

    Returns:
        领取成功返回任务对象；失败（别人在跑 / 已成功 / 不存在）返回 None。
    """
    if not task_crud.claim(db, task_id, stale_before=stale_before()):
        return None
    task = task_crud.get(db, task_id)
    if task is not None:
        logger.info(
            "任务开始执行: task_id=%s user_id=%s date=%s 第 %s 次尝试",
            task.id,
            task.user_id,
            task.briefing_date,
            task.attempts,
        )
    return task


def succeed(
    db: Session,
    task: GenerationTask,
    *,
    briefing: NewsBriefing,
    run_id: str | None,
    warnings: list[str] | None = None,
) -> GenerationTask:
    """记录生成成功。"""
    task_crud.mark_success(db, task, briefing_id=briefing.id, run_id=run_id, warnings=warnings)
    logger.info("任务成功: task_id=%s user_id=%s briefing_id=%s", task.id, task.user_id, briefing.id)
    return task


def fail(
    db: Session,
    task: GenerationTask,
    *,
    message: str,
    warnings: list[str] | None = None,
    run_id: str | None = None,
) -> GenerationTask:
    """记录生成失败（保留任务行，允许重新执行）。"""
    task_crud.mark_failed(db, task, error_message=message, warnings=warnings, run_id=run_id)
    logger.warning("任务失败: task_id=%s user_id=%s 原因=%s", task.id, task.user_id, message[:200])
    return task


def skip(db: Session, task: GenerationTask, *, reason: str) -> GenerationTask:
    """记录「主动跳过」（例如未配置订阅偏好）。"""
    task_crud.mark_skipped(db, task, reason=reason)
    logger.info("任务跳过: task_id=%s user_id=%s 原因=%s", task.id, task.user_id, reason)
    return task


# ---------------------------------------------------------------- 查询视图
#
# 列表页需要「任务 + 简报 + 推送」三份数据。用逐条 lazy load 会产生 N+1 查询
# （20 条任务 = 61 次 SQL），这里改成三次批量查询后在内存里拼装。


@dataclass(slots=True)
class TaskView:
    """任务及其关联数据的展示组合。"""

    task: GenerationTask
    briefing: NewsBriefing | None = None
    push: PushRecord | None = None


def build_views(db: Session, tasks: list[GenerationTask]) -> list[TaskView]:
    """批量补齐简报与推送记录，避免 N+1。"""
    if not tasks:
        return []

    briefing_ids = {task.briefing_id for task in tasks if task.briefing_id}
    task_ids = [task.id for task in tasks]

    briefings: dict[int, NewsBriefing] = {}
    if briefing_ids:
        rows = db.query(NewsBriefing).filter(NewsBriefing.id.in_(briefing_ids)).all()
        briefings = {row.id: row for row in rows}

    pushes: dict[int, PushRecord] = {}
    if task_ids:
        rows = db.query(PushRecord).filter(PushRecord.task_id.in_(task_ids)).all()
        # 同一任务可能有多个渠道，当前只展示邮件
        for row in rows:
            if row.channel == "email" or row.task_id not in pushes:
                pushes[row.task_id] = row

    return [
        TaskView(
            task=task,
            briefing=briefings.get(task.briefing_id) if task.briefing_id else None,
            push=pushes.get(task.id),
        )
        for task in tasks
    ]


def list_tasks(
    db: Session,
    user_id: int,
    *,
    page: int = 1,
    page_size: int = 20,
    status: str | None = None,
) -> tuple[list[TaskView], int]:
    """分页列出某用户的任务（含简报与推送摘要）。"""
    if user_crud.get(db, user_id) is None:
        raise NotFoundError(f"用户不存在（id={user_id}）")
    tasks, total = task_crud.list_by_user(db, user_id, limit=page_size, offset=(page - 1) * page_size, status=status)
    return build_views(db, list(tasks)), total


def reap_stale_tasks(db: Session) -> list[GenerationTask]:
    """回收「心跳过期但状态仍是 running」的任务。

    为什么必须有这一步：
    - 只靠「下次触发时顺手领取」不够——如果任务在 08:00 中断，下一次定时是明天 08:00，
      用户会在这一天里一直看到「正在生成」，等于当天没有简报；
    - 直接允许抢占也有风险——多进程部署时，另一个进程可能真的还在跑。

    折中：心跳超过 `SCHEDULER_STALE_MINUTES`（默认 30 分钟）就认定执行者已死，
    标记为 failed 并写明原因，由调用方（维护作业）决定是否立刻重新派发。
    用 `stale_before` 作为时间闸，既不会误杀正在跑的任务，也不会让僵尸任务永久占位。

    Args:
        db: 数据库会话。

    Returns:
        被回收的任务列表（调用方可据此重新入队）。
    """
    stale = task_crud.list_stale_running(db, stale_before())
    reaped: list[GenerationTask] = []
    for task in stale:
        reason = (
            f"执行中断（心跳停在 {task.heartbeat_at or task.started_at}，"
            f"超过 {settings.SCHEDULER_STALE_MINUTES} 分钟未更新），已回收"
        )
        task_crud.mark_failed(db, task, error_message=reason)
        reaped.append(task)
        logger.warning("回收僵尸任务: task_id=%s user_id=%s date=%s 原因=%s", task.id, task.user_id, task.briefing_date, reason)
    return reaped



def get_view(db: Session, task_id: int) -> TaskView:
    """取单条任务视图。

    Raises:
        NotFoundError: 任务不存在。
    """
    task = task_crud.get(db, task_id)
    if task is None:
        raise NotFoundError(f"任务不存在（id={task_id}）")
    return build_views(db, [task])[0]


def latest_view(db: Session, user_id: int) -> TaskView | None:
    """某用户最近一条任务视图。"""
    task = task_crud.latest_for_user(db, user_id)
    if task is None:
        return None
    return build_views(db, [task])[0]


def running_view(db: Session, user_id: int) -> TaskView | None:
    """某用户当前正在执行的任务（前端刷新后据此恢复轮询）。"""
    tasks, _ = task_crud.list_by_user(db, user_id, limit=5)
    for task in tasks:
        if task.status == TaskStatus.RUNNING.value:
            return build_views(db, [task])[0]
    return None


def push_of_task(db: Session, task_id: int) -> PushRecord | None:
    """取某任务的推送记录（邮件渠道）。"""
    return push_crud.get_by_task(db, task_id)


def backfill_from_briefings(db: Session) -> int:
    """为「第三阶段生成的简报」补建任务记录（升级对齐，幂等）。

    第三阶段的简报只有 `news_briefings` 行，没有任务记录。第四阶段的历史列表
    以任务表为主视图，如果不补，老简报会在列表里「消失」——数据还在，
    但用户看到的是空白，比丢数据更让人困惑。

    补出来的记录一律标记为 success（简报确实存在），`started_at` 取生成时间，
    `trigger_type` 记为 scheduled（第三阶段只有这条路径）。

    Args:
        db: 数据库会话。

    Returns:
        本次补建的条数。
    """
    from sqlalchemy import select

    from app.models.briefing import NewsBriefing

    briefings = db.scalars(select(NewsBriefing).order_by(NewsBriefing.id.asc())).all()
    created = 0

    for briefing in briefings:
        if task_crud.get_by_user_date(db, briefing.user_id, briefing.briefing_date) is not None:
            continue
        try:
            task_crud.create(
                db,
                user_id=briefing.user_id,
                briefing_date=briefing.briefing_date,
                trigger_type=TriggerType.SCHEDULED.value,
                status=TaskStatus.SUCCESS.value,
                attempts=1,
                started_at=briefing.generated_at,
                finished_at=briefing.generated_at,
                heartbeat_at=briefing.generated_at,
                briefing_id=briefing.id,
                run_id=briefing.run_id,
                warnings=[],
            )
            db.commit()
            created += 1
        except IntegrityError:
            db.rollback()
        except Exception:  # noqa: BLE001 - 单条补建失败不影响启动
            db.rollback()
            logger.warning("补建任务记录失败: briefing_id=%s", briefing.id, exc_info=True)

    if created:
        logger.info("已为 %s 份历史简报补建任务记录", created)
    return created

