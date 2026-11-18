"""任务与简报生成接口（第四阶段）。

路由一览（都在 `/api/v1` 下）：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/users/{user_id}/dashboard` | 首页概览（用户 + 偏好 + 最近简报/任务/推送 + 调度器状态） |
| POST | `/users/{user_id}/briefings/trigger` | 手动触发（异步，立即返回任务，前端轮询状态） |
| GET | `/tasks` | 历史任务列表（含简报与推送状态） |
| GET | `/tasks/{task_id}` | 任务详情（含简报全文） |
| POST | `/tasks/{task_id}/retry` | 重新执行失败/跳过的任务 |

为什么手动触发不是同步返回简报：
真实模型跑一次要 1~3 分钟。同步接口要么让浏览器等三分钟（容易超时、用户以为卡死），
要么就得改成 SSE/WebSocket。这里选择「先登记任务、立刻返回、前端轮询」，
和定时任务走**完全同一条执行链路**，避免出现「手动能跑、定时跑不通」这种双轨问题。
"""

from __future__ import annotations

import logging
from datetime import date

from fastapi import APIRouter, Path, Query

from app.api.deps import CurrentUser, DbSession
from app.core.exceptions import AppError, NotFoundError
from app.core.response import ApiResponse, success
from app.schemas.briefing import BriefingOut
from app.schemas.task import (
    DashboardOut,
    TaskDetailOut,
    TaskListData,
    TaskOut,
    TriggerOut,
    TriggerRequest,
)
from app.services import briefing_service, dashboard_service, task_service
from app.services.generation_worker import generation_worker

logger = logging.getLogger(__name__)

router = APIRouter(tags=["任务与简报（第四阶段）"])


# ---------------------------------------------------------------- 概览


@router.get(
    "/users/{user_id}/dashboard",
    response_model=ApiResponse[DashboardOut],
    summary="首页概览",
    description=(
        "一次返回首页所需的全部信息：用户资料、订阅偏好、最近一次简报、最近一次任务、"
        "最近一次推送状态、各类计数，以及定时任务的下次执行时间。"
        "\n\n`running_task` 不为空时说明该用户正有任务在跑，前端应直接进入轮询状态。"
    ),
)
def get_dashboard(user: CurrentUser, db: DbSession) -> dict:
    """聚合首页数据。

    Args:
        user: 当前用户（路径 user_id 经依赖校验）。
        db: 数据库会话。

    Returns:
        统一信封，data 为 DashboardOut。
    """
    data = dashboard_service.build_dashboard(db, user.id)
    return success(data)


# ---------------------------------------------------------------- 手动触发


@router.post(
    "/users/{user_id}/briefings/trigger",
    response_model=ApiResponse[TriggerOut],
    summary="立即生成简报（异步）",
    description=(
        "为当前用户登记一条生成任务并立刻返回，真正执行在后台 worker 中进行。\n\n"
        "请用 `GET /tasks/{task_id}` 轮询状态：pending（等待执行）→ running（正在生成）"
        "→ success / failed / skipped。\n\n"
        "**幂等**：同一用户同一天只有一条任务。重复点击不会产生第二条任务、也不会重复推送，"
        "返回的 `action` 会说明本次是新建（created）、复用（resumed）、覆盖重跑（regenerated）"
        "还是已有任务在跑（already_running）。"
    ),
)
async def trigger_generation(payload: TriggerRequest, user: CurrentUser, db: DbSession) -> dict:
    """登记并派发一次生成任务。

    Args:
        payload: 日期 / 是否强制重跑 / 模型模式。
        user: 当前用户。
        db: 请求级会话（只用于登记任务与校验偏好）。

    Returns:
        统一信封，data 含 action 与任务快照。

    Raises:
        AppError: 用户尚未配置订阅偏好（无法个性化，提前给出可执行的提示）。
    """
    # 1) 手动触发的即时校验：偏好为空时直接告诉用户「先去设置」，而不是排进队列再失败
    profile = briefing_service.get_user_profile(db, user.id)
    if not profile.focus:
        raise AppError(
            "当前用户还没有设置订阅偏好（关注话题 / 关键词都为空），无法生成个性化简报。"
            "请先在「订阅偏好」里添加至少一个关注领域或关键词。",
            code=400,
        )

    target_date = payload.briefing_date or date.today()

    # 2) 幂等登记：DB 唯一键 + 状态机决定本次是否真的需要执行
    result = task_service.get_or_create(
        db,
        user_id=user.id,
        briefing_date=target_date,
        trigger_type="manual",
        force=payload.force,
    )

    queued = "not_needed"
    if result.needs_run:
        queued = generation_worker.submit(result.task.id)
        logger.info(
            "手动触发生成：user_id=%s date=%s task_id=%s action=%s 入队=%s",
            user.id,
            target_date,
            result.task.id,
            result.action,
            queued,
        )

    view = task_service.get_view(db, result.task.id)
    data = TriggerOut(
        action=result.action,
        task=TaskOut.from_model(view.task, briefing=view.briefing, push=view.push),
    )

    messages = {
        "created": "任务已创建，正在排队执行",
        "resumed": "任务已重新进入队列（上次未完成）",
        "regenerated": "正在重新生成今日简报（会覆盖当天已有内容）",
        "already_running": "已有任务正在生成中，无需重复触发",
        "already_generated": "今日简报已生成，如需覆盖请使用「重新生成」",
    }
    return success(data, message=messages.get(result.action, "任务已受理"))


