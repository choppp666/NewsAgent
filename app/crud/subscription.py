"""订阅偏好数据访问层。

一个用户对应一条 Subscription，其下挂多条 SubscriptionTerm。
"""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.subscription import Subscription, SubscriptionTerm


def get_by_user_id(db: Session, user_id: int) -> Subscription | None:
    """查询某用户的订阅配置，不存在返回 None。"""
    stmt = select(Subscription).where(Subscription.user_id == user_id).limit(1)
    return db.scalars(stmt).first()


def create(db: Session, user_id: int) -> Subscription:
    """为指定用户新建一条空订阅配置（未提交）。"""
    subscription = Subscription(user_id=user_id)
    db.add(subscription)
    db.flush()
    return subscription


def replace_terms(db: Session, subscription: Subscription, terms_by_type: Mapping[str, list[str]]) -> None:
    """全量替换订阅词条（先删后插）。

    实现要点：
    1. 通过关系集合 remove/append，而不是写原生 DELETE —— 这样 ORM 的
       delete-orphan 级联与会话状态保持一致，不会出现「已删除对象仍在内存」的问题；
    2. 删除后先 flush，再插入新词条。否则 SQLAlchemy 的工作单元可能把
       INSERT 排在 DELETE 之前，触发 (subscription_id, term_type, term_value)
       唯一约束冲突；
    3. 参数 terms_by_type 形如 {"topic": [...], "keyword": [...], "exclude_keyword": [...]}。

    未提交，事务由 service 层统一 commit。
    """
    # ---- 1. 清空旧词条 ----
    if subscription.terms:
        for term in list(subscription.terms):
            subscription.terms.remove(term)  # delete-orphan 会标记为删除
        db.flush()  # 先落 DELETE，避免与下方 INSERT 抢唯一键

    # ---- 2. 写入新词条 ----
    for term_type, values in terms_by_type.items():
        for value in values:
            subscription.terms.append(SubscriptionTerm(term_type=term_type, term_value=value))

    db.flush()
