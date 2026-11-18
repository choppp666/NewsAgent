"""Agent 执行循环的极简示意（工作区示例文件，非项目真实代码）。

这个文件被用来验证 search_content 能否按关键字定位到「Agent 相关的 Python 文件」。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Step:
    """一次工具调用。"""

    tool: str
    arguments: dict


class AgentRunner:
    """最小可运行的 Agent 循环：LLM 决策 → 执行工具 → 结果回填。"""

    def __init__(self, llm, registry, max_steps: int = 8) -> None:
        self.llm = llm
        self.registry = registry
        self.max_steps = max_steps

    def run(self, task: str) -> str:
        """返回最终回答；工具调用顺序完全由 LLM 决定。"""
        messages = [{"role": "user", "content": task}]
        for step in range(self.max_steps):
            response = self.llm.chat(messages, tools=self.registry.openai_tools())
            if not response.tool_calls:
                return response.content or ""
            for call in response.tool_calls:
                result = self.registry.execute(call.name, call.arguments)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
        return "达到最大轮数，未得出结论"
