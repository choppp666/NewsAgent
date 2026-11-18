"""LLM 客户端抽象。

为什么要抽象一层：Agent 主循环只依赖 `chat(messages, tools) -> LLMResponse` 这一件事。
换成别的厂商（通义、DeepSeek、vLLM 本地部署……）只要实现同样的接口；
测试时换成脚本化的假模型也能跑同一套循环——这正是「主循环不认识具体模型」的好处。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(slots=True)
class ToolCall:
    """模型请求的一次工具调用。"""

    id: str
    name: str
    arguments: str  # 原始 JSON 字符串，故意不预先解析：解析失败也要能回传给模型

    def to_message_dict(self) -> dict[str, Any]:
        """转成 assistant 消息里的 tool_calls 元素（回填上下文用）。"""
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


@dataclass(slots=True)
class LLMResponse:
    """一次模型响应的归一化结果。"""

    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict[str, int] | None = None
    model: str | None = None
    raw: dict[str, Any] | None = None

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


class LLMError(RuntimeError):
    """LLM 调用失败（网络 / 鉴权 / 配额 / 响应格式异常）。

    Attributes:
        code: 稳定的错误标识，接口层据此给出可读提示。
        status_code: HTTP 状态码（若来自上游响应）。
    """

    def __init__(self, message: str, *, code: str = "llm_error", status_code: int | None = None) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(message)


@runtime_checkable
class LLMClient(Protocol):
    """所有 LLM 客户端都要满足的最小契约。"""

    mode: str  # "openai" / "mock"
    model: str

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] = "auto",
    ) -> LLMResponse:
        """提交一轮对话。

        Args:
            messages: OpenAI 格式的完整消息列表。
            tools: 工具定义（Function Calling Schema）。
            tool_choice: "auto" / "none" / 指定工具。

        Returns:
            LLMResponse。

        Raises:
            LLMError: 请求失败或响应不可解析。
        """
        ...
