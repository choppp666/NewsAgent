"""订阅偏好相关接口。

路由前缀：/api/v1/users/{user_id}/preferences
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import CurrentUser, DbSession
from app.core.response import ApiResponse, success
from app.schemas.subscription import SubscriptionOut, SubscriptionUpsert
from app.services import subscription_service

router = APIRouter(prefix="/users/{user_id}/preferences", tags=["订阅偏好"])


@router.put(
    "",
    response_model=ApiResponse[SubscriptionOut],
    summary="创建或更新订阅偏好",
    description=(
        "幂等覆盖写入：首次调用创建订阅，之后调用则用请求体**整份替换**原有配置。\n\n"
        "三个数组均可为空/省略，表示该维度不设置词条；\n"
        "服务端会自动去重、去首尾空格，并做数量与长度校验。"
    ),
)
def upsert_preferences(payload: SubscriptionUpsert, user: CurrentUser, db: DbSession) -> dict:
    subscription = subscription_service.upsert_subscription(db, user.id, payload)
    return success(SubscriptionOut.from_model(subscription), message="订阅偏好已保存")


@router.get(
    "",
    response_model=ApiResponse[SubscriptionOut | None],
    summary="获取订阅偏好",
    description="用户已配置时返回配置内容；从未配置过则返回 data=null。",
)
def get_preferences(user: CurrentUser, db: DbSession) -> dict:
    subscription = subscription_service.get_subscription(db, user.id)
    if subscription is None:
        return success(None, message="该用户尚未设置订阅偏好")
    return success(SubscriptionOut.from_model(subscription))
