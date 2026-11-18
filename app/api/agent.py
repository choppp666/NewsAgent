"""Agent 调试接口。

路由前缀：/api/agent（按第二阶段验收要求固定为该路径，未放进 /api/v1）

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | /api/agent/run | 提交任务，同步返回最终回答 + 完整工具调用轨迹 |
| GET | /api/agent/tools | 查看已注册工具及其 JSON Schema（验证「工具能正确注册给 LLM」）|
| GET | /api/agent/runs | 最近若干次运行摘要 |
| GET | /api/agent/runs/{run_id} | 单次运行的完整轨迹 |

为什么 run 接口是同步阻塞的：
本阶段的目标是「看得见 Agent 怎么一步步走的」，同步返回可以直接把轨迹和答案一起给出来，
不用引入 SSE / WebSocket。真正的产品形态应该改成流式（边执行边推送事件），
但那是下一阶段的事——现在优先把循环本身讲清楚。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query, status

from app.agent.loop import AgentRunner
from app.agent.llm import LLMError, build_llm_client
from app.agent.registry import tool_registry
from app.agent.schemas import (
    AgentRunListOut,
    AgentRunOut,
    AgentRunRequest,
    ToolListOut,
    ToolSpecOut,
)
from app.agent.tracer import run_store
from app.core.exceptions import AppError, NotFoundError
from app.core.response import ApiResponse, success

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent", tags=["Agent（第二阶段）"])


@router.post(
    "/run",
    response_model=ApiResponse[AgentRunOut],
    summary="执行 Agent 任务",
    description=(
        "把自然语言任务交给 Agent。工具调用顺序、次数与停止时机全部由 LLM 决定，"
        "服务端只负责执行与回填。返回结果包含最终回答与按顺序排列的工具调用记录。"
    ),
    status_code=status.HTTP_200_OK,
)
async def run_agent(payload: AgentRunRequest) -> dict:
    """执行一次 Agent 任务。

    Args:
        payload: 任务文本、可选轮数上限、可选模型模式覆盖。

    Returns:
        统一响应信封，data 为本次运行的完整记录。

    Raises:
        AppError: 模型模式非法或缺少 LLM_API_KEY 等配置问题。
    """
    try:
        llm = build_llm_client(payload.llm_mode)
    except LLMError as exc:
        # 配置类问题属于「请求方可以修正」的错误，返回 400 而不是 500
        raise AppError(str(exc), code=400, status_code=400) from exc

    runner = AgentRunner(llm)
    record = await runner.run(payload.task, max_steps=payload.max_steps)
    run_store.save(record)

    message = "任务完成" if record.status == "completed" else f"任务未正常结束：{record.status}"
    return success(record.to_dict(), message=message)


@router.get(
    "/tools",
    response_model=ApiResponse[ToolListOut],
    summary="查看已注册工具",
    description="返回工具名、描述与其参数 JSON Schema。这份内容就是提交给 LLM 的 tools 定义来源。",
)
def list_tools() -> dict:
    """列出全部已注册工具。"""
    tools = [ToolSpecOut(**item) for item in tool_registry.describe()]
    return success(ToolListOut(total=len(tools), tools=tools))


@router.get(
    "/runs",
    response_model=ApiResponse[AgentRunListOut],
    summary="最近运行记录",
    description="按时间倒序返回内存中保留的运行摘要，便于回看「刚才那次调用到底做了什么」。",
)
def list_runs(limit: int = Query(default=20, ge=1, le=50, description="返回条数")) -> dict:
    """列出最近的运行记录（不含逐条工具明细）。"""
    records = run_store.list(limit=limit)
    items = [
        {
            "run_id": record.run_id,
            "task": record.task,
            "status": record.status,
            "steps": record.steps,
            "tool_call_count": record.tool_call_count,
            "duration_ms": record.duration_ms,
            "llm_mode": record.llm_mode,
            "started_at": record.started_at.isoformat(timespec="seconds") if record.started_at else None,
        }
        for record in records
    ]
    return success({"total": len(items), "items": items})


@router.get(
    "/runs/{run_id}",
    response_model=ApiResponse[AgentRunOut],
    summary="单次运行详情",
    description="根据 run_id 取回完整工具调用轨迹（仅在进程内保留最近 AGENT_KEEP_RUNS 次）。",
)
def get_run(run_id: str) -> dict:
    """查询单次运行的完整记录。

    Raises:
        NotFoundError: run_id 不存在或已被环形缓冲淘汰。
    """
    record = run_store.get(run_id)
    if record is None:
        raise NotFoundError(f"运行记录不存在：{run_id}")
    return success(record.to_dict())
