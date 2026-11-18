"""APScheduler 封装。

只做三件事：注册作业、启动/停止、把运行状态暴露给接口。

为什么用 `AsyncIOScheduler`：
简报生成是 `async` 的（要 await LLM 与工具调用），AsyncIOScheduler 直接在
FastAPI 的事件循环里跑协程作业，不需要额外的线程池与 `run_until_complete` 桥接。
而推送重试是同步的 SMTP 调用，APScheduler 会把它丢进默认线程池，正好不阻塞事件循环。

重启后能自动恢复，靠的是：
1. **作业定义写在代码里**，每次进程启动都在 lifespan 里重新 `add_job`（不依赖
   持久化的 jobstore —— 本项目没有引入 APScheduler 的数据库表）；
2. **幂等体现在数据库**，即使调度器重启后重复触发，任务唯一键 + 条件 UPDATE
   也会把重复挡掉；
3. **启动补跑**（`jobs.catch_up_missed_tasks`）兜住「停机期间错过时间点」的情况。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from app.core.config import settings
from app.scheduler.jobs import (
    catch_up_missed_tasks,
    dispatch_daily_generation,
    recover_stale_tasks_job,
    retry_failed_pushes,
)

logger = logging.getLogger(__name__)

__all__ = ["SchedulerManager", "scheduler_manager"]

DAILY_JOB_ID = "daily-briefing-generation"
PUSH_RETRY_JOB_ID = "push-retry"
STALE_RECOVERY_JOB_ID = "stale-task-recovery"


class SchedulerManager:
    """定时任务管理器的生命周期封装。"""

    def __init__(self) -> None:
        self._scheduler: Any = None
        self._started_at: datetime | None = None
        self._last_dispatch: dict[str, object] | None = None

    # ---------------------------------------------------------- 状态

    @property
    def enabled(self) -> bool:
        return bool(settings.SCHEDULER_ENABLED)

    @property
    def running(self) -> bool:
        return self._scheduler is not None and bool(getattr(self._scheduler, "running", False))

    # ---------------------------------------------------------- 生命周期

    async def start(self) -> None:
        """注册作业并启动调度器（幂等，失败不阻断 Web 服务）。"""
        if not self.enabled:
            logger.warning("定时任务已关闭（SCHEDULER_ENABLED=false），仅支持手动生成")
            return
        if self.running:
            return

        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            from apscheduler.triggers.cron import CronTrigger
            from apscheduler.triggers.interval import IntervalTrigger
        except ImportError:  # pragma: no cover - 依赖缺失时降级
            logger.error("未安装 APScheduler，定时任务不可用。请执行 pip install -r requirements.txt")
            return

        hour, minute = settings.scheduler_time_parts
        timezone = settings.scheduler_timezone_obj

        scheduler = AsyncIOScheduler(
            timezone=timezone,
            job_defaults={
                # 错过触发点后的合并策略：同一次「该跑的时刻」只补一次，不堆积
                "coalesce": True,
                # 同一个作业不并发：避免上一轮还没跑完又起一轮（比如用户很多、生成很慢）
                "max_instances": 1,
                # 错过超过宽限时间就放弃（重启后由补跑逻辑负责，不在这里硬追）
                "misfire_grace_time": max(60, int(settings.SCHEDULER_MISFIRE_GRACE)),
            },
        )

        scheduler.add_job(
            dispatch_daily_generation,
            trigger=CronTrigger(hour=hour, minute=minute, timezone=timezone),
            id=DAILY_JOB_ID,
            name="每日新闻简报生成",
            replace_existing=True,
        )

        retry_minutes = max(1, int(settings.PUSH_RETRY_INTERVAL_MINUTES))
        scheduler.add_job(
            retry_failed_pushes,
            trigger=IntervalTrigger(minutes=retry_minutes),
            id=PUSH_RETRY_JOB_ID,
            name="失败推送重试",
            replace_existing=True,
        )

        recovery_minutes = max(1, int(settings.SCHEDULER_RECOVERY_INTERVAL_MINUTES))
        scheduler.add_job(
            recover_stale_tasks_job,
            trigger=IntervalTrigger(minutes=recovery_minutes),
            id=STALE_RECOVERY_JOB_ID,
            name="僵尸任务回收与重派",
            replace_existing=True,
        )

        scheduler.start()
        self._scheduler = scheduler
        self._started_at = datetime.now()
        logger.info(
            "定时任务已启动：每天 %02d:%02d（%s），推送重试每 %s 分钟，僵尸回收每 %s 分钟，并发上限 %s",
            hour,
            minute,
            settings.SCHEDULER_TIMEZONE,
            retry_minutes,
            recovery_minutes,
            settings.SCHEDULER_MAX_CONCURRENCY,
        )

    async def shutdown(self) -> None:
        """停止调度器（等待正在跑的作业结束）。"""
        if self._scheduler is None:
            return
        try:
            self._scheduler.shutdown(wait=False)
            logger.info("定时任务已停止")
        except Exception:  # noqa: BLE001 - 关闭阶段的异常不应影响进程退出
            logger.warning("停止定时任务时出现异常", exc_info=True)
        finally:
            self._scheduler = None

    # ---------------------------------------------------------- 补跑与手动触发

    async def run_catch_up(self) -> dict[str, object]:
        """执行启动补跑（在 lifespan 中调用一次）。"""
        if not self.enabled:
            return {"enabled": False, "submitted": 0}
        try:
            result = await catch_up_missed_tasks()
            self._last_dispatch = result
            return result
        except Exception:  # noqa: BLE001 - 补跑失败不能影响服务启动
            logger.exception("启动补跑执行失败")
            return {"enabled": True, "submitted": 0, "error": "catch_up_failed"}

    async def trigger_daily_now(self, *, force: bool = False) -> dict[str, object]:
        """立即执行一次「每日派发」（运维/验收用，不影响原定计划）。"""
        result = await dispatch_daily_generation(force=force)
        self._last_dispatch = result
        return result

    # ---------------------------------------------------------- 对外展示

    def info(self) -> dict[str, object]:
        """给接口用的状态快照。"""
        hour, minute = settings.scheduler_time_parts
        jobs: list[dict[str, object]] = []
        next_run = None

        if self._scheduler is not None:
            for job in self._scheduler.get_jobs():
                job_next = getattr(job, "next_run_time", None)
                jobs.append(
                    {
                        "id": job.id,
                        "name": job.name,
                        "next_run_at": job_next.isoformat(timespec="seconds") if job_next else None,
                    }
                )
                if job.id == DAILY_JOB_ID and job_next is not None:
                    next_run = job_next

        return {
            "enabled": self.enabled,
            "running": self.running,
            "time": f"{hour:02d}:{minute:02d}",
            "timezone": settings.SCHEDULER_TIMEZONE,
            "next_run_at": next_run,
            "jobs": jobs,
            "catch_up": bool(settings.SCHEDULER_CATCH_UP),
            "max_concurrency": int(settings.SCHEDULER_MAX_CONCURRENCY),
            "last_dispatch": self._last_dispatch,
        }


# 进程内单例：lifespan 启动它，接口读取它的状态
scheduler_manager = SchedulerManager()
