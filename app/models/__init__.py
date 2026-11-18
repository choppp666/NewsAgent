"""ORM 模型统一出口。

必须在这里 import 所有模型，保证 Base.metadata 完整注册，
否则 init_db() 建表时会漏表。
"""

from app.models.base import BaseEntityMixin, local_now
from app.models.briefing import NewsBriefing
from app.models.subscription import (
    TERM_TYPE_VALUES,
    Subscription,
    SubscriptionTerm,
    TermType,
)
from app.models.user import User

__all__ = [
    "BaseEntityMixin",
    "local_now",
    "User",
    "Subscription",
    "SubscriptionTerm",
    "TermType",
    "TERM_TYPE_VALUES",
    "NewsBriefing",
]
