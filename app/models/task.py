"""任务执行记录模型（第四阶段）。

为什么必须有这张表，而不是「内存里记一下今天做过了」：

1. **幂等的落点必须是数据库**。定时任务、手动触发、启动补跑是三条独立的触发路径，
   它们可能在同一秒发生（重启后补跑 + 用户点「立即生成」）。内存标记在多进程/重启后
   全部失效，只有数据库唯一约束才是最后一道闸。
2. **`UNIQUE(user_id, briefing_date)`** 让「同一用户同一天只有一条任务」成为物理事实。
   于是 idempotency 不再是「先查后写」（并发下必穿透），而是「插入冲突即已存在」。
3. **状态机落在行上**：`pending → running → success / failed / skipped`。
   领取任务用「条件 UPDATE」实现乐观锁（`WHERE status IN (...) AND ...`），
   受影响行数为 1 才代表抢到了执行权，为 0 说明别人已经在跑，直接放弃即可。
   这比进程内互斥锁可靠——锁不会跨进程、也不会跨重启。
4. **heartbeat_at** 是为了区分「真的在跑」和「进程崩了留下的僵尸任务」：
   超过阈值没心跳的 running 任务允许被重新领取，否则一次崩溃就会让该用户当天永远不再生成。

`attempts` 记录被领取过几次，配合状态可以看出「重试了几轮才成功」。
"""

from __future__ import annotations

import enum
from datetime import date, datetime

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import BaseEntityMixin


class TaskStatus(str, enum.Enum):
    """单个「用户 + 日期」生成任务的状态。

    - PENDING  : 已登记，等待执行（前端显示「等待执行」）
    - RUNNING  : 正在执行 Agent（前端显示「正在生成」）
    - SUCCESS  : 生成成功且简报已入库
    - FAILED   : 执行失败（可重新执行，error_message 记录原因）
    - SKIPPED  : 主动跳过（当天已有简报 / 未配置偏好 / 用户已关闭订阅）
    """

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class TriggerType(str, enum.Enum):
    """任务由谁触发——排查问题时第一个要看的信息。"""

    MANUAL = "manual"          # 前端「立即生成简报」
    SCHEDULED = "scheduled"    # APScheduler 每日定时
    CATCH_UP = "catch_up"      # 启动补跑（服务停机错过了时间点）
    RETRY = "retry"            # 失败后重新执行


TASK_STATUS_VALUES: tuple[str, ...] = tuple(item.value for item in TaskStatus)
TRIGGER_TYPE_VALUES: tuple[str, ...] = tuple(item.value for item in TriggerType)


class GenerationTask(BaseEntityMixin, Base):
    """一条「给某用户生成某天简报」的任务记录。"""

    __tablename__ = "generation_tasks"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属用户 ID",
    )
    briefing_date: Mapped[date] = mapped_column(Date, nullable=False, index=True, comment="任务归属日期（自然日）")

    trigger_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default=TriggerType.MANUAL.value, comment="触发来源：manual/scheduled/catch_up/retry"
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=TaskStatus.PENDING.value, index=True, comment="任务状态"
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="被领取执行的次数")

    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="开始执行时间")
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="结束时间")
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="心跳时间：用于识别进程崩溃遗留的僵尸任务"
    )

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True, comment="失败 / 跳过原因")
    warnings: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, comment="质量提示（未生成简报、条数为 0 等）"
    )

    briefing_id: Mapped[int | None] = mapped_column(
        ForeignKey("news_briefings.id", ondelete="SET NULL"), nullable=True, comment="生成成功时对应的简报 ID"
    )
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="对应 Agent 运行 ID，可回查轨迹")

    __table_args__ = (
        # 幂等的物理保证：一个用户一天最多一条任务
        UniqueConstraint("user_id", "briefing_date", name="uq_task_user_date"),
        # 覆盖「某用户按日期倒序取历史任务」
        Index("ix_task_user_date", "user_id", "briefing_date"),
        # 覆盖「扫描卡住的 running 任务」与「按状态统计」
        Index("ix_task_status_heartbeat", "status", "heartbeat_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"<GenerationTask id={self.id} user_id={self.user_id} "
            f"date={self.briefing_date} status={self.status}>"
        )
