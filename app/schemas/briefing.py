"""新闻简报相关请求 / 响应模型。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.agent.schemas import AgentRunOut


class BriefingRunRequest(BaseModel):
    """POST /api/agent/briefings 的请求体。"""

    user_id: int = Field(ge=1, description="为哪个用户生成简报", examples=[1])
    briefing_date: date | None = Field(
        default=None, description="简报日期，默认今天；历史补生成时可指定过去日期"
    )
    focus: str | None = Field(
        default=None,
        max_length=100,
        description="本次额外关注点（一次性覆盖），会附加到任务描述里，不写回订阅偏好",
        examples=["MCP 生态"],
    )
    max_steps: int | None = Field(
        default=None, ge=3, le=20, description="本次运行的 LLM 轮数上限，留空用服务端默认（BRIEFING_MAX_STEPS）"
    )
    llm_mode: Literal["auto", "openai", "mock"] | None = Field(
        default=None, description="临时覆盖模型模式；mock 为离线策略（不代表模型能力）"
    )


class BriefingSummaryOut(BaseModel):
    """简报列表项（不含正文）。"""

    id: int
    user_id: int
    briefing_date: date
    title: str
    news_count: int
    generated_at: datetime
    file_path: str | None = None
    run_id: str | None = None
    focus_topics: list[str] = Field(default_factory=list)

    @classmethod
    def from_model(cls, briefing) -> BriefingSummaryOut:  # noqa: ANN001 - ORM 对象
        return cls(
            id=briefing.id,
            user_id=briefing.user_id,
            briefing_date=briefing.briefing_date,
            title=briefing.title,
            news_count=briefing.news_count,
            generated_at=briefing.generated_at,
            file_path=briefing.file_path,
            run_id=briefing.run_id,
            focus_topics=list(briefing.focus_topics or []),
        )


class BriefingOut(BriefingSummaryOut):
    """简报详情（含 Markdown 正文）。"""

    content: str

    @classmethod
    def from_model(cls, briefing) -> BriefingOut:  # noqa: ANN001 - ORM 对象
        return cls(
            id=briefing.id,
            user_id=briefing.user_id,
            briefing_date=briefing.briefing_date,
            title=briefing.title,
            news_count=briefing.news_count,
            generated_at=briefing.generated_at,
            file_path=briefing.file_path,
            run_id=briefing.run_id,
            focus_topics=list(briefing.focus_topics or []),
            content=briefing.content,
        )


class BriefingListData(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[BriefingSummaryOut]


class BriefingRunOut(BaseModel):
    """一次「生成简报」调用的完整结果。

    `run` 是 Agent 轨迹（和第二阶段完全同构），`briefing` 是落库结果。
    两者放在一起的原因：验收要看「Agent 是否真的自己走完流程」，
    只看最终文件是看不出它搜了几次、读了什么、有没有重复调用的。
    """

    run: AgentRunOut
    briefing: BriefingOut | None = Field(default=None, description="入库成功的简报；未成功时为 null")
    briefing_saved: bool = Field(default=False, description="本次运行是否成功把简报写入数据库")
    file_path: str | None = Field(default=None, description="Markdown 文件在工作区内的相对路径")
    news_candidates: int = Field(default=0, description="本轮检索到的候选新闻总数（去重后）")
    fetched_articles: int = Field(default=0, description="本轮成功抓取正文的文章数")
    searches: list[dict] = Field(default_factory=list, description="本轮执行过的搜索（关键词与返回条数）")
    warnings: list[str] = Field(default_factory=list, description="质量提示（结构问题、链接校验等）")
