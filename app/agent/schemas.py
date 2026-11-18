"""Agent 接口的请求 / 响应模型。

放在这里而不是 app/schemas/ 的原因：这些结构只服务于 Agent 调试接口，
与「用户 / 订阅」那套业务模型没有耦合，就近放在 agent 包内更好维护。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class AgentRunRequest(BaseModel):
    """POST /api/agent/run 的请求体。"""

    task: str = Field(
        min_length=1,
        max_length=2000,
        description="交给 Agent 的自然语言任务，例如「查看 workspace 目录，找到包含 Agent 的 Python 文件并整理成 summary.md」",
        examples=["帮我查看工作区里的文件，找到包含 Agent 关键字的 Python 文件，读取内容后整理成 summary.md"],
    )
    max_steps: int | None = Field(
        default=None,
        ge=1,
        le=20,
        description="本次运行的 LLM 轮数上限；留空使用服务端默认值（AGENT_MAX_STEPS）",
    )
    llm_mode: Literal["auto", "openai", "mock"] | None = Field(
        default=None,
        description="临时覆盖模型模式：auto（有 key 用真实模型）/ openai / mock（离线验证链路）",
    )


class ToolCallOut(BaseModel):
    """一次工具调用的执行记录。"""

    index: int = Field(description="本次运行内的序号，从 1 开始")
    tool: str = Field(description="工具名")
    arguments: dict[str, Any] = Field(default_factory=dict, description="模型给出的参数")
    ok: bool = Field(description="是否执行成功")
    summary: str = Field(default="", description="一句话结果描述")
    duration_ms: int = Field(default=0, description="耗时（毫秒）")
    error_code: str | None = Field(default=None, description="失败时的错误码")
    error_message: str | None = Field(default=None, description="失败原因")
    preview: str = Field(default="", description="返回数据的截断预览")


class AgentRunOut(BaseModel):
    """一次 Agent 运行的完整结果（含调用轨迹）。"""

    run_id: str
    task: str
    status: Literal[
        "completed",
        "max_steps_reached",
        "max_tool_calls_reached",
        "timeout",
        "llm_error",
        "error",
        "running",
    ]
    answer: str = Field(default="", description="最终回答")
    steps: int = Field(default=0, description="实际消耗的 LLM 轮数")
    tool_call_count: int = Field(default=0)
    duration_ms: int = Field(default=0)
    llm_mode: str = Field(default="mock")
    model: str = Field(default="")
    error: str | None = None
    usage: dict[str, int] = Field(default_factory=dict)
    started_at: str | None = None
    finished_at: str | None = None
    tool_calls: list[ToolCallOut] = Field(default_factory=list, description="按执行顺序排列的工具调用记录")


class ToolSpecOut(BaseModel):
    """一个已注册工具的说明（含其 JSON Schema）。"""

    name: str
    description: str
    parameters: dict[str, Any] = Field(description="符合 Function Calling 要求的 JSON Schema")


class ToolListOut(BaseModel):
    """工具清单。"""

    total: int
    tools: list[ToolSpecOut]


class AgentRunListItem(BaseModel):
    """运行记录摘要（不含逐条工具明细）。"""

    run_id: str
    task: str
    status: str
    steps: int
    tool_call_count: int
    duration_ms: int
    llm_mode: str
    started_at: str | None = None


class AgentRunListOut(BaseModel):
    total: int
    items: list[AgentRunListItem]
