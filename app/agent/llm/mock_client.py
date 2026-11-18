"""Mock LLM 客户端（仅用于验证工具注册与调用链路）。

⚠️ 定位说明（很重要，不要当成 Agent 能力）：
真实 Function Calling 的顺序由模型基于语义决定；这里的决策是**正则抽取 + 固定优先级**，
本质是一个「假的模型」。它存在的唯一意义是：在拿不到 API Key 的环境中，
仍然可以把「Schema 生成 → 模型返回 tool_calls → 解析 → 执行 → 结果回填 → 下一轮」
这条链路完整跑通，并验证工具本身的正确性。换成真模型时，主循环一行都不用改。

它并非完全死板：每一步都读取上一轮真实的工具结果再决定下一步
（例如只有 search_content 真的命中了文件，它才会去 read_file 那些文件；
写文件的内容也来自实际读到的文本），因此顺序会随任务描述与工作区实际内容变化。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.agent.llm.base import LLMResponse, ToolCall

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 意图抽取


@dataclass(slots=True)
class Intent:
    """从自然语言任务里抽出来的「想做什么」。"""

    want_list: bool = False
    want_read: bool = False
    keyword: str | None = None
    directory: str = "."
    read_target: str | None = None
    write_target: str | None = None
    append: bool = False
    python_only: bool = False


@dataclass(slots=True)
class Observation:
    """一条已完成工具调用的观察结果。"""

    name: str
    arguments: dict[str, Any]
    ok: bool
    summary: str
    data: dict[str, Any] = field(default_factory=dict)


_LIST_HINTS = ("目录", "列出", "有哪些文件", "文件列表", "看看", "查看 workspace", "查看workspace")
_READ_HINTS = ("读取", "读一下", "内容", "看看里面", "查看内容")

_DIR_PATTERN = re.compile(r"(workspace(?:/[A-Za-z0-9_.\-\u4e00-\u9fa5]+)*)")
_KEYWORD_PATTERNS = (
    re.compile(r"包含\s*[「\"']?([^\s「」\"'，,。]+?)[」\"']?\s*(?:关键字的|关键词|关键字)"),
    re.compile(r"(?:关键词|关键字)\s*(?:为|是|:|：)?\s*[「\"']?([^\s「」\"'，,。]+)"),
    re.compile(r"[「\"']([A-Za-z_][\w\-]{1,30})[」\"']"),
    re.compile(r"\b([A-Z][A-Z0-9_]{1,})\b"),  # TODO / FIXME 这类大写常量
)
_WRITE_PATTERNS = (
    re.compile(
        r"(?:整理成|整理为|汇总成|汇总到|写成|写入|保存到|保存为|追加到|输出到|生成)\s*"
        r"[「\"']?([A-Za-z0-9_\u4e00-\u9fa5./\-]+\.(?:md|txt|json|csv|log))"
    ),
)
_READ_TARGET_PATTERN = re.compile(
    r"(?:读取|读一下|查看)\s*[「\"']?([A-Za-z0-9_\u4e00-\u9fa5./\-]+\.(?:py|md|txt|json|toml|ya?ml|cfg|ini))"
)


def parse_intent(task: str) -> Intent:
    """把任务文本解析为 Intent（仅供 Mock 使用）。

    Args:
        task: 用户任务原文。

    Returns:
        Intent 实例。
    """
    intent = Intent()

    dir_match = _DIR_PATTERN.search(task)
    if dir_match:
        raw_dir = dir_match.group(1)
        intent.directory = raw_dir[len("workspace") :].lstrip("/") or "."

    intent.want_list = any(hint in task for hint in _LIST_HINTS)
    intent.want_read = any(hint in task for hint in _READ_HINTS)
    intent.python_only = "Python" in task or "python" in task or ".py" in task

    for pattern in _KEYWORD_PATTERNS:
        found = pattern.search(task)
        if found:
            intent.keyword = found.group(1).strip()
            break

    for pattern in _WRITE_PATTERNS:
        found = pattern.search(task)
        if found:
            intent.write_target = found.group(1).strip()
            break

    read_match = _READ_TARGET_PATTERN.search(task)
    if read_match:
        intent.read_target = read_match.group(1).strip()

    intent.append = "追加" in task
    return intent


# ---------------------------------------------------------------- 客户端


class MockLLMClient:
    """基于规则的多步决策器，接口与真实客户端一致。"""

    mode = "mock"

    def __init__(self, model: str = "mock-agent", max_read_files: int = 2) -> None:
        self.model = model
        self.max_read_files = max(1, max_read_files)
        self._seq = 0

    # ------------------------------------------------------------ 接口实现

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] = "auto",
    ) -> LLMResponse:
        """按「任务意图 + 已有观察」决定下一步：返回工具调用或最终回答。"""
        task = self._extract_task(messages)
        observations = self._collect_observations(messages)

        if not tools or tool_choice == "none":
            return LLMResponse(content="（Mock 模式）未提供可用工具，无法执行任务。", finish_reason="stop", model=self.model)

        return self._decide(task, observations)

    # ------------------------------------------------------------ 上下文解析

    @staticmethod
    def _extract_task(messages: list[dict[str, Any]]) -> str:
        """取第一条 user 消息作为任务原文。"""
        for message in messages:
            if message.get("role") == "user":
                return str(message.get("content") or "")
        return ""

    @staticmethod
    def _collect_observations(messages: list[dict[str, Any]]) -> list[Observation]:
        """把历史 assistant.tool_calls 与 role=tool 消息配对成 Observation 列表。"""
        pending: dict[str, dict[str, Any]] = {}
        observations: list[Observation] = []

        for message in messages:
            role = message.get("role")
            if role == "assistant":
                for call in message.get("tool_calls") or []:
                    function = call.get("function") or {}
                    try:
                        arguments = json.loads(function.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        arguments = {}
                    pending[call.get("id", "")] = {
                        "name": function.get("name", ""),
                        "arguments": arguments if isinstance(arguments, dict) else {},
                    }
            elif role == "tool":
                call_id = message.get("tool_call_id", "")
                meta = pending.get(call_id) or {"name": message.get("name", ""), "arguments": {}}
                try:
                    payload = json.loads(message.get("content") or "{}")
                except json.JSONDecodeError:
                    payload = {}
                observations.append(
                    Observation(
                        name=meta["name"],
                        arguments=meta["arguments"],
                        ok=bool(payload.get("ok")),
                        summary=str(payload.get("summary") or ""),
                        data=payload.get("data") or {},
                    )
                )
        return observations

    # ------------------------------------------------------------ 决策

    def _decide(self, task: str, observations: list[Observation]) -> LLMResponse:
        """决策主函数：从「还没做的事」里挑一件，全都做完就给最终答案。"""
        intent = parse_intent(task)
        done = [item.name for item in observations]

        # ① 需要先看清目录结构
        if intent.want_list and "list_dir" not in done:
            return self._call("list_dir", {"path": intent.directory, "recursive": False})

        # ② 关键词搜索（用 Python 文件模式还是全量，取决于任务描述）
        if intent.keyword and "search_content" not in done:
            return self._call(
                "search_content",
                {
                    "keyword": intent.keyword,
                    "dir": intent.directory,
                    "glob": "*.py" if intent.python_only else "*",
                },
            )

        # ③ 读取：优先读任务点名的文件，否则读搜索命中的前几个
        if intent.want_read:
            pending = self._pending_reads(intent, observations)
            if pending:
                return self._call("read_file", {"path": pending, "max_lines": 200})

        # ④ 写文件：只有任务明确要求产出文件时才做
        if intent.write_target and not any(item.name == "write_file" and item.ok for item in observations):
            content = self._compose_document(task, intent, observations)
            return self._call(
                "write_file",
                {
                    "path": intent.write_target,
                    "content": content,
                    "mode": "append" if intent.append else "overwrite",
                },
            )

        # ⑤ 不再需要工具 → 停止
        return LLMResponse(content=self._final_answer(task, intent, observations), finish_reason="stop", model=self.model)

    def _pending_reads(self, intent: Intent, observations: list[Observation]) -> str | None:
        """算出「下一步该读哪个文件」，没有可读的返回 None。"""
        attempted = {
            str(item.arguments.get("path"))
            for item in observations
            if item.name == "read_file"
        }
        successes = sum(1 for item in observations if item.name == "read_file" and item.ok)

        if intent.read_target:
            candidate = intent.read_target
            if intent.directory not in (".", "") and "/" not in candidate:
                candidate = f"{intent.directory}/{candidate}"
            return None if candidate in attempted else candidate

        if successes >= self.max_read_files:
            return None

        for item in observations:
            if item.name != "search_content" or not item.ok:
                continue
            for path in item.data.get("files") or []:
                if path not in attempted:
                    return str(path)
        return None

    def _call(self, name: str, arguments: dict[str, Any]) -> LLMResponse:
        """构造一次工具调用响应。"""
        self._seq += 1
        call = ToolCall(
            id=f"mock_call_{self._seq}",
            name=name,
            arguments=json.dumps(arguments, ensure_ascii=False),
        )
        logger.debug("Mock 决策：调用 %s(%s)", name, call.arguments)
        return LLMResponse(
            content=f"（Mock）下一步调用 {name} 获取事实。",
            tool_calls=[call],
            finish_reason="tool_calls",
            model=self.model,
        )

    # ------------------------------------------------------------ 输出构造

    def _compose_document(self, task: str, intent: Intent, observations: list[Observation]) -> str:
        """把本轮实际拿到的信息整理成 Markdown 文档（内容全部来自真实工具结果）。"""
        lines: list[str] = [
            f"# {intent.write_target or '整理结果'}",
            "",
            "> 本文件由 Agent 在工作区内自动生成（当前 Mock 模式，仅用于验证工具调用链路）。",
            "",
            "## 一、任务",
            "",
            task,
            "",
            "## 二、工具调用轨迹",
            "",
        ]

        for index, item in enumerate(observations, start=1):
            status = "成功" if item.ok else "失败"
            lines.append(f"{index}. `{item.name}`（{status}）— {item.summary}")

        search_items = [item for item in observations if item.name == "search_content" and item.ok]
        for item in search_items:
            lines += ["", "## 三、搜索结果", ""]
            matches = item.data.get("matches") or []
            if not matches:
                lines.append("没有命中任何内容。")
            for match in matches[:20]:
                lines.append(f"- `{match.get('path')}:{match.get('line')}` {match.get('text')}")

        read_items = [item for item in observations if item.name == "read_file" and item.ok]
        for item in read_items:
            lines += ["", f"## 四、{item.data.get('path')} 内容摘录", "", "```"]
            lines.append(str(item.data.get("content") or "")[:2000])
            lines.append("```")

        if not read_items:
            lines += ["", "## 四、内容摘录", "", "本次未读取到文件内容。"]

        return "\n".join(lines) + "\n"

    def _final_answer(self, task: str, intent: Intent, observations: list[Observation]) -> str:
        """给出面向用户的最终回答，只陈述工具真实返回的事实。"""
        parts: list[str] = ["**（Mock 模式运行）** 已完成工具调用，结论如下：", ""]

        if not observations:
            parts.append("没有可执行的步骤：任务中未识别出目录、关键词或文件操作意图。")
            return "\n".join(parts)

        parts.append(f"共执行 {len(observations)} 次工具调用：")
        for index, item in enumerate(observations, start=1):
            parts.append(f"{index}. `{item.name}` — {item.summary}")

        search_items = [item for item in observations if item.name == "search_content" and item.ok]
        if search_items:
            matches = search_items[-1].data.get("matches") or []
            files = search_items[-1].data.get("files") or []
            parts += ["", f"命中 {len(matches)} 处，涉及文件：{', '.join(files) if files else '无'}"]

        read_items = [item for item in observations if item.name == "read_file" and item.ok]
        if read_items:
            parts += ["", "读取到的要点："]
            for item in read_items:
                snippet = str(item.data.get("content") or "").strip().replace("\n", " ")[:200]
                parts.append(f"- `{item.data.get('path')}`：{snippet}…" if len(snippet) >= 200 else f"- `{item.data.get('path')}`：{snippet}")

        if any(item.name == "write_file" and item.ok for item in observations):
            parts += ["", f"结果已写入 `{intent.write_target}`。"]

        return "\n".join(parts)
