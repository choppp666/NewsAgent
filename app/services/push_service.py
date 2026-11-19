"""推送业务服务：把「生成好的简报」通过邮件发给用户，并记录每一次尝试。

三条铁律：

1. **推送失败绝不影响生成结果**。生成任务已经 success 了，邮件发不出去是另一件事；
   两者用不同的表记录状态，页面也能分别展示（生成成功 + 推送失败）。
2. **重复推送由数据库拦**。`push_records` 上 `UNIQUE(task_id, channel)`，
   已 success 的记录默认不再发（除非显式 force）。这与「任务唯一约束」是同一套思路：
   幂等放在存储层，不靠内存变量。
3. **失败要能单独重试**。重试不重新跑 Agent（那是几毛钱 + 几分钟的事），
   只重发同一份已入库的简报。`next_retry_at` 让重试变成一次简单的 SQL 扫描，
   服务重启后待重试队列仍然在库里。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.crud import push as push_crud
from app.crud import task as task_crud
from app.models.base import local_now
from app.models.briefing import NewsBriefing
from app.models.push import PushChannel, PushRecord, PushStatus
from app.models.task import GenerationTask, TaskStatus
from app.models.user import User
from app.push.email import EmailNotConfigured, EmailSender
from app.push.template import build_email_content

logger = logging.getLogger(__name__)

__all__ = [
    "PushResult",
    "push_briefing",
    "push_for_task",
    "requeue_config_skipped",
    "retry_due_pushes",
    "retry_push_record",
]


@dataclass(slots=True)
class PushResult:
    """一次推送的结果。"""

    ok: bool
    status: str
    record: PushRecord | None = None
    reason: str | None = None

    @property
    def message(self) -> str:
        if self.ok:
            return "推送成功"
        return self.reason or "推送未完成"


# ---------------------------------------------------------------- 对外入口


def push_for_task(task_id: int, *, force: bool = False) -> PushResult:
    """为某条已完成的任务推送简报（生成流程结束后自动调用）。

    自己开一个数据库会话：生成任务使用的是另一个会话，推送不该依赖它是否还活着。

    Args:
        task_id: 任务 ID。
        force: 已成功推送过时是否强制再发一次。

    Returns:
        PushResult。
    """
    from app.core.database import SessionLocal  # 延迟导入，便于测试替换引擎

    with SessionLocal() as db:
        task = task_crud.get(db, task_id)
        if task is None:
            return PushResult(False, PushStatus.SKIPPED.value, None, f"任务不存在（id={task_id}）")
        if task.status != TaskStatus.SUCCESS.value or not task.briefing_id:
            return PushResult(
                False,
                PushStatus.SKIPPED.value,
                None,
                f"任务未成功生成简报（状态 {task.status}），没有可推送的内容",
            )

        briefing = db.get(NewsBriefing, task.briefing_id)
        user = db.get(User, task.user_id)
        if briefing is None or user is None:
            return PushResult(False, PushStatus.SKIPPED.value, None, "简报或用户记录已不存在")

        return push_briefing(db, task=task, briefing=briefing, user=user, force=force)


def push_briefing(
    db: Session,
    *,
    task: GenerationTask,
    briefing: NewsBriefing,
    user: User,
    force: bool = False,
) -> PushResult:
    """推送一份简报（会话由调用方提供，事务在本函数内提交）。

    Args:
        db: 数据库会话。
        task: 简报所属任务（推送记录挂在任务上）。
        briefing: 简报。
        user: 收件用户。
        force: 已成功推送过是否强制重发。

    Returns:
        PushResult；失败详情已写入 `push_records.last_error`。
    """
    channel = PushChannel.EMAIL.value
    record = push_crud.get_by_task(db, task.id, channel)

    if record is not None and record.status == PushStatus.SUCCESS.value and not force:
        logger.info("任务 %s 的简报已推送过，跳过重复推送", task.id)
        return PushResult(True, record.status, record, "该简报已推送过，本次跳过以避免重复打扰")

    blocked = _blocked_reason(user)
    if blocked is not None:
        status, reason = blocked
        record = _save_record(db, record, task=task, briefing=briefing, user=user, status=status, error=reason)
        logger.info("推送跳过: task_id=%s status=%s 原因=%s", task.id, status, reason)
        return PushResult(False, status, record, reason)

    record = _save_record(
        db, record, task=task, briefing=briefing, user=user, status=PushStatus.PENDING.value
    )
    return _attempt_send(db, record, briefing=briefing, user=user)


def retry_push_record(push_id: int, *, force: bool = False) -> PushResult:
    """手动重试一条推送记录（接口层调用）。

    Args:
        push_id: 推送记录 ID。
        force: 已成功时是否重发。

    Returns:
        PushResult。记录不存在时返回 ok=False。
    """
    from app.core.database import SessionLocal

    with SessionLocal() as db:
        record = push_crud.get(db, push_id)
        if record is None:
            return PushResult(False, PushStatus.SKIPPED.value, None, f"推送记录不存在（id={push_id}）")
        return _retry_in_session(db, record, force=force)


def retry_due_pushes(*, limit: int = 50) -> dict[str, int]:
    """扫描并重试到期的失败推送（定时任务调用）。

    返回统计信息，方便在日志里一眼看出「这轮重试了几条、成了几条」。
    """
    from app.core.database import SessionLocal

    stats = {"scanned": 0, "succeeded": 0, "failed": 0, "skipped": 0}
    with SessionLocal() as db:
        records = push_crud.list_retryable(
            db,
            now=local_now(),
            max_attempts=max(1, settings.PUSH_MAX_ATTEMPTS),
            limit=limit,
        )
        if not records:
            return stats

        for record in records:
            stats["scanned"] += 1
            result = _retry_in_session(db, record)
            if result.ok and result.status == PushStatus.SUCCESS.value:
                stats["succeeded"] += 1
            elif result.status == PushStatus.FAILED.value:
                stats["failed"] += 1
            else:
                stats["skipped"] += 1
    logger.info(
        "推送重试完成：扫描 %s 条，成功 %s，失败 %s，跳过 %s",
        stats["scanned"],
        stats["succeeded"],
        stats["failed"],
        stats["skipped"],
    )
    return stats


def requeue_config_skipped(*, days: int = 3, limit: int = 50) -> int:
    """把「因服务端配置缺失而跳过」的历史推送翻回待重试。

    为什么需要它（一个真实会踩的体验坑）：用户忘了填 SMTP 授权码，当天简报生成成功
    但推送记为 `skipped`；第二天补上授权码、重启服务，前一天的简报**永远不会自己发出去**
    ——`skipped` 是终态，`retry_due_pushes` 只扫 `failed`，不做这一步只能一条条手动点重试。

    只在配置已经补齐时执行：否则翻出来也还是跳过，白白刷一遍记录。
    翻回 `failed` 而不是 `pending`，是为了复用 `retry_due_pushes` 现成的扫描条件
    （`failed` + `attempts` 未超上限 + 已到期）。

    Args:
        days: 往回追溯多少天（默认 3 天，避免把很久以前的旧简报突然群发）。
        limit: 单次最多翻回多少条。

    Returns:
        被翻回的记录数。
    """
    if settings.smtp_missing_items:
        return 0

    from app.core.database import SessionLocal

    since = local_now() - timedelta(days=max(1, days))
    with SessionLocal() as db:
        records = push_crud.list_config_skipped(db, since=since, limit=limit)
        for record in records:
            record.status = PushStatus.FAILED.value
            record.next_retry_at = None  # 立刻到期，本轮重试就会把它发出去
        if records:
            db.commit()
            logger.warning(
                "推送配置已补齐：%s 条「因配置跳过」的记录重新排入重试（追溯 %s 天内）",
                len(records),
                days,
            )
        return len(records)


# ---------------------------------------------------------------- 内部实现


def _blocked_reason(user: User) -> tuple[str, str] | None:
    """判断「本来就不该发」的情况，返回 (状态, 可读原因)。"""
    if not settings.PUSH_ENABLED:
        return PushStatus.SKIPPED.value, "服务端已关闭推送（PUSH_ENABLED=false）"
    if not bool(user.push_enabled):
        return PushStatus.DISABLED.value, "用户已关闭邮件推送"
    if not (user.email or "").strip():
        return PushStatus.SKIPPED.value, "用户未填写接收邮箱"
    missing = settings.smtp_missing_items
    if missing:
        return (
            PushStatus.SKIPPED.value,
            "服务端邮件配置不完整（缺少 " + "、".join(missing) + "），简报已生成但未发送",
        )
    return None


def _save_record(
    db: Session,
    record: PushRecord | None,
    *,
    task: GenerationTask,
    briefing: NewsBriefing,
    user: User,
    status: str,
    error: str | None = None,
) -> PushRecord:
    """创建或更新推送记录（同一任务同一渠道只有一行）。"""
    if record is None:
        try:
            record = push_crud.create(
                db,
                task_id=task.id,
                user_id=user.id,
                briefing_id=briefing.id,
                channel=PushChannel.EMAIL.value,
                target=(user.email or "").strip() or None,
                status=status,
                attempts=0,
                last_error=error,
            )
            db.commit()
            return record
        except IntegrityError:
            # 并发下另一个流程刚建好，退化成「更新已有记录」
            db.rollback()
            existing = push_crud.get_by_task(db, task.id, PushChannel.EMAIL.value)
            if existing is None:  # pragma: no cover - 理论上不会发生
                raise
            record = existing

    record.briefing_id = briefing.id
    record.target = (user.email or "").strip() or record.target
    record.status = status
    if error is not None:
        record.last_error = error
    if status != PushStatus.FAILED.value:
        record.next_retry_at = None
    db.commit()
    return record


def _retry_in_session(db: Session, record: PushRecord, *, force: bool = False) -> PushResult:
    """在同一会话内执行一次重试。"""
    if record.status == PushStatus.SUCCESS.value and not force:
        return PushResult(True, record.status, record, "该简报已推送成功，无需重试")
    if record.status in (PushStatus.DISABLED.value, PushStatus.SKIPPED.value) and not force:
        return PushResult(False, record.status, record, record.last_error or "该记录不会被重试")

    task = task_crud.get(db, record.task_id)
    user = db.get(User, record.user_id)
    briefing = db.get(NewsBriefing, record.briefing_id) if record.briefing_id else None
    if task is None or user is None or briefing is None:
        record.status = PushStatus.SKIPPED.value
        record.last_error = "任务 / 用户 / 简报记录缺失，无法重试"
        record.next_retry_at = None
        db.commit()
        return PushResult(False, record.status, record, record.last_error)

    blocked = _blocked_reason(user)
    if blocked is not None:
        status, reason = blocked
        record.status = status
        record.last_error = reason
        record.next_retry_at = None
        db.commit()
        return PushResult(False, status, record, reason)

    return _attempt_send(db, record, briefing=briefing, user=user)


def _attempt_send(db: Session, record: PushRecord, *, briefing: NewsBriefing, user: User) -> PushResult:
    """真正发一次邮件，并把结果写回记录。"""
    record.attempts += 1
    record.status = PushStatus.PENDING.value
    db.commit()

    try:
        sender = EmailSender()
    except EmailNotConfigured as exc:
        record.status = PushStatus.SKIPPED.value
        record.last_error = str(exc)
        record.next_retry_at = None
        db.commit()
        logger.warning("推送配置不完整: %s", exc)
        return PushResult(False, record.status, record, str(exc))

    content = build_email_content(briefing, user, base_url=settings.PUSH_BASE_URL)
    record.subject = content.subject[:255]
    record.target = (user.email or "").strip() or record.target
    db.commit()

    outcome = sender.send(
        to=(user.email or "").strip(),
        subject=content.subject,
        text=content.text,
        html=content.html,
    )

    if outcome.ok:
        record.status = PushStatus.SUCCESS.value
        record.sent_at = local_now()
        record.message_id = (outcome.message_id or "")[:255] or None
        record.last_error = None
        record.next_retry_at = None
        db.commit()
        logger.info(
            "推送成功: task_id=%s user_id=%s attempts=%s 耗时=%sms",
            record.task_id,
            record.user_id,
            record.attempts,
            outcome.duration_ms,
        )
        return PushResult(True, record.status, record, None)

    record.status = PushStatus.FAILED.value
    record.last_error = (outcome.error or "未知错误")[:2000]
    record.next_retry_at = _next_retry_at(record.attempts)
    db.commit()
    logger.warning(
        "推送失败: task_id=%s user_id=%s attempts=%s 下次重试=%s 原因=%s",
        record.task_id,
        record.user_id,
        record.attempts,
        record.next_retry_at,
        record.last_error,
    )
    return PushResult(False, record.status, record, record.last_error)


def _next_retry_at(attempts: int):
    """指数退避计算下次重试时间；达到上限返回 None（不再自动重试）。"""
    max_attempts = max(1, settings.PUSH_MAX_ATTEMPTS)
    if attempts >= max_attempts:
        return None
    base = max(1, settings.PUSH_RETRY_BACKOFF_SECONDS)
    delay = base * (2 ** max(0, attempts - 1))
    return local_now() + timedelta(seconds=delay)
