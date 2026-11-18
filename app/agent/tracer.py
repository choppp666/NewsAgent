"""运行记录（Trace）。

验收要求「能看到完整的工具调用日志」，光靠控制台打印不够——
HTTP 调用结束后日志就散了。这里把每次运行的工具轨迹落在一个进程内环形缓冲里，
既能随 /api/agent/run 的响应直接返回，也能事后用 /api/agent/runs/{id} 回看。

为什么放内存而不是数据库：
本阶段的目标是「调试可见性」，运行记录属于临时调试数据，写库会引入
表结构、清理策略等无关成本。真正需要审计时（比如上线），再换成
`agent_runs` / `agent_run_steps` 两张表即可，接口形状不用变。
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class ToolCallRecord:
    """一次工具调用的完整记录。"""

    index: int
    tool: str
    arguments: dict[str, Any]
    ok: bool
    summary: str
    duration_ms: int
    error_code: str | None = None
    error_message: str | None = None
    preview: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "tool": self.tool,
            "arguments": self.arguments,
            "ok": self.ok,
            "summary": self.summary,
            "duration_ms": self.duration_ms,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "preview": self.preview,
        }


@dataclass(slots=True)
class RunRecord:
    """一次 Agent 运行的完整记录。"""

    run_id: str
    task: str
    status: str
    answer: str = ""
    steps: int = 0
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int = 0
    llm_mode: str = "mock"
    model: str = ""
    error: str | None = None
    usage: dict[str, int] = field(default_factory=dict)

    @property
    def tool_call_count(self) -> int:
        return len(self.tool_calls)

    def to_dict(self, *, include_trace: bool = True) -> dict[str, Any]:
        """转为可直接 JSON 化的字典。"""
        payload: dict[str, Any] = {
            "run_id": self.run_id,
            "task": self.task,
            "status": self.status,
            "answer": self.answer,
            "steps": self.steps,
            "tool_call_count": self.tool_call_count,
            "duration_ms": self.duration_ms,
            "llm_mode": self.llm_mode,
            "model": self.model,
            "error": self.error,
            "usage": self.usage,
            "started_at": self.started_at.isoformat(timespec="seconds") if self.started_at else None,
            "finished_at": self.finished_at.isoformat(timespec="seconds") if self.finished_at else None,
        }
        if include_trace:
            payload["tool_calls"] = [item.to_dict() for item in self.tool_calls]
        return payload


class RunStore:
    """线程安全的内存环形缓冲。"""

    def __init__(self, capacity: int = 50) -> None:
        self._items: deque[RunRecord] = deque(maxlen=max(1, capacity))
        self._lock = threading.Lock()

    def save(self, record: RunRecord) -> None:
        with self._lock:
            self._items.appendleft(record)

    def get(self, run_id: str) -> RunRecord | None:
        with self._lock:
            for item in self._items:
                if item.run_id == run_id:
                    return item
        return None

    def list(self, limit: int = 20) -> list[RunRecord]:
        with self._lock:
            return list(self._items)[: max(1, limit)]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


# 进程内单例；测试里可以自己 new 一个干净的
run_store = RunStore()
