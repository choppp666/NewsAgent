"""v1 版本路由聚合。"""

from fastapi import APIRouter

from app.api.v1 import briefings, push, scheduler, subscriptions, tasks, users

api_router = APIRouter()
api_router.include_router(users.router)
api_router.include_router(subscriptions.router)
api_router.include_router(briefings.router)
# 第四阶段：任务与生成、邮件推送、定时任务
api_router.include_router(tasks.router)
api_router.include_router(push.router)
api_router.include_router(scheduler.router)

__all__ = ["api_router"]
