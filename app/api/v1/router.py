"""v1 版本路由聚合。"""

from fastapi import APIRouter

from app.api.v1 import subscriptions, users

api_router = APIRouter()
api_router.include_router(users.router)
api_router.include_router(subscriptions.router)

__all__ = ["api_router"]
