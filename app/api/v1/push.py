"""邮件推送接口（第四阶段）。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/users/{user_id}/push-settings` | 查看推送开关与「能不能真的发出去」 |
| PUT | `/users/{user_id}/push-settings` | 打开 / 关闭邮件推送 |
| GET | `/users/{user_id}/push-records` | 推送历史（含失败原因与下次重试时间） |
| POST | `/push-records/{push_id}/retry` | 手动重试某条推送（不重新生成简报） |

「关闭推送」只停发信，不停生成：简报照常入库，用户随时可以在历史里回看。
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Path, Query

from app.api.deps import CurrentUser, DbSession
from app.core.config import settings
from app.core.response import ApiResponse, success
from app.schemas.push import PushListData, PushRecordOut, PushSettingsIn, PushSettingsOut
from app.services import push_service, user_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["邮件推送（第四阶段）"])


@router.get(
    "/users/{user_id}/push-settings",
    response_model=ApiResponse[PushSettingsOut],
    summary="查看推送配置",
    description=(
        "同时返回用户意愿（push_enabled）、收件邮箱、服务端全局开关与 SMTP 配置情况。"
        "前端据此提示「为什么暂时收不到邮件」，而不是让用户以为已经开通。"
    ),
)
def get_push_settings(user: CurrentUser) -> dict:
    """读取推送配置视图。"""
    data = PushSettingsOut(
        user_id=user.id,
        push_enabled=bool(user.push_enabled),
        email=user.email,
        global_enabled=bool(settings.PUSH_ENABLED),
        smtp_configured=settings.smtp_configured,
        sender=settings.smtp_sender_address or None,
    )
    return success(data)


@router.put(
    "/users/{user_id}/push-settings",
    response_model=ApiResponse[PushSettingsOut],
    summary="开关邮件推送",
    description="关闭后定时任务仍会生成简报并入库，只是不再发送邮件。",
)
def update_push_settings(payload: PushSettingsIn, user: CurrentUser, db: DbSession) -> dict:
    """更新用户级推送开关。

    Args:
        payload: `{"push_enabled": true|false}`。
        user: 当前用户。
        db: 数据库会话。
    """
    updated = user_service.update_push_settings(db, user.id, payload.push_enabled)
    data = PushSettingsOut(
        user_id=updated.id,
        push_enabled=bool(updated.push_enabled),
        email=updated.email,
        global_enabled=bool(settings.PUSH_ENABLED),
        smtp_configured=settings.smtp_configured,
        sender=settings.smtp_sender_address or None,
    )
    message = "已开启邮件推送" if updated.push_enabled else "已关闭邮件推送，简报仍会正常生成"
    return success(data, message=message)


@router.get(
    "/users/{user_id}/push-records",
    response_model=ApiResponse[PushListData],
    summary="推送历史",
    description="按时间倒序返回推送记录；失败记录带 `last_error` 与 `next_retry_at`。",
)
def list_push_records(
    user: CurrentUser,
    db: DbSession,
    page: int = Query(default=1, ge=1, description="页码"),
    page_size: int = Query(default=20, ge=1, le=100, description="每页数量"),
) -> dict:
    """分页列出推送记录。"""
    from app.crud import push as push_crud

    records, total = push_crud.list_by_user(db, user.id, limit=page_size, offset=(page - 1) * page_size)
    data = PushListData(
        total=total,
        page=page,
        page_size=page_size,
        items=[PushRecordOut.from_model(record) for record in records],
    )
    return success(data)


@router.post(
    "/push-records/{push_id}/retry",
    response_model=ApiResponse[PushRecordOut],
    summary="重试推送",
    description=(
        "只重发已入库的简报，**不会重新跑 Agent**（省时省钱）。"
        "已经成功的记录默认不再重发，除非显式 `force=true`。"
    ),
)
async def retry_push(
    push_id: int = Path(ge=1, description="推送记录 ID"),
    force: bool = Query(default=False, description="已成功时是否仍然重发"),
) -> dict:
    """重试一条推送记录。

    Raises:
        NotFoundError: 推送记录不存在。
    """
    from app.core.exceptions import NotFoundError

    # SMTP 是阻塞 IO，放进线程池，避免一个慢连接把事件循环卡住
    result = await asyncio.to_thread(push_service.retry_push_record, push_id, force=force)
    if result.record is None:
        raise NotFoundError(result.message)

    logger.info("手动重试推送 push_id=%s → %s", push_id, result.status)
    return success(PushRecordOut.from_model(result.record), message=result.message)
