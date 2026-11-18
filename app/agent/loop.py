"""Agent 主循环（手写 Function Calling / ReAct 循环）。

═══════════════════════════════════════════════════════════════════
执行过程（一轮 = 一次 LLM 请求）
═══════════════════════════════════════════════════════════════════

    用户任务
       │
       ▼
    ┌──────────────────────────────────────────────┐
    │ 1. 把 system prompt + 任务 + 工具 Schema     │
    │    一起提交给 LLM                            │
    └──────────────────────────────────────────────┘
       │
       ▼
    LLM 返回：content（推理） + tool_calls（动作）
       │
       ├── 没有 tool_calls ──► 这就是最终回答，循环结束（模型自己决定停止）
       │
       └── 有 tool_calls
              │
              ▼
    ┌──────────────────────────────────────────────┐
    │ 2. 解析 name / arguments（JSON 字符串）      │
    │ 3. registry.execute() 按名路由到具体函数     │
    │ 4. 结果序列化后作为 role="tool" 消息回填     │
    └──────────────────────────────────────────────┘
       │
       └────────► 回到第 1 步（下一轮）

关键点：循环里**没有任何 if tool == "xxx" 的分支**。
调用什么、调用几次、什么时候停，全部由模型在每一轮基于最新上下文决定；
程序只负责四件事：发请求、解析、执行、回填，外加护栏（轮数/次数/超时/重复检测）。

为什么工具调用必须串行执行：
后一个调用经常依赖前一个的结果（先搜索才知道该读哪个文件）。
并行调用能省时间，但前提是模型明确声明这些调用互不依赖，
本阶段以「顺序执行」保证正确性优先。
═══════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import Counter
from datetime import datetime
from functools import partial
from typing import Any

import anyio

from app.agent.context import ToolContext
from app.agent.llm import LLMClient, LLMError
from app.agent.llm.base import LLMResponse, ToolCall
from app.agent.prompts import build_system_prompt
from app.agent.registry import ToolRegistry, tool_registry
from app.agent.result import ToolResult
from app.agent.tracer import RunRecord, ToolCallRecord
from app.core.config import settings

logger = logging.getLogger(__name__)

# 硬上限：即使请求方传了更大的值也不放行，作为「防止无限调用」的最后一道闸
MAX_STEPS_HARD_LIMIT = 20
MAX_TOOL_CALLS_HARD_LIMIT = 50
# 同一 (工具, 参数) 连续出现这么多次，就提醒模型换策略
REPEAT_WARNING_THRESHOLD = 3


class AgentRunner:
    """把一个任务跑完：多轮 LLM 决策 + 工具执行。"""

    def __init__(
        self,
        llm: LLMClient,
        *,
        registry: ToolRegistry | None = None,
        ctx: ToolContext | None = None,
        max_steps: int | None = None,
        max_tool_calls: int | None = None,
        run_timeout: float | None = None,
        system_prompt: str | None = None,
    ) -> None:
        self.llm = llm
        self.registry = registry or tool_registry
        self.ctx = ctx or ToolContext.from_settings()
        self.max_steps = max_steps if max_steps is not None else settings.AGENT_MAX_STEPS
        self.max_tool_calls = max_tool_calls if max_tool_calls is not None else settings.AGENT_MAX_TOOL_CALLS
        self.run_timeout = run_timeout if run_timeout is not None else settings.AGENT_RUN_TIMEOUT
        # 工具 Schema 只生成一次：每次请求都一样，没必要重复构造
        self._tools_schema = self.registry.openai_tools()
        # system prompt 允许外部注入：不同业务场景（文件整理 / 新闻简报）
        # 需要不同的行为约束，但循环本身完全一致。
        self._system_prompt = system_prompt or build_system_prompt(
            workspace_name=self.ctx.workspace.name or "workspace",
            tool_names=self.registry.names(),
        )

    # ------------------------------------------------------------ 对外入口

    async def run(self, task: str, *, max_steps: int | None = None) -> RunRecord:
        """执行一次完整任务。

        Args:
            task: 用户任务原文。
            max_steps: 本次运行的轮数上限（会与硬上限取较小值）。

        Returns:
            RunRecord：包含最终回答、状态与完整工具调用轨迹。
            注意本方法**不抛业务异常**：LLM 失败、超时等都会体现在 status / error 字段里。
        """
        cleaned_task = (task or "").strip()
        run_id = uuid.uuid4().hex[:12]
        started_at = datetime.now()
        started = time.perf_counter()
        limit_steps = self._resolve_steps(max_steps)

        # 新闻类工具需要把 run_id 写进简报，便于回溯本次轨迹
        self.ctx.news_state.run_id = run_id

        record = RunRecord(
            run_id=run_id,
            task=cleaned_task,
            status="running",
            started_at=started_at,
            llm_mode=getattr(self.llm, "mode", "unknown"),
            model=getattr(self.llm, "model", ""),
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": cleaned_task},
        ]
        repeat_counter: Counter[str] = Counter()
        warned_repeat = False
        last_content = ""

        logger.info(
            "[run:%s] 开始 | 模式=%s | 模型=%s | 轮数上限=%s | 工具次数上限=%s",
            run_id,
            record.llm_mode,
            record.model,
            limit_steps,
            self.max_tool_calls,
        )

        try:
            async with asyncio.timeout(self.run_timeout):
                for step in range(1, limit_steps + 1):
                    record.steps = step
                    response = await self.llm.chat(messages, tools=self._tools_schema)
                    self._merge_usage(record, response.usage)
                    if response.content:
                        last_content = response.content

                    # ---- 停止条件：模型不再要求调用工具 ----
                    if not response.tool_calls:
                        record.status = "completed"
                        record.answer = (response.content or "").strip() or "（模型未返回任何内容）"
                        logger.info("[run:%s] 第 %s 轮无工具调用，任务结束", run_id, step)
                        break

                    messages.append(self._assistant_message(response))
                    logger.info(
                        "[run:%s] 第 %s 轮收到 %s 个工具调用请求：%s",
                        run_id,
                        step,
                        len(response.tool_calls),
                        ", ".join(call.name for call in response.tool_calls),
                    )

                    hit_call_limit = False
                    for call in response.tool_calls:
                        if len(record.tool_calls) >= self.max_tool_calls:
                            hit_call_limit = True
                            break

                        result = await self._execute(call)
                        messages.append(self._tool_message(call, result))
                        record.tool_calls.append(self._to_record(call, result, len(record.tool_calls) + 1))

                        logger.info(
                            "[run:%s] step=%s #%s %s(%s) → %s | %sms",
                            run_id,
                            step,
                            len(record.tool_calls),
                            call.name,
                            call.arguments[:160],
                            "成功" if result.ok else f"失败[{result.error.code if result.error else '-'}]",
                            result.meta.get("duration_ms", 0),
                        )

                        # 同一调用反复出现时提醒模型换策略（只提醒一次，避免刷屏）
                        if not warned_repeat and self._track_repeat(repeat_counter, call):
                            warned_repeat = True
                            messages.append(
                                {
                                    "role": "user",
                                    "content": (
                                        "【系统提醒】你已多次重复完全相同的工具调用，结果不会变化。"
                                        "请换一种策略（换路径、换关键词、缩小范围）或直接基于现有信息给出结论。"
                                    ),
                                }
                            )
                            logger.warning("[run:%s] 检测到重复工具调用，已提醒模型换策略", run_id)

                    if hit_call_limit:
                        record.status = "max_tool_calls_reached"
                        record.answer = last_content or f"已达到单次任务最多 {self.max_tool_calls} 次工具调用的上限"
                        logger.warning("[run:%s] 工具调用次数达到上限 %s", run_id, self.max_tool_calls)
                        break
                else:
                    # 循环跑满都没有 break：模型一直在调工具，说明它停不下来
                    record.status = "max_steps_reached"
                    record.answer = last_content or f"已达到最大轮数（{limit_steps} 轮）仍未得出结论"
                    logger.warning("[run:%s] 达到最大轮数 %s", run_id, limit_steps)

        except TimeoutError:
            record.status = "timeout"
            record.error = f"任务整体超时（>{self.run_timeout}s）"
            record.answer = last_content or "任务执行超时，已中止"
            logger.warning("[run:%s] 整体超时", run_id)
        except LLMError as exc:
            record.status = "llm_error"
            record.error = f"[{exc.code}] {exc}"
            record.answer = last_content or f"模型调用失败：{exc}"
            logger.error("[run:%s] LLM 调用失败：%s", run_id, exc)
        except Exception as exc:  # noqa: BLE001 - 兜底，保证接口永远有结构化返回
            record.status = "error"
            record.error = f"{type(exc).__name__}: {exc}"
            record.answer = last_content or "Agent 执行出现未预期错误"
            logger.exception("[run:%s] 未预期异常", run_id)
        finally:
            record.finished_at = datetime.now()
            record.duration_ms = int((time.perf_counter() - started) * 1000)
            logger.info(
                "[run:%s] 结束 | 状态=%s | 轮数=%s | 工具调用=%s | 耗时=%sms",
                run_id,
                record.status,
                record.steps,
                len(record.tool_calls),
                record.duration_ms,
            )

        return record

    # ------------------------------------------------------------ 内部方法

    def _resolve_steps(self, override: int | None) -> int:
        """轮数上限：请求值 → 配置值 → 硬上限，取最小。"""
        raw = override if override is not None else self.max_steps
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = settings.AGENT_MAX_STEPS
        return max(1, min(value, MAX_STEPS_HARD_LIMIT))

    async def _execute(self, call: ToolCall) -> ToolResult:
        """执行工具。

        放到线程池里跑的原因：文件读写和 subprocess 都是阻塞调用，
        直接在事件循环里执行会卡住整个服务（包括其他请求）。
        """
        return await anyio.to_thread.run_sync(
            partial(self.registry.execute, call.name, call.arguments, self.ctx)
        )

    @staticmethod
    def _assistant_message(response: LLMResponse) -> dict[str, Any]:
        """把模型响应还原成 assistant 消息，作为下一轮上下文的起点。

        必须把 tool_calls 原样放回去，否则模型会丢失「我刚请求过什么」的记忆，
        有些服务商还会直接因为 tool 消息找不到对应的 tool_call_id 而报 400。
        """
        message: dict[str, Any] = {"role": "assistant", "content": response.content or ""}
        if response.tool_calls:
            message["tool_calls"] = [call.to_message_dict() for call in response.tool_calls]
        return message

    def _tool_message(self, call: ToolCall, result: ToolResult) -> dict[str, Any]:
        """构造回填给模型的 tool 消息。"""
        return {
            "role": "tool",
            "tool_call_id": call.id,
            "name": call.name,
            "content": result.to_llm_payload(settings.AGENT_TOOL_RESULT_LIMIT),
        }

    @staticmethod
    def _to_record(call: ToolCall, result: ToolResult, index: int) -> ToolCallRecord:
        """把一次执行转成可读日志。"""
        try:
            parsed_args = json.loads(call.arguments) if call.arguments else {}
            if not isinstance(parsed_args, dict):
                parsed_args = {"_raw": call.arguments}
        except json.JSONDecodeError:
            parsed_args = {"_raw": call.arguments}

        preview = json.dumps(result.data, ensure_ascii=False, default=str)
        if len(preview) > 400:
            preview = preview[:400] + "…"

        return ToolCallRecord(
            index=index,
            tool=call.name,
            arguments=parsed_args,
            ok=result.ok,
            summary=result.summary,
            duration_ms=int(result.meta.get("duration_ms", 0)),
            error_code=result.error.code if result.error else None,
            error_message=result.error.message if result.error else None,
            preview=preview,
        )

    @staticmethod
    def _track_repeat(counter: Counter[str], call: ToolCall) -> bool:
        """统计相同调用次数，返回是否刚跨过提醒阈值。"""
        key = f"{call.name}::{call.arguments}"
        counter[key] += 1
        return counter[key] == REPEAT_WARNING_THRESHOLD

    @staticmethod
    def _merge_usage(record: RunRecord, usage: dict[str, int] | None) -> None:
        """累加多轮的 token 用量（不同服务商字段可能不全，能加就加）。"""
        if not usage:
            return
        for key, value in usage.items():
            if isinstance(value, int):
                record.usage[key] = record.usage.get(key, 0) + value
