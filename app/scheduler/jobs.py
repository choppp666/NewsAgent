"""定时任务的具体作业（与调度器解耦）。

**为什么要和调度器分开**：
调度器代码（`manager.py`）只关心「什么时候触发、触发失败怎么办」；
这里只关心「触发后要做什么」。这样：

- 作业可以直接被接口 / 测试调用（`await dispatch_daily_generation()`），
  不需要真的等到明天早上八点；
- 换成 Celery、系统 crontab、K8s CronJob 时，删除 `manager.py` 即可，
  业务逻辑一行都不用改。

三个作业：

| 作业 | 触发方式 | 职责 |
| --- | --- | --- |
| `dispatch_daily_generation` | 每天固定时间 | 遍历用户 → 逐个登记任务 → 交给 worker |
| `catch_up_missed_tasks` | 服务启动时 | 补做「今天该做但没做」的任务 |
| `retry_failed_pushes` | 每 N 分钟 | 重发失败的邮件（不重新生成简报） |
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time

from app.core.config import settings
from app.crud import user as user_crud
from app.models.task import TaskStatus, TriggerType
from app.services import push_service, task_service
from app.services.generation_worker import generation_worker

logger = logging.getLogger(__name__)

__all__ = [
    "DispatchStats",
    "dispatch_daily_generation",
    "catch_up_missed_tasks",
    "retry_failed_pushes",
    "recover_stale_tasks_job",
]


@dataclass(slots=True)
class DispatchStats:
    """一次批量派发的统计。"""

    briefing_date: date
    trigger: str
    users: int = 0
    submitted: int = 0
    reused: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "briefing_date": self.briefing_date.isoformat(),
            "trigger": self.trigger,
            "users": self.users,
            "submitted": self.submitted,
            "reused": self.reused,
            "failed": self.failed,
            "errors": self.errors[:10],
        }


def _today_for_scheduler() -> date:
    """按调度器时区取「今天」。

    为什么不直接用 `date.today()`：`SCHEDULER_TIMEZONE` 可能和服务器时区不同
    （例如服务器是 UTC、业务在东八区）。「每天 08:00」属于业务时区，
    任务归属的日期也应该按业务时区算，否则会出现「早上八点生成的是昨天的简报」。
    """
    tz = settings.scheduler_timezone_obj
    try:
        return datetime.now(tz).date()
    except Exception:  # noqa: BLE001 - tz 对象异常时退回系统本地时间
        return date.today()


async def dispatch_daily_generation(
    *,
    trigger: TriggerType | str = TriggerType.SCHEDULED,
    target_date: date | None = None,
    force: bool = False,
) -> dict[str, object]:
    """为所有用户派发当天的简报生成任务。

    关键点：**每个用户独立 try/except**。某个用户的数据异常（画像读取失败、
    唯一键冲突、甚至数据库瞬时锁）不会中断整个循环——这正是「单个用户任务失败
    不能影响其他用户」的落点。

    Args:
        trigger: 触发来源，写入任务记录。
        target_date: 目标日期，默认按调度器时区取今天。
        force: 当天已生成时是否强制重跑（定时任务永远传 False）。

    Returns:
        统计字典（写入日志，也便于接口返回）。
    """
    day = target_date or _today_for_scheduler()
    stats = DispatchStats(briefing_date=day, trigger=trigger.value if isinstance(trigger, TriggerType) else str(trigger))

    from app.core.database import SessionLocal

    # 只取用户 ID：逐个用户使用独立会话，避免一个坏会话污染整批
    with SessionLocal() as db:
        user_ids = user_crud.list_ids(db)

    stats.users = len(user_ids)
    if not user_ids:
        logger.info("定时任务：当前没有用户，跳过本次派发")
        return stats.to_dict()

    logger.info("定时任务开始：日期=%s 触发=%s 用户数=%s", day, stats.trigger, len(user_ids))

    for user_id in user_ids:
        try:
            with SessionLocal() as db:
                result = task_service.get_or_create(
                    db,
                    user_id=user_id,
                    briefing_date=day,
                    trigger_type=trigger,
                    force=force,
                )
            if result.needs_run:
                outcome = generation_worker.submit(result.task.id)
                if outcome == "duplicate":
                    stats.reused += 1
                else:
                    stats.submitted += 1
                logger.info(
                    "用户 %s 任务已派发：task_id=%s action=%s 入队=%s",
                    user_id,
                    result.task.id,
                    result.action,
                    outcome,
                )
            else:
                stats.reused += 1
                logger.info("用户 %s 无需重复生成：%s（task_id=%s）", user_id, result.action, result.task.id)
        except Exception as exc:  # noqa: BLE001 - 单用户失败必须被隔离
            stats.failed += 1
            stats.errors.append(f"user {user_id}: {exc}")
            logger.exception("用户 %s 的任务派发失败（不影响其它用户）", user_id)

    logger.info(
        "定时任务派发完成：用户 %s，提交 %s，复用/跳过 %s，失败 %s",
        stats.users,
        stats.submitted,
        stats.reused,
        stats.failed,
    )
    return stats.to_dict()


async def catch_up_missed_tasks(*, target_date: date | None = None) -> dict[str, object]:
    """启动补跑：把「今天该做但没做」的任务补上。

    触发条件（三个都满足才补）：
    1. `SCHEDULER_CATCH_UP=true`；
    2. 当前时间已过今天的预定执行时间（没过就说明还没到点，不算漏）；
    3. 当天该用户没有 success 任务（failed / 不存在 / 僵尸 running 都算「该补」）。

    为什么需要它：服务如果在 07:50 挂了、09:00 才起来，APScheduler 的
    misfire 机制只对「进程还活着」的情况有效；进程重启后是全新调度器，
    不会知道自己错过了一次。补跑把「重启」这个场景兜住。
    """
    if not settings.SCHEDULER_CATCH_UP:
        logger.info("启动补跑未启用（SCHEDULER_CATCH_UP=false）")
        return {"enabled": False, "submitted": 0}

    day = target_date or _today_for_scheduler()
    hour, minute = settings.scheduler_time_parts
    tz = settings.scheduler_timezone_obj

    try:
        now = datetime.now(tz)
        scheduled_at = datetime.combine(day, time(hour=hour, minute=minute), tzinfo=tz)
    except Exception:  # noqa: BLE001 - 时区异常时保守跳过补跑
        logger.warning("补跑时间计算失败，跳过补跑", exc_info=True)
        return {"enabled": True, "submitted": 0, "skipped": "time_calc_failed"}

    if now < scheduled_at:
        logger.info("尚未到今日执行时间（%s），无需补跑", scheduled_at.strftime("%H:%M"))
        return {"enabled": True, "submitted": 0, "reason": "before_scheduled_time"}

    from app.core.database import SessionLocal
    from app.crud import task as task_crud

    with SessionLocal() as db:
        user_ids = user_crud.list_ids(db)

    submitted = 0
    skipped = 0
    failed = 0
    for user_id in user_ids:
        try:
            with SessionLocal() as db:
                existing = task_crud.get_by_user_date(db, user_id, day)
                if existing is not None and existing.status == TaskStatus.SUCCESS.value:
                    skipped += 1
                    continue
                result = task_service.get_or_create(
                    db,
                    user_id=user_id,
                    briefing_date=day,
                    trigger_type=TriggerType.CATCH_UP,
                    force=False,
                )
            if result.needs_run:
                generation_worker.submit(result.task.id)
                submitted += 1
                logger.info("补跑用户 %s：task_id=%s action=%s", user_id, result.task.id, result.action)
            else:
                skipped += 1
        except Exception:  # noqa: BLE001 - 单用户失败隔离
            failed += 1
            logger.exception("补跑用户 %s 失败（不影响其它用户）", user_id)

    logger.info("启动补跑完成：提交 %s，跳过 %s，失败 %s", submitted, skipped, failed)
    return {"enabled": True, "date": day.isoformat(), "submitted": submitted, "skipped": skipped, "failed": failed}


def retry_failed_pushes() -> dict[str, int]:
    """重试失败的邮件推送（同步作业，由调度器放入线程池执行）。

    单独成作业的原因：推送失败往往是临时的（SMTP 连接抖动、被限流），
    而重新生成简报要花几分钟和真金白银。重试只重发同一份已入库的简报。
    """
    try:
        stats = push_service.retry_due_pushes()
        if stats.get("scanned"):
            logger.info("推送重试作业：%s", stats)
        return stats
    except Exception:  # noqa: BLE001 - 作业异常不能带崩调度器
        logger.exception("推送重试作业执行失败")
        return {"scanned": 0, "succeeded": 0, "failed": 0, "skipped": 0}


def recover_stale_tasks_job(max_attempts: int = 3) -> dict[str, object]:
    """回收僵尸任务并把当天的任务重新派发（自愈作业）。

    为什么需要独立的定时作业，而不是只在「下次触发时顺手处理」：
    如果任务在早上 08:00 执行到一半进程被杀，下一次定时触发是**明天** 08:00，
    用户今天就没有简报了。这个作业每 10 分钟扫一次，把心跳过期（默认 30 分钟）
    的 running 任务标记失败并立刻重新入队，当天就能补上。

    `max_attempts` 是防止「每次都崩」的无限重试：同一个任务被回收超过 3 次，
    就只标记失败、不再自动重派，交给用户手动重试并排查原因。

    Returns:
        统计字典。
    """
    from app.core.database import SessionLocal

    reaped_count = 0
    resubmitted = 0
    exhausted = 0
    pending_dispatch: list[tuple[int, int, str]] = []

    try:
        with SessionLocal() as db:
            reaped = task_service.reap_stale_tasks(db)
            reaped_count = len(reaped)
            for task in reaped:
                if task.attempts < max_attempts:
                    pending_dispatch.append((task.id, task.user_id, str(task.briefing_date)))
            exhausted = reaped_count - len(pending_dispatch)
    except Exception:  # noqa: BLE001 - 作业异常不能带崩调度器
        logger.exception("僵尸任务回收作业执行失败")
        return {"reaped": 0, "resubmitted": 0, "failed": 0}

    for task_id, user_id, day in pending_dispatch:
        try:
            with SessionLocal() as db:
                result = task_service.get_or_create(
                    db,
                    user_id=user_id,
                    briefing_date=date.fromisoformat(day),
                    trigger_type=TriggerType.RETRY,
                    force=False,
                )
            if result.needs_run:
                generation_worker.submit(result.task.id)
                resubmitted += 1
                logger.info("僵尸任务已重新派发：原 task_id=%s → user_id=%s date=%s", task_id, user_id, day)
        except Exception:  # noqa: BLE001 - 单个用户失败不影响其它用户
            logger.exception("僵尸任务重派失败：task_id=%s user_id=%s", task_id, user_id)

    if reaped_count:
        logger.warning(
            "僵尸任务回收：发现 %s 个，重新派发 %s 个，超出重试上限 %s 个",
            reaped_count,
            resubmitted,
            exhausted,
        )
    return {"reaped": reaped_count, "resubmitted": resubmitted, "failed": exhausted}

