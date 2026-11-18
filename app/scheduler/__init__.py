"""定时任务包。

- `jobs`    : 具体作业（派发、补跑、推送重试、僵尸回收），与调度器解耦，可被直接调用
- `manager` : APScheduler 封装（注册 / 启动 / 停止 / 状态查询）
"""

from app.scheduler.jobs import (
    catch_up_missed_tasks,
    dispatch_daily_generation,
    recover_stale_tasks_job,
    retry_failed_pushes,
)
from app.scheduler.manager import SchedulerManager, scheduler_manager

__all__ = [
    "SchedulerManager",
    "scheduler_manager",
    "dispatch_daily_generation",
    "catch_up_missed_tasks",
    "retry_failed_pushes",
    "recover_stale_tasks_job",
]
