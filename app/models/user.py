"""用户模型。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import BaseEntityMixin

if TYPE_CHECKING:  # 仅用于类型提示，避免循环导入
    from app.models.subscription import Subscription


class User(BaseEntityMixin, Base):
    """用户基础信息。

    字段说明：
    - nickname     : 昵称，必填
    - identity     : 身份 / 职业描述（如「后端实习生」「算法工程师」），必填，
                     后续阶段会作为简报生成的风格与深度参考
    - email        : 新闻接收邮箱，选填；填写后第四阶段用于邮件推送
    - push_enabled : 是否接收邮件推送（默认开启）。用户关掉后定时任务仍然照常生成
                     简报并入库，只是不再发信——「不想被打扰」不等于「不想积累」。
    """

    __tablename__ = "users"

    nickname: Mapped[str] = mapped_column(String(50), nullable=False, index=True, comment="用户昵称")
    identity: Mapped[str] = mapped_column(String(200), nullable=False, comment="身份/职业描述")
    email: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True, comment="新闻接收邮箱（选填）"
    )
    push_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="1",
        comment="是否接收邮件推送（用户级开关）",
    )

    # 一个用户对应一条订阅配置（可选：未设置偏好时为 None）
    subscription: Mapped[Subscription | None] = relationship(
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
        lazy="selectin",  # 查询用户时顺带取出订阅，避免 N+1
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<User id={self.id} nickname={self.nickname!r}>"
