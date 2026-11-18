"""新闻简报模型。

表设计要点：
1. **唯一约束 (user_id, briefing_date)**：这就是「同一个用户同一天只保留一份简报」的去重手段。
   放在数据库层而不是只靠应用层判断，是因为定时任务 + 手动触发可能并发写同一天，
   应用层的「先查后写」在并发下必然穿透；数据库唯一键是最后一道闸。
   重新生成时走 UPDATE（覆盖内容并刷新 updated_at），不新增行。
2. **content 存全文**：简报的 Markdown 正文直接入库，便于接口直接返回、也便于全文检索；
   文件只是「导出物」，以库为准，避免文件被删/改后数据不一致。
3. **run_id**：把简报和那一次 Agent 运行关联起来，出问题能顺着轨迹回查是第几次调用、
   用了哪些工具、搜了什么词。
4. 不建 ORM relationship（只保留外键）：简报是「写多读少」的从属数据，
   查询时几乎不会需要从 User 反向遍历全部简报，加关系反而会在删除用户时引入级联开销；
   删除用户由外键 `ON DELETE CASCADE` 在数据库层完成。
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import BaseEntityMixin, local_now


class NewsBriefing(BaseEntityMixin, Base):
    """一份每日新闻简报。"""

    __tablename__ = "news_briefings"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属用户 ID",
    )
    briefing_date: Mapped[date] = mapped_column(Date, nullable=False, index=True, comment="简报日期（自然日）")
    title: Mapped[str] = mapped_column(String(200), nullable=False, comment="简报标题")
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="简报 Markdown 全文")
    news_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="简报收录的新闻条数")
    generated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=local_now, comment="生成时间"
    )

    # ---------- 辅助字段 ----------
    focus_topics: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, comment="生成时的关注方向快照（用户偏好可能随后变化）"
    )
    file_path: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="Markdown 导出文件在工作区内的相对路径"
    )
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="生成该简报的 Agent 运行 ID")

    __table_args__ = (
        UniqueConstraint("user_id", "briefing_date", name="uq_briefing_user_date"),
        # 覆盖「某用户按日期倒序取最近若干份」这种最高频查询
        Index("ix_briefing_user_date", "user_id", "briefing_date"),
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<NewsBriefing id={self.id} user_id={self.user_id} date={self.briefing_date}>"
