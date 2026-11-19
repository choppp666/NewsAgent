"""任务 / 概览相关请求与响应模型（第四阶段）。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.briefing import BriefingOut, BriefingSummaryOut
from app.schemas.push import PushRecordOut, PushSettingsOut
from app.schemas.subscription import SubscriptionOut
from app.schemas.user import UserOut


class TaskBriefingOut(BaseModel):
    """任务关联的简报摘要（生成成功时才有）。"""

    id: int
    title: str
    news_count: int
    file_path: str | None = None

    @classmethod
    def from_model(cls, briefing) -> TaskBriefingOut:  # noqa: ANN001 - ORM 对象
        return cls(
            id=briefing.id,
            title=briefing.title,
            news_count=briefing.news_count,
            file_path=briefing.file_path,
        )


class TaskOut(BaseModel):
    """一条任务记录（历史列表 / 状态轮询的返回结构）。"""

    id: int
    user_id: int
    briefing_date: date
    trigger_type: str
    status: str
    attempts: int
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_message: str | None = None
    warnings: list[str] = Field(default_factory=list)
    run_id: str | None = None
    briefing: TaskBriefingOut | None = None
    push: PushRecordOut | None = None

    @classmethod
    def from_model(cls, task, *, briefing=None, push=None) -> TaskOut:  # noqa: ANN001 - ORM 对象
        return cls(
            id=task.id,
            user_id=task.user_id,
            briefing_date=task.briefing_date,
            trigger_type=task.trigger_type,
            status=task.status,
            attempts=task.attempts,
            started_at=task.started_at,
            finished_at=task.finished_at,
            error_message=task.error_message,
            warnings=list(task.warnings or []),
            run_id=task.run_id,
            briefing=TaskBriefingOut.from_model(briefing) if briefing else None,
            push=PushRecordOut.from_model(push) if push else None,
        )


class TaskListData(BaseModel):
    """任务分页列表。"""

    total: int
    page: int
    page_size: int
    items: list[TaskOut]


class TriggerRequest(BaseModel):
    """POST /api/v1/users/{user_id}/briefings/trigger 的请求体。"""

    briefing_date: date | None = Field(
        default=None, description="要生成哪一天，默认今天；补生成历史日期时可指定过去日期"
    )
    force: bool = Field(
        default=False,
        description="当天已生成时是否强制重跑（前端「重新生成」按钮传 true；定时任务永远不强制）",
    )
    llm_mode: Literal["auto", "openai", "mock"] | None = Field(
        default=None, description="临时覆盖模型模式（mock 为离线策略，仅用于验证链路）"
    )


class TriggerOut(BaseModel):
    """触发结果。

    `action` 是给前端做状态机的依据：
    - created        : 新建了任务，已进入队列
    - resumed        : 上次失败/中断，本次重新领取
    - regenerated    : 当天已有简报，按用户要求覆盖重跑
    - already_running: 已有任务正在执行，直接返回该任务供前端继续轮询（不会重复生成）
    - already_generated: 当天已完成且未要求强制重跑（定时任务场景）
    """

    action: Literal["created", "resumed", "regenerated", "already_running", "already_generated"]
    task: TaskOut


class SchedulerInfoOut(BaseModel):
    """定时任务运行状态（首页展示「下次自动生成时间」）。"""

    enabled: bool
    running: bool
    time: str = Field(description="每日执行时间 HH:MM")
    timezone: str
    next_run_at: datetime | None = None
    jobs: list[dict] = Field(default_factory=list)
    catch_up: bool = False
    max_concurrency: int = 1


class DashboardStatsOut(BaseModel):
    """概览页的计数信息。"""

    total_briefings: int = 0
    total_tasks: int = 0
    task_status: dict[str, int] = Field(default_factory=dict)
    push_status: dict[str, int] = Field(default_factory=dict)


class DashboardOut(BaseModel):
    """首页概览：一次请求拿全，避免前端串行发 5 个请求。"""

    user: UserOut
    preferences: SubscriptionOut | None = None
    latest_briefing: BriefingSummaryOut | None = None
    latest_task: TaskOut | None = None
    latest_push: PushRecordOut | None = None
    running_task: TaskOut | None = Field(default=None, description="当前正在生成的任务（前端据此恢复轮询）")
    stats: DashboardStatsOut = Field(default_factory=DashboardStatsOut)
    scheduler: SchedulerInfoOut | None = None
    push: PushSettingsOut | None = Field(
        default=None,
        description="推送可达性（含缺失的 SMTP 配置项），前端据此解释「为什么收不到邮件」",
    )


class TaskDetailOut(BaseModel):
    """任务详情：任务本身 + 简报全文（有的话）。"""

    task: TaskOut
    briefing: BriefingOut | None = None