# ---------------------------------------------------------------- 任务查询


@router.get(
    "/tasks",
    response_model=ApiResponse[TaskListData],
    summary="历史任务列表",
    description=(
        "按任务日期倒序分页返回。每条记录同时带简报摘要与推送状态，"
        "前端的历史简报列表直接用这一个接口即可（无需再分别查简报表和推送表）。"
    ),
)
def list_tasks(
    db: DbSession,
    user_id: int = Query(ge=1, description="用户 ID"),
    page: int = Query(default=1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(default=20, ge=1, le=100, description="每页数量"),
    status: str | None = Query(
        default=None,
        description="按状态过滤：pending / running / success / failed / skipped",
    ),
) -> dict:
    """分页列出任务。

    这里用查询参数而不是路径参数，是为了和第三阶段的
    `GET /api/v1/briefings?user_id=` 保持一致（同一类「按用户筛列表」的语义）。
    `task_service.list_tasks` 内部会校验用户是否存在（不存在返回 404）。
    """
    views, total = task_service.list_tasks(db, user_id, page=page, page_size=page_size, status=status)
    data = TaskListData(
        total=total,
        page=page,
        page_size=page_size,
        items=[
            TaskOut.from_model(view.task, briefing=view.briefing, push=view.push)
            for view in views
        ],
    )
    return success(data)


@router.get(
    "/tasks/{task_id}",
    response_model=ApiResponse[TaskDetailOut],
    summary="任务详情（状态轮询用）",
    description="返回任务状态、失败原因、推送状态，以及生成成功时的简报全文（Markdown）。",
)
def get_task(
    task_id: int = Path(ge=1, description="任务 ID"),
    *,
    db: DbSession,
) -> dict:
    """取单条任务详情。

    Raises:
        NotFoundError: 任务不存在。
    """
    view = task_service.get_view(db, task_id)
    briefing = view.briefing
    data = TaskDetailOut(
        task=TaskOut.from_model(view.task, briefing=briefing, push=view.push),
        briefing=BriefingOut.from_model(briefing) if briefing else None,
    )
    return success(data)


@router.post(
    "/tasks/{task_id}/retry",
    response_model=ApiResponse[TriggerOut],
    summary="重新执行任务",
    description=(
        "把失败 / 跳过的任务重新放进队列。若任务已经成功，需要先在「重新生成」里显式确认覆盖。"
        "重复调用不会产生第二条任务。"
    ),
)
async def retry_task(task_id: int = Path(ge=1, description="任务 ID"), *, db: DbSession) -> dict:
    """重新执行一条已有任务。

    Raises:
        NotFoundError: 任务不存在。
    """
    view = task_service.get_view(db, task_id)
    task = view.task

    result = task_service.get_or_create(
        db,
        user_id=task.user_id,
        briefing_date=task.briefing_date,
        trigger_type="retry",
        force=False,
    )
    if result.needs_run:
        generation_worker.submit(result.task.id)

    fresh = task_service.get_view(db, result.task.id)
    data = TriggerOut(
        action=result.action,
        task=TaskOut.from_model(fresh.task, briefing=fresh.briefing, push=fresh.push),
    )
    if result.action == "already_generated":
        return success(data, message="该任务已成功生成简报，未重复执行")
    if result.action == "already_running":
        return success(data, message="任务正在执行中")
    return success(data, message="任务已重新进入队列")


# ---------------------------------------------------------------- 简报详情（补充）


@router.get(
    "/users/{user_id}/briefings/{briefing_id}",
    response_model=ApiResponse[BriefingOut],
    summary="按用户取简报详情（带归属校验）",
    description="与 `/briefings/{id}` 等价，额外校验该简报确实属于该用户，避免前端拼错 ID 看到别人的简报。",
)
def get_user_briefing(
    user: CurrentUser,
    briefing_id: int = Path(ge=1, description="简报 ID"),
    *,
    db: DbSession,
) -> dict:
    """取属于该用户的简报详情。

    Raises:
        NotFoundError: 简报不存在或不属于该用户。
    """
    briefing = briefing_service.get_briefing(db, briefing_id)
    if briefing.user_id != user.id:
        raise NotFoundError(f"简报不存在（id={briefing_id}）")
    return success(BriefingOut.from_model(briefing))
