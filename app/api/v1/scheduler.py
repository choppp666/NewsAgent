"""定时任务状态接口（第四阶段）。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/scheduler` | 查看开关、下次执行时间、已注册作业 |
| POST | `/scheduler/run-now` | 立即执行一次「每日派发」（运维 / 验收用） |

`run-now` 不会改动原定计划，只是把「遍历用户 → 登记任务 → 入队」这一步立刻跑一遍；
因为任务本身有幂等约束，重复调用是安全的（已有 success 的用户会被跳过）。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query

from app.core.config import settings
from app.core.response import ApiResponse, success
from app.schemas.task import SchedulerInfoOut
from app.scheduler.manager import scheduler_manager

logger = logging.getLogger(__name__)

router = APIRouter(tags=["定时任务（第四阶段）"])


@router.get(
    "/scheduler",
    response_model=ApiResponse[SchedulerInfoOut],
    summary="定时任务状态",
    description="返回是否启用、每日执行时间与时区、下次触发时间、已注册作业列表与并发上限。",
)
def get_scheduler_info() -> dict:
    """读取调度器状态。"""
    return success(SchedulerInfoOut(**scheduler_manager.info()))


@router.post(
    "/scheduler/run-now",
    response_model=ApiResponse[dict],
    summary="立即执行一次批量派发",
    description=(
        "等价于「现在就是每天的触发时刻」，遍历全部用户并按幂等规则登记任务。"
        "已成功的用户不会重复生成；`force=true` 时才会覆盖当天已有简报（慎用）。"
    ),
)
async def run_scheduler_now(
    force: bool = Query(default=False, description="是否对已完成用户强制重跑"),
) -> dict:
    """手动触发一次批量派发。"""
    if not settings.SCHEDULER_ENABLED:
        logger.warning("SCHEDULER_ENABLED=false，但仍接受手动派发请求（用于验收）")

    result = await scheduler_manager.trigger_daily_now(force=force)
    submitted = int(result.get("submitted", 0) or 0)
    return success(result, message=f"派发完成：新提交 {submitted} 个用户任务")


@router.post(
    "/scheduler/recover",
    response_model=ApiResponse[dict],
    summary="立即回收僵尸任务",
    description=(
        "把心跳过期（超过 `SCHEDULER_STALE_MINUTES`）的 running 任务标记为失败，"
        "并把当天的任务重新派发。正常情况下由后台作业每 10 分钟自动执行，"
        "这个接口用于排查/验收时立即触发一次。"
    ),
)
async def recover_stale_tasks() -> dict:
    """立即执行一次僵尸任务回收（与后台作业同一份逻辑）。"""
    import asyncio

    from app.scheduler.jobs import recover_stale_tasks_job

    result = await asyncio.to_thread(recover_stale_tasks_job)
    reaped = int(result.get("reaped", 0) or 0)
    resubmitted = int(result.get("resubmitted", 0) or 0)
    return success(result, message=f"回收 {reaped} 个僵尸任务，重新派发 {resubmitted} 个")
