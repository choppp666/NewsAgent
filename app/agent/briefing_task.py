"""「生成每日新闻简报」的编排入口。

为什么单独一层而不是直接写在路由里：
接口（HTTP）和脚本（离线跑批/演示）都需要同一套编排——
读偏好 → 拼任务书 → 建上下文 → 跑 Agent → 回读结果。
写在路由里的话，脚本只能复制一遍，两边迟早会走偏。

注意这里**没有步骤编排**：函数只负责准备「环境 + 任务」，
搜索几次、读哪些原文、何时收笔，全部由模型在循环里自己决定。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy.orm import Session, sessionmaker

from app.agent.context import ToolContext
from app.agent.llm import LLMError, build_llm_client, resolve_mode
from app.agent.loop import AgentRunner
from app.agent.prompts import build_briefing_prompt
from app.agent.registry import tool_registry
from app.agent.tracer import RunRecord, run_store
from app.core.config import settings
from app.core.exceptions import AppError
from app.models.briefing import NewsBriefing
from app.services import briefing_service
from app.services.briefing_service import UserProfile

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class BriefingRunResult:
    """一次生成任务的全部产出。"""

    record: RunRecord
    briefing: NewsBriefing | None = None
    file_path: str | None = None
    news_candidates: int = 0
    fetched_articles: int = 0
    searches: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def saved(self) -> bool:
        return self.briefing is not None


async def run_briefing(
    db: Session,
    profile: UserProfile,
    *,
    briefing_date: date | None = None,
    focus: str | None = None,
    llm_mode: str | None = None,
    max_steps: int | None = None,
    workspace=None,  # noqa: ANN001 - Path | None
) -> BriefingRunResult:
    """为指定用户跑一次完整的简报生成。

    Args:
        db: 请求/脚本持有的会话（用于读偏好与回读结果）。
        profile: 用户画像（由 `briefing_service.get_user_profile` 得到）。
        briefing_date: 简报日期，默认今天。
        focus: 本次额外关注点。
        llm_mode: auto / openai / mock。
        max_steps: 轮数上限，默认取 BRIEFING_MAX_STEPS。
        workspace: 覆盖工作区（测试用临时目录）。

    Returns:
        BriefingRunResult。

    Raises:
        AppError: 模型配置缺失（4xx 类错误，调用方可修正）。
    """
    day = briefing_date or date.today()

    try:
        llm = build_llm_client(llm_mode)
    except LLMError as exc:
        raise AppError(str(exc), code=400, status_code=400) from exc

    # 只有真实模型才在简报抬头标注「Agent 模式」。离线策略的抬头由 mock_news 自己写
    # （明确写「离线策略」），两边各说各的实话，不会出现「跑的是 mock、抬头却写 Agent」。
    task = briefing_service.build_briefing_task(
        profile,
        day,
        focus=focus,
        llm_label=settings.LLM_MODEL if resolve_mode(llm_mode) == "openai" else None,
    )

    if workspace is not None:
        ctx = ToolContext(workspace=workspace)
    else:
        ctx = ToolContext.from_settings()
    ctx.user_id = profile.user_id
    # 工具在线程池里执行，不能复用请求会话；绑同一个 Engine 即可共用连接池与事务边界
    ctx.db_session_factory = sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)

    prompt = build_briefing_prompt(
        workspace_name=ctx.workspace.name or "workspace",
        tool_names=tool_registry.names(),
        today=date.today().isoformat(),
        user_id=profile.user_id,
    )

    runner = AgentRunner(
        llm,
        ctx=ctx,
        max_tool_calls=settings.BRIEFING_MAX_TOOL_CALLS,
        run_timeout=settings.BRIEFING_RUN_TIMEOUT,
        system_prompt=prompt,
    )
    record = await runner.run(task, max_steps=max_steps or settings.BRIEFING_MAX_STEPS)
    run_store.save(record)

    # 工具里已 commit，这里需要先让当前会话看到别人写入的数据
    db.expire_all()
    briefing = briefing_service.get_briefing_by_date(db, profile.user_id, day)

    warnings: list[str] = []
    if record.status != "completed":
        warnings.append(f"Agent 未正常结束（{record.status}）：{record.error or record.answer[:120]}")
    if briefing is None:
        warnings.append("本次运行没有产生入库的简报（可能当天没有相关新闻，或 save_briefing 未通过校验）")
    elif briefing.news_count == 0:
        warnings.append("简报已入库但没有任何新闻条目")

    return BriefingRunResult(
        record=record,
        briefing=briefing,
        # 入库记录里的 file_path 是 save_briefing 自己算出来并实际落盘的那个，
        # 比回溯轨迹（旧版本还会 write_file）更可信，所以放在前面。
        file_path=(briefing.file_path if briefing else None) or _last_written_file(record),
        news_candidates=len(ctx.news_state.items),
        fetched_articles=len(ctx.news_state.fetches),
        searches=list(ctx.news_state.searches),
        warnings=warnings,
    )


def _last_written_file(record: RunRecord) -> str | None:
    """从轨迹里找出最后一次成功的产出文件路径。

    两个来源：`save_briefing` 的 file_path 参数（当前版本由它自己落盘），
    以及 `write_file` 的 path（旧轨迹/自定义任务仍可能单独写文件）。
    """
    for item in reversed(record.tool_calls):
        if item.tool in {"save_briefing", "write_file"} and item.ok:
            path = item.arguments.get("file_path") or item.arguments.get("path")
            if isinstance(path, str) and path:
                return path
    return None
