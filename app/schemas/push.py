"""推送相关请求 / 响应模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class PushRecordOut(BaseModel):
    """一条推送记录。"""

    id: int
    task_id: int
    user_id: int
    briefing_id: int | None = None
    channel: str
    target: str | None = None
    subject: str | None = None
    status: str
    attempts: int
    last_error: str | None = None
    sent_at: datetime | None = None
    next_retry_at: datetime | None = None

    @classmethod
    def from_model(cls, record) -> PushRecordOut:  # noqa: ANN001 - ORM 对象
        return cls(
            id=record.id,
            task_id=record.task_id,
            user_id=record.user_id,
            briefing_id=record.briefing_id,
            channel=record.channel,
            target=record.target,
            subject=record.subject,
            status=record.status,
            attempts=record.attempts,
            last_error=record.last_error,
            sent_at=record.sent_at,
            next_retry_at=record.next_retry_at,
        )


class PushListData(BaseModel):
    """推送记录分页列表。"""

    total: int
    page: int
    page_size: int
    items: list[PushRecordOut]


class PushSettingsIn(BaseModel):
    """更新用户推送开关。"""

    push_enabled: bool = Field(..., description="是否接收邮件推送")


class PushSettingsOut(BaseModel):
    """推送配置视图：把「用户意愿」和「服务端能力」放在一起，前端才能给出准确提示。

    例如用户开了开关但没填邮箱、或者服务端根本没配 SMTP，
    前端应该直接说明「为什么暂时收不到邮件」，而不是让用户以为已经开通。
    """

    user_id: int
    push_enabled: bool
    email: str | None = None
    global_enabled: bool = Field(description="服务端全局推送开关 PUSH_ENABLED")
    smtp_configured: bool = Field(description="SMTP_HOST 等是否已配置")
    smtp_missing: list[str] = Field(
        default_factory=list,
        description="缺少的 SMTP 配置项（前端直接展示，避免用户对着「未配置」四个字猜）",
    )
    sender: str | None = Field(default=None, description="发件人地址（不含密码）")

    @property
    def deliverable(self) -> bool:
        """三个条件同时满足才会真的发信。"""
        return bool(self.push_enabled and self.email and self.global_enabled and self.smtp_configured)

    @classmethod
    def from_user(cls, user: Any) -> PushSettingsOut:
        """按「当前用户 + 当前服务端配置」组装。

        统一收在这里，是因为概览接口和推送设置接口都要返回同一份信息；
        分别拼装很容易出现「一处补了新字段、另一处忘了」的漂移。
        """
        from app.core.config import settings

        return cls(
            user_id=user.id,
            push_enabled=bool(user.push_enabled),
            email=user.email,
            global_enabled=bool(settings.PUSH_ENABLED),
            smtp_configured=settings.smtp_configured,
            smtp_missing=settings.smtp_missing_items,
            sender=settings.smtp_sender_address or None,
        )
