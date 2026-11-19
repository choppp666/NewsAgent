"""首页概览聚合服务。

首页要展示五块信息：用户基本信息、订阅偏好、最近一次简报、最近一次生成、最近一次推送。
如果让前端串行发五个请求，页面会先空一下再逐块跳出来；更重要的是
「最近一次推送状态」和「最近一次简报日期」在逻辑上必须来自同一时刻的快照，
并发请求拿到的可能是两次事务的结果，前后对不上。

所以这里做一次服务端聚合，前端一个请求拿全。
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.crud import briefing as briefing_crud
from app.crud import push as push_crud
from app.crud import task as task_crud
from app.schemas.push import PushRecordOut, PushSettingsOut
from app.schemas.subscription import SubscriptionOut
from app.schemas.task import DashboardOut, DashboardStatsOut, SchedulerInfoOut, TaskOut
from app.schemas.user import UserOut
from app.services import task_service

logger = logging.getLogger(__name__)

__all__ = ["build_dashboard"]


def build_dashboard(db: Session, user_id: int, *, include_scheduler: bool = True) -> DashboardOut:
    """组装首页概览。

    Args:
        db: 数据库会话。
        user_id: 用户 ID。
        include_scheduler: 是否附带调度器状态（纯离线场景可关掉）。

    Returns:
        DashboardOut。

    Raises:
        NotFoundError: 用户不存在。
    """
    # 延迟导入，避免 service 之间形成包级循环依赖
    from app.services import subscription_service, user_service

    user = user_service.get_user(db, user_id)
    if user is None:  # pragma: no cover - get_user 内部已抛 404
        raise NotFoundError(f"用户不存在（id={user_id}）")

    subscription = subscription_service.get_subscription(db, user_id)

    latest_briefings, total_briefings = briefing_crud.list_by_user(db, user_id, limit=1, offset=0)
    latest_briefing = latest_briefings[0] if latest_briefings else None

    latest_task_view = task_service.latest_view(db, user_id)
    running_view = task_service.running_view(db, user_id)

    latest_push = push_crud.latest_for_user(db, user_id)

    task_counts = task_crud.count_by_status(db, user_id=user_id)
    push_counts = push_crud.count_by_status(db, user_id=user_id)

    scheduler_info = None
    if include_scheduler:
        try:
            from app.scheduler.manager import scheduler_manager

            scheduler_info = SchedulerInfoOut(**scheduler_manager.info())
        except Exception:  # noqa: BLE001 - 调度器状态取不到不该让首页 500
            logger.warning("读取调度器状态失败", exc_info=True)

    return DashboardOut(
        user=UserOut.model_validate(user),
        preferences=SubscriptionOut.from_model(subscription) if subscription else None,
        latest_briefing=(
            _briefing_summary(latest_briefing) if latest_briefing is not None else None
        ),
        latest_task=(
            TaskOut.from_model(latest_task_view.task, briefing=latest_task_view.briefing, push=latest_task_view.push)
            if latest_task_view
            else None
        ),
        latest_push=PushRecordOut.from_model(latest_push) if latest_push else None,
        # 附带「能不能真的发信」：概览页要能回答「为什么我没收到邮件」
        push=PushSettingsOut.from_user(user),
        running_task=(
            TaskOut.from_model(running_view.task, briefing=running_view.briefing, push=running_view.push)
            if running_view
            else None
        ),
        stats=DashboardStatsOut(
            total_briefings=int(total_briefings),
            total_tasks=sum(task_counts.values()),
            task_status=task_counts,
            push_status=push_counts,
        ),
        scheduler=scheduler_info,
    )


def _briefing_summary(briefing):
    """延迟导入 + 转换，避免模块顶部引入过多 schema 依赖。"""
    from app.schemas.briefing import BriefingSummaryOut

    return BriefingSummaryOut.from_model(briefing)
