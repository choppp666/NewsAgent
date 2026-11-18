"""订阅偏好业务服务。

核心接口是一个「upsert」语义：用户第一次保存时新建，之后保存则整条覆盖。
之所以不做局部更新，是因为前端表单天然是「整份提交」，
覆盖语义能避免「删了标签但数据库没删」这类前后端不一致问题。
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.crud import subscription as subscription_crud
from app.crud import user as user_crud
from app.models.base import local_now
from app.models.subscription import Subscription, TermType
from app.schemas.subscription import SubscriptionUpsert

logger = logging.getLogger(__name__)


def get_subscription(db: Session, user_id: int) -> Subscription | None:
    """获取用户订阅偏好。

    先校验用户是否存在（不存在 -> 404），再查订阅；
    用户存在但从未保存过偏好时返回 None，由接口层转成 data=null。
    """
    if user_crud.get(db, user_id) is None:
        raise NotFoundError(f"用户不存在（id={user_id}）")
    return subscription_crud.get_by_user_id(db, user_id)


def upsert_subscription(db: Session, user_id: int, payload: SubscriptionUpsert) -> Subscription:
    """创建或更新订阅偏好（全量覆盖）。"""
    if user_crud.get(db, user_id) is None:
        raise NotFoundError(f"用户不存在（id={user_id}），请先创建用户")

    subscription = subscription_crud.get_by_user_id(db, user_id)
    if subscription is None:
        subscription = subscription_crud.create(db, user_id)
        logger.info("首次创建订阅: user_id=%s", user_id)

    terms_by_type = {
        TermType.TOPIC.value: payload.topics,
        TermType.KEYWORD.value: payload.keywords,
        TermType.EXCLUDE_KEYWORD.value: payload.exclude_keywords,
    }
    subscription_crud.replace_terms(db, subscription, terms_by_type)

    # 词条变化不会触发 subscriptions 行本身的 UPDATE，
    # 因此这里显式刷新更新时间，保证「更新时间」真实反映偏好最后一次修改。
    subscription.updated_at = local_now()

    db.commit()
    db.refresh(subscription)
    logger.info(
        "保存订阅成功: user_id=%s topics=%d keywords=%d exclude=%d",
        user_id,
        len(payload.topics),
        len(payload.keywords),
        len(payload.exclude_keywords),
    )
    return subscription
