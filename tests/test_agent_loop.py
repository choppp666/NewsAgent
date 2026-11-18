"""Agent 主循环测试（验收项 3-7：主动调用、解析执行、结果回填、多轮、自主停止）。

用「脚本化假模型」而不是 Mock 客户端：Mock 有自己的决策逻辑，
而这里要验证的是**主循环**本身——所以直接规定模型每一步返回什么，
再看循环是否按协议正确执行、回填、以及是否会在该停的时候停。

最关键的一组用例是 `test_loop_follows_model_order_*`：
同一套循环代码，喂三种完全不同的调用顺序，都能正确跑完，
证明「调用顺序由模型决定，而不是由代码写死的流程决定」。
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.context import ToolContext  # noqa: E402
from app.agent.llm.base import LLMError, LLMResponse, ToolCall  # noqa: E402
from app.agent.loop import AgentRunner  # noqa: E402


# ---------------------------------------------------------------- 假模型


@dataclass
class ScriptedLLM:
    """按预设脚本逐轮返回的假模型。

    脚本元素可以是：
    - {"tool": "read_file", "args": {...}}      返回一次工具调用
    - {"calls": [{"tool": ..., "args": ...}]}   一次返回多个工具调用
    - {"content": "..."}                        返回最终回答（不调用工具）
    - {"raise": "..."}                          抛出 LLMError
    """

    steps: list[dict[str, Any]]
    mode: str = "test"
    model: str = "scripted"
    history: list[list[dict[str, Any]]] = field(default_factory=list)
    index: int = 0

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] = "auto",
    ) -> LLMResponse:
        # 深拷贝一份快照，便于事后断言「模型当时看到了什么上下文」
        self.history.append(json.loads(json.dumps(messages, ensure_ascii=False, default=str)))
        if self.index >= len(self.steps):
            return LLMResponse(content="（脚本已放完）", finish_reason="stop", model=self.model)

        step = self.steps[self.index]
        self.index += 1

        if "raise" in step:
            raise LLMError(str(step["raise"]), code="llm_test_error")

        calls = step.get("calls") or ([{"tool": step["tool"], "args": step["args"]}] if "tool" in step else [])
        if calls:
            return LLMResponse(
                content=step.get("content"),
                tool_calls=[
                    ToolCall(
                        id=f"call_{self.index}_{i}",
                        name=item["tool"],
                        arguments=json.dumps(item.get("args", {}), ensure_ascii=False),
                    )
                    for i, item in enumerate(calls)
                ],
                finish_reason="tool_calls",
                model=self.model,
            )

        return LLMResponse(content=step.get("content", ""), finish_reason="stop", model=self.model)


@pytest.fixture()
def workspace(tmp_path: Path) -> Generator[Path, None, None]:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "agent_runner.py").write_text("class AgentRunner:\n    pass\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("Agent 设计笔记\n", encoding="utf-8")
    yield tmp_path


@pytest.fixture()
def ctx(workspace: Path) -> ToolContext:
    return ToolContext(workspace=workspace)


def run(llm: ScriptedLLM, task: str, ctx: ToolContext, **kwargs: Any):
    """同步跑一个异步任务，避免为测试额外引入 pytest-asyncio 依赖。"""
    runner = AgentRunner(llm, ctx=ctx, **kwargs)
    return asyncio.run(runner.run(task))


# ---------------------------------------------------------------- 顺序自由


@pytest.mark.parametrize(
    ("order", "expected"),
    [
        # 顺序一：先看目录 → 再搜索 → 再读 → 最后写文件
        (
            [
                {"tool": "list_dir", "args": {"path": "."}},
                {"tool": "search_content", "args": {"keyword": "Agent"}},
                {"tool": "read_file", "args": {"path": "src/agent_runner.py"}},
                {"tool": "write_file", "args": {"path": "summary.md", "content": "# 汇总\n"}},
                {"content": "已完成整理，结果写入 summary.md"},
            ],
            ["list_dir", "search_content", "read_file", "write_file"],
        ),
        # 顺序二：跳过目录，直接搜索 → 读取 → 回答（不写文件）
        (
            [
                {"tool": "search_content", "args": {"keyword": "Agent"}},
                {"tool": "read_file", "args": {"path": "notes.md"}},
                {"content": "搜索到 1 个文件，内容为 Agent 设计笔记"},
            ],
            ["search_content", "read_file"],
        ),
        # 顺序三：直接读 → 跑命令 → 回答（完全不同的起点）
        (
            [
                {"tool": "read_file", "args": {"path": "notes.md"}},
                {"tool": "bash", "args": {"command": "echo checked"}},
                {"content": "已读取笔记并执行校验命令"},
            ],
            ["read_file", "bash"],
        ),
    ],
)
def test_loop_follows_model_order(order, expected, ctx: ToolContext) -> None:
    """三种不同顺序都能跑通：说明循环不预设流程，只是忠实地执行模型的每一次决策。"""
    llm = ScriptedLLM(steps=order)

    record = run(llm, "随便一个任务", ctx)

    assert record.status == "completed"
    assert [item.tool for item in record.tool_calls] == expected
    assert record.steps == len(expected) + 1  # 每个工具一轮 + 最后一轮给回答
    assert record.answer.strip()


def test_loop_stops_when_model_returns_no_tool_calls(ctx: ToolContext) -> None:
    """停止时机由模型决定：第一轮就直接回答，程序不该擅自动手。"""
    llm = ScriptedLLM(steps=[{"content": "这个问题不需要工具"}])

    record = run(llm, "1+1 等于几", ctx)

    assert record.status == "completed"
    assert record.steps == 1
    assert record.tool_calls == []
    assert record.answer == "这个问题不需要工具"


def test_tool_result_is_fed_back_to_model(ctx: ToolContext) -> None:
    """工具执行结果必须作为 role=tool 消息回填，模型才可能基于事实继续推理。"""
    llm = ScriptedLLM(
        steps=[
            {"tool": "read_file", "args": {"path": "notes.md"}},
            {"content": "读到了"},
        ]
    )

    run(llm, "读取 notes.md", ctx)

    # 第二次调用模型时，上下文里应该有 assistant(tool_calls) + tool(结果)
    second_round = llm.history[1]
    assistant_msg = next(msg for msg in second_round if msg["role"] == "assistant")
    tool_msg = next(msg for msg in second_round if msg["role"] == "tool")

    assert assistant_msg["tool_calls"][0]["function"]["name"] == "read_file"
    assert tool_msg["tool_call_id"] == assistant_msg["tool_calls"][0]["id"]
    payload = json.loads(tool_msg["content"])
    assert payload["ok"] is True
    assert "Agent 设计笔记" in payload["data"]["content"]


# ---------------------------------------------------------------- 异常处理


def test_unknown_tool_does_not_crash_and_feeds_error_back(ctx: ToolContext) -> None:
    """模型编造工具名时：记录失败 + 把错误回传，让它自己纠偏。"""
    llm = ScriptedLLM(
        steps=[
            {"tool": "delete_everything", "args": {"path": "/"}},
            {"tool": "list_dir", "args": {"path": "."}},
            {"content": "已改用合法工具"},
        ]
    )

    record = run(llm, "清理磁盘", ctx)

    assert record.status == "completed"
    assert record.tool_calls[0].ok is False
    assert record.tool_calls[0].error_code == "unknown_tool"
    # 模型第二轮看到的上下文里必须包含这条错误
    second_round = llm.history[1]
    tool_payload = json.loads(next(msg for msg in second_round if msg["role"] == "tool")["content"])
    assert tool_payload["error"]["code"] == "unknown_tool"
    assert "list_dir" in tool_payload["error"]["message"]


def test_invalid_arguments_reported_to_model(ctx: ToolContext) -> None:
    """参数格式错误同样不该 500，而是让模型看到「哪个字段错了」。"""
    llm = ScriptedLLM(
        steps=[
            {"tool": "read_file", "args": {"path": "notes.md", "start_line": "第一行"}},
            {"content": "已修正参数"},
        ]
    )

    record = run(llm, "读文件", ctx)

    assert record.tool_calls[0].ok is False
    assert record.tool_calls[0].error_code == "invalid_arguments"
    assert "start_line" in record.tool_calls[0].error_message


def test_safety_violation_is_recorded(ctx: ToolContext) -> None:
    """越界/危险操作会被工具层拦下，并以失败记录出现在轨迹里。"""
    llm = ScriptedLLM(
        steps=[
            {"tool": "bash", "args": {"command": "rm -rf /"}},
            {"content": "放弃危险操作"},
        ]
    )

    record = run(llm, "清理", ctx)

    assert record.tool_calls[0].ok is False
    assert record.tool_calls[0].error_code == "safety_violation"


def test_llm_error_is_reported(ctx: ToolContext) -> None:
    llm = ScriptedLLM(steps=[{"raise": "上游 500"}])

    record = run(llm, "任务", ctx)

    assert record.status == "llm_error"
    assert record.error is not None and "上游 500" in record.error


# ---------------------------------------------------------------- 护栏


def test_max_steps_guard(ctx: ToolContext) -> None:
    """模型停不下来时，靠轮数上限强制收口。"""
    llm = ScriptedLLM(steps=[{"tool": "list_dir", "args": {"path": "."}} for _ in range(10)])

    record = run(llm, "无限循环任务", ctx, max_steps=2)

    assert record.status == "max_steps_reached"
    assert record.steps == 2
    assert len(record.tool_calls) == 2


def test_max_tool_calls_guard(ctx: ToolContext) -> None:
    """一轮里塞很多调用时，靠工具次数上限兜底。"""
    many_calls = [{"tool": "list_dir", "args": {"path": "."}} for _ in range(5)]
    llm = ScriptedLLM(steps=[{"calls": many_calls}])

    record = run(llm, "批量调用", ctx, max_tool_calls=2)

    assert record.status == "max_tool_calls_reached"
    assert len(record.tool_calls) == 2


def test_repeat_call_warning_injected(ctx: ToolContext) -> None:
    """同一调用重复 3 次后，循环会插入提醒，促使模型换策略。"""
    step = {"tool": "list_dir", "args": {"path": "."}}
    llm = ScriptedLLM(steps=[step, step, step, {"content": "好了"}])

    record = run(llm, "重复调用", ctx)

    assert record.status == "completed"
    all_contents = [msg.get("content") or "" for msg in llm.history[-1]]
    assert any("系统提醒" in text for text in all_contents)


def test_run_record_serializable(ctx: ToolContext) -> None:
    """运行记录必须能直接 JSON 化（接口直接返回它）。"""
    llm = ScriptedLLM(steps=[{"tool": "list_dir", "args": {"path": "."}}, {"content": "ok"}])

    record = run(llm, "看看目录", ctx)
    payload = record.to_dict()

    assert payload["status"] == "completed"
    assert payload["tool_calls"][0]["tool"] == "list_dir"
    json.dumps(payload, ensure_ascii=False)  # 不抛异常即为可序列化
