"""工具执行结果的统一数据结构。

设计目标：
1. **一个格式**：无论哪个工具、成功还是失败，都返回同一个结构，
   LLM 与服务端日志都不需要为不同工具写不同的解析分支。
2. **可序列化**：直接 json.dumps 后作为 `role="tool"` 消息内容回填给 LLM。
3. **自描述**：`summary` 给 LLM 看（一句话结论），`data` 给程序看（结构化明细），
   `error.code` 给上层代码判断（未知工具 / 参数错 / 安全拦截 / 执行异常）。

统一格式：

    {
        "ok": true,
        "tool": "read_file",
        "summary": "读取 src/agent_runner.py 共 86 行",
        "data": {...},
        "error": null,
        "meta": {"duration_ms": 3}
    }
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# 错误码常量：上层代码与前端都按这些字符串判断，避免散落的魔法字符串
ERR_UNKNOWN_TOOL = "unknown_tool"
ERR_INVALID_ARGUMENTS = "invalid_arguments"
ERR_SAFETY_VIOLATION = "safety_violation"
ERR_NOT_FOUND = "not_found"
ERR_TOOL_ERROR = "tool_error"
ERR_LIMIT_EXCEEDED = "limit_exceeded"


@dataclass(slots=True)
class ToolError:
    """工具失败时的错误描述。"""

    code: str
    message: str
    detail: Any = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload


@dataclass(slots=True)
class ToolResult:
    """所有工具的统一返回对象。"""

    ok: bool
    tool: str
    summary: str = ""
    data: Any = None
    error: ToolError | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------ 构造快捷方法

    @classmethod
    def success(
        cls,
        tool: str,
        summary: str,
        data: Any = None,
        **meta: Any,
    ) -> ToolResult:
        """构造成功结果。Args 见字段说明。"""
        return cls(ok=True, tool=tool, summary=summary, data=data, meta=dict(meta))

    @classmethod
    def failure(
        cls,
        tool: str,
        code: str,
        message: str,
        detail: Any = None,
        data: Any = None,
        **meta: Any,
    ) -> ToolResult:
        """构造失败结果。

        Args:
            tool: 工具名。
            code: 错误码，取本模块 ERR_* 常量之一。
            message: 面向 LLM / 用户的可读原因。
            detail: 附加调试信息（如校验错误的字段列表）。
            data: 失败时仍然有价值的载荷（例如命令已经产生的 stdout/stderr），
                让模型能根据输出自行调整策略。
        """
        return cls(
            ok=False,
            tool=tool,
            summary=message,
            data=data,
            error=ToolError(code=code, message=message, detail=detail),
            meta=dict(meta),
        )

    # ------------------------------------------------------------ 序列化

    def to_dict(self) -> dict[str, Any]:
        """转成可直接 JSON 化的字典。"""
        payload: dict[str, Any] = {
            "ok": self.ok,
            "tool": self.tool,
            "summary": self.summary,
            "data": self.data,
            "error": self.error.to_dict() if self.error else None,
        }
        if self.meta:
            payload["meta"] = self.meta
        return payload

    def to_llm_payload(self, max_chars: int = 12_000) -> str:
        """序列化为回填给 LLM 的字符串。

        超长时不再截断 JSON 字符串（那会让 LLM 看到半个 JSON），
        而是整体替换为一个合法的小对象，保留摘要与前缀预览，
        这样模型仍能知道「结果太长」并调整策略（例如缩小范围再查）。

        Args:
            max_chars: 允许的最大字符数。
        """
        text = json.dumps(self.to_dict(), ensure_ascii=False, default=str)
        if len(text) <= max_chars:
            return text
        return json.dumps(
            {
                "ok": self.ok,
                "tool": self.tool,
                "summary": self.summary,
                "error": self.error.to_dict() if self.error else None,
                "truncated": True,
                "note": f"结果共 {len(text)} 字符，超过 {max_chars} 字符上限，已截断。可缩小范围（更窄的目录/更小的 max_lines）后重试。",
                "preview": text[: max_chars // 2],
            },
            ensure_ascii=False,
            default=str,
        )
