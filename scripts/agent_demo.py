"""Agent 命令行演示：跑三组不同形态的多步任务，并把工具调用轨迹打印出来。

用法：
    python scripts/agent_demo.py                 # auto：有 key 用真模型，没有则 Mock
    python scripts/agent_demo.py --mode mock     # 强制 Mock（离线验证链路）
    python scripts/agent_demo.py --mode openai   # 强制真实模型（需要 LLM_API_KEY）

三个任务刻意设计成三种不同的「合理路径」：
  1. 需要先看目录再搜索、再读取、最后落盘（长链路，含产物）；
  2. 不需要看目录，直接从关键词搜索入手，产物用追加模式（短链路，不同起点）；
  3. 纯问答，读完直接回答，不产出文件（验证「模型自己决定何时停止」）。
它们跑在同一套循环代码上，顺序差异全部来自任务语义与模型决策。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# 允许直接 `python scripts/agent_demo.py` 运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.llm import build_llm_client  # noqa: E402
from app.agent.loop import AgentRunner  # noqa: E402
from app.core.config import settings  # noqa: E402

TASKS: list[tuple[str, str]] = [
    (
        "任务一：长链路（列目录 → 搜索 → 读取 → 写入）",
        "帮我查看 workspace 目录中的文件，找到包含 Agent 关键字的 Python 文件，读取相关内容，最后整理成 summary.md",
    ),
    (
        "任务二：不同起点（直接搜索 → 读取 → 追加写入）",
        "在 workspace 里搜索包含 TODO 的文件，读一下第一个文件，然后把结论追加到 todo_report.md",
    ),
    (
        "任务三：只读即止（列目录 → 读取 → 直接回答，不写文件）",
        "workspace/notes 目录下有哪些文件？读取 agent_design.md 并告诉我它讲了什么",
    ),
]


def print_record(index: int, title: str, record) -> None:
    """打印一次运行的完整轨迹。"""
    print("=" * 78)
    print(f"{title}")
    print("-" * 78)
    print(f"状态：{record.status} | 轮数：{record.steps} | 工具调用：{record.tool_call_count} | 耗时：{record.duration_ms}ms")
    print(f"模式：{record.llm_mode} / {record.model}")

    if record.tool_calls:
        print("\n工具调用轨迹：")
        for call in record.tool_calls:
            flag = "OK " if call.ok else f"ERR({call.error_code})"
            args = ", ".join(f"{key}={value!r}" for key, value in call.arguments.items())
            if len(args) > 90:
                args = args[:90] + "…"
            print(f"  #{call.index} [{flag}] {call.tool}({args})")
            print(f"       → {call.summary[:120]}")
    else:
        print("\n（本次没有调用任何工具）")

    print("\n最终回答：")
    for line in record.answer.splitlines():
        print(f"  {line}")

    if record.error:
        print(f"\n错误：{record.error}")
    print()


async def main() -> int:
    parser = argparse.ArgumentParser(description="Agent 多步任务演示")
    parser.add_argument("--mode", choices=["auto", "openai", "mock"], default=None, help="模型模式，默认读配置")
    parser.add_argument("--only", type=int, default=None, help="只跑第 N 个任务（从 1 开始）")
    args = parser.parse_args()

    llm = build_llm_client(args.mode)
    print(f"工作区：{settings.agent_workspace_path}")
    print(f"模型模式：{llm.mode} / {llm.model}")
    print(f"轮数上限：{settings.AGENT_MAX_STEPS} | 工具次数上限：{settings.AGENT_MAX_TOOL_CALLS}")
    print()

    tasks = TASKS if args.only is None else [TASKS[args.only - 1]]
    for index, (title, task) in enumerate(tasks, start=1):
        runner = AgentRunner(llm)
        record = await runner.run(task)
        print(f"任务原文：{task}")
        print_record(index, title, record)

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
