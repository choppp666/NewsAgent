"""推送记录模型（第四阶段）。

设计要点：

1. **一条任务一条推送记录**：`UNIQUE(task_id, channel)`。
   「避免重复推送」不是靠在内存里记一个 `already_pushed` 布尔值，
   而是让第二次插入直接冲突——这在多进程、重启、并发重试下都成立。
   重试走的是同一行的 `attempts += 1`（UPDATE），不会产生新行。
2. **状态与失败原因分离**：
   - `status=failed` + `last_error` 说明「尝试过但没成功」，可以重试；
   - `status=skipped/disabled` 说明「本来就不该发」（没邮箱、用户关闭、SMTP 未配置），
     这类记录不参与自动重试，否则会反复刷错误日志。
3. **next_retry_at** 让「失败重试」变成一次数据库扫描，而不是进程内定时器：
   重试扫描任务只需要 `WHERE status='failed' AND next_retry_at <= now`，
   服务重启后待重试的列表仍然在库里，不会丢。
4. **从简报派生，不复制正文**：只存 `subject`，正文在发送时从简报表读，
   避免同一份 Markdown 在两张表里各存一份导致不一致。
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import BaseEntityMixin


class PushChannel(str, enum.Enum):
    """推送渠道。当前只实现邮件，留出扩展位（企业微信 / Webhook）。"""

    EMAIL = "email"


class PushStatus(str, enum.Enum):
    """推送状态。

    - PENDING  : 正在发送（写入后立刻尝试，异常退出会停留在该状态等重试扫描兜底）
    - SUCCESS  : 已成功投递给 SMTP 服务器，记录 message_id
    - FAILED   : 发送失败，可重试（受 PUSH_MAX_ATTEMPTS 限制）
    - SKIPPED  : 不具备发送条件（无邮箱 / SMTP 未配置 / 全局开关关闭）
    - DISABLED : 用户自己关闭了推送
    """

    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    DISABLED = "disabled"


PUSH_STATUS_VALUES: tuple[str, ...] = tuple(item.value for item in PushStatus)
# 这些状态不再参与自动重试
PUSH_TERMINAL_STATUSES: tuple[str, ...] = (
    PushStatus.SUCCESS.value,
    PushStatus.SKIPPED.value,
    PushStatus.DISABLED.value,
)


class PushRecord(BaseEntityMixin, Base):
    """一次推送尝试的记录（同一任务同一渠道唯一）。"""

    __tablename__ = "push_records"

    task_id: Mapped[int] = mapped_column(
        ForeignKey("generation_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="关联的生成任务 ID",
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True, comment="收件用户 ID"
    )
    briefing_id: Mapped[int | None] = mapped_column(
        ForeignKey("news_briefings.id", ondelete="SET NULL"), nullable=True, comment="推送的简报 ID"
    )

    channel: Mapped[str] = mapped_column(
        String(20), nullable=False, default=PushChannel.EMAIL.value, comment="推送渠道"
    )
    target: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="收件地址（邮箱）")
    subject: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="邮件主题")

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=PushStatus.PENDING.value, index=True, comment="推送状态"
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="已尝试次数")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True, comment="最后一次失败原因")

    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="投递成功时间")
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="下次允许重试的时间（NULL 表示不再自动重试）"
    )
    message_id: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="SMTP 返回的 Message-ID")

    __table_args__ = (
        # 「同一任务同一渠道只能推一次」的物理保证
        UniqueConstraint("task_id", "channel", name="uq_push_task_channel"),
        # 重试扫描走这个索引（status + 时间）
        Index("ix_push_status_retry", "status", "next_retry_at"),
        Index("ix_push_user_created", "user_id", "created_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<PushRecord id={self.id} task_id={self.task_id} status={self.status}>"
