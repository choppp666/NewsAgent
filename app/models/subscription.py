"""订阅偏好模型。

设计说明（为什么拆成两张表）：
一个用户的关注话题、关键词、排除关键词都可能是「多个」，
如果都塞进一张表的三个字符串字段（逗号拼接）会带来：
    1. 无法按关键词精确检索、无法统计热度；
    2. 长度受字段限制，增删改需要整串重写；
    3. 后续阶段「按话题匹配新闻」只能全表取出来在内存里 split。

因此采用「订阅头表 + 词条明细表」的两表方案：
    subscriptions      : 一个用户一条订阅配置（记录创建/更新时间）
    subscription_terms : 每个话题 / 关键词 / 排除关键词占一行，用 term_type 区分

这样一次订阅可以挂任意多个词条，且天然支持索引与去重约束。
相比 SQLite 的 JSON 字段方案，关系表在跨库迁移、聚合查询上更稳妥。
"""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import BaseEntityMixin

if TYPE_CHECKING:
    from app.models.user import User


class TermType(str, enum.Enum):
    """词条类型。

    继承 str 便于直接 JSON 序列化与字符串比较；
    取值同时写入数据库 CheckConstraint，防止脏数据。
    """

    TOPIC = "topic"                    # 关注话题 / 领域
    KEYWORD = "keyword"                # 关键词
    EXCLUDE_KEYWORD = "exclude_keyword"  # 排除关键词


# 数据库层面允许的取值，与 TermType 保持一致
TERM_TYPE_VALUES: tuple[str, ...] = tuple(t.value for t in TermType)


class Subscription(BaseEntityMixin, Base):
    """订阅配置（每个用户至多一条）。"""

    __tablename__ = "subscriptions"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,   # 一对一：一个用户只有一条订阅记录
        index=True,
        comment="所属用户 ID",
    )

    user: Mapped[User] = relationship(back_populates="subscription")

    # 词条随订阅一起加载（selectin），并级联删除
    terms: Mapped[list[SubscriptionTerm]] = relationship(
        back_populates="subscription",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="SubscriptionTerm.id",
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<Subscription id={self.id} user_id={self.user_id} terms={len(self.terms)}>"


class SubscriptionTerm(BaseEntityMixin, Base):
    """订阅词条明细：一条记录对应一个话题 / 关键词 / 排除关键词。"""

    __tablename__ = "subscription_terms"

    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属订阅 ID",
    )
    term_type: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="词条类型：topic / keyword / exclude_keyword"
    )
    term_value: Mapped[str] = mapped_column(String(64), nullable=False, comment="词条内容")

    subscription: Mapped[Subscription] = relationship(back_populates="terms")

    __table_args__ = (
        # 同一订阅下，同类型词条不允许重复（数据库层兜底，应用层也会先去重）
        UniqueConstraint("subscription_id", "term_type", "term_value", name="uq_sub_term_value"),
        # 防止写入非法类型
        CheckConstraint(
            "term_type IN ('topic', 'keyword', 'exclude_keyword')", name="ck_sub_term_type"
        ),
        # 覆盖「按订阅 + 类型」批量查询（后续按话题匹配新闻时会用到）
        Index("ix_sub_term_sub_type", "subscription_id", "term_type"),
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<SubscriptionTerm {self.term_type}={self.term_value!r}>"
