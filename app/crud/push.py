"""推送记录数据访问层。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.push import PUSH_TERMINAL_STATUSES, PushChannel, PushRecord, PushStatus

# 判断「跳过原因是服务端配置缺失」的标记：`_blocked_reason` 里的配置类文案都带 SMTP
CONFIG_SKIP_MARKER = "SMTP"


def get(db: Session, push_id: int) -> PushRecord | None:
    """按主键查询。"""
    return db.get(PushRecord, push_id)


def get_by_task(db: Session, task_id: int, channel: str = PushChannel.EMAIL.value) -> PushRecord | None:
    """查询某任务某渠道的推送记录（唯一键查询）。"""
    stmt = (
        select(PushRecord)
        .where(PushRecord.task_id == task_id, PushRecord.channel == channel)
        .limit(1)
    )
    return db.scalars(stmt).first()


def create(db: Session, **fields: object) -> PushRecord:
    """新建推送记录（未提交；唯一键冲突由调用方处理）。"""
    record = PushRecord(**fields)
    db.add(record)
    db.flush()
    return record


def list_by_user(
    db: Session,
    user_id: int,
    *,
    limit: int = 20,
    offset: int = 0,
) -> tuple[Sequence[PushRecord], int]:
    """按创建时间倒序分页列出某用户的推送记录。"""
    base = select(PushRecord).where(PushRecord.user_id == user_id)
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    stmt = base.order_by(PushRecord.id.desc()).limit(limit).offset(offset)
    return db.scalars(stmt).all(), int(total)


def latest_for_user(db: Session, user_id: int) -> PushRecord | None:
    """用户最近一次推送记录。"""
    stmt = select(PushRecord).where(PushRecord.user_id == user_id).order_by(PushRecord.id.desc()).limit(1)
    return db.scalars(stmt).first()


def list_retryable(db: Session, *, now: datetime, max_attempts: int, limit: int = 50) -> list[PushRecord]:
    """扫描「可以重试」的失败记录。

    条件里刻意排除终态（success / skipped / disabled）：
    它们不是「发送失败」，重试没有意义，反复重试只会刷错误日志。
    """
    stmt = (
        select(PushRecord)
        .where(
            PushRecord.status == PushStatus.FAILED.value,
            PushRecord.attempts < max_attempts,
            or_(PushRecord.next_retry_at.is_(None), PushRecord.next_retry_at <= now),
            PushRecord.status.notin_(list(PUSH_TERMINAL_STATUSES)),
        )
        .order_by(PushRecord.next_retry_at.asc(), PushRecord.id.asc())
        .limit(limit)
    )
    return list(db.scalars(stmt).all())


def list_config_skipped(db: Session, *, since: datetime, limit: int = 50) -> list[PushRecord]:
    """扫描「因为服务端配置缺失而被跳过」的记录。

    与 `list_retryable` 的区别：这里捞的是终态 skipped 里**原因在服务端**的那部分。
    因为 skipped 是终态，`list_retryable` 永远不会看它，于是「补上配置后历史简报
    仍不自动补发」——这正是需要单独扫一遍的理由。

    筛选用 `last_error LIKE '%SMTP%'`：用户侧的原因（未填邮箱、关了开关）
    文案里不含 SMTP，天然被排除，只有「服务端没配好」的记录会被选中。
    """
    stmt = (
        select(PushRecord)
        .where(
            PushRecord.status == PushStatus.SKIPPED.value,
            PushRecord.last_error.like(f"%{CONFIG_SKIP_MARKER}%"),
            PushRecord.created_at >= since,
        )
        .order_by(PushRecord.id.asc())
        .limit(limit)
    )
    return list(db.scalars(stmt).all())


def count_by_status(db: Session, *, user_id: int | None = None) -> dict[str, int]:
    """按状态统计推送数。"""
    stmt = select(PushRecord.status, func.count()).group_by(PushRecord.status)
    if user_id is not None:
        stmt = stmt.where(PushRecord.user_id == user_id)
    return {status: int(count) for status, count in db.execute(stmt).all()}
