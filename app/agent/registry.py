"""工具注册中心。

这是整个 Agent 的「能力清单」：工具只声明一次（名字、描述、参数模型），
Schema 生成、参数校验、异常兜底、按名路由都在这里统一完成。

为什么不用 if-else 分发：
如果主循环里写 `if name == "read_file": ... elif name == "bash": ...`，
每加一个工具就要改主循环，主循环会越来越难维护，也无法满足
「LLM 决定调用什么」这个前提。用「名称 → 规格」的字典做间接层之后，
主循环只认识 `registry.execute(name, args)`，与具体工具有多少个完全解耦。

参数校验为什么用 Pydantic：
LLM 传来的 arguments 是 JSON 字符串，字段可能缺失、类型可能是
`"3"` 而不是 `3`。Pydantic 模型既能生成 JSON Schema（给 LLM 看），
又能做类型转换与范围校验（给运行时用），一份定义两处复用。
"""

from __future__ import annotations

import inspect
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from app.agent.context import ToolContext
from app.agent.result import (
    ERR_INVALID_ARGUMENTS,
    ERR_TOOL_ERROR,
    ERR_UNKNOWN_TOOL,
    ToolResult,
)
from app.agent.safety import SafetyViolation

logger = logging.getLogger(__name__)

# 工具函数的统一签名：第一个参数是已校验的参数模型，第二个是执行上下文
ToolFunc = Callable[[Any, ToolContext], ToolResult]


@dataclass(slots=True)
class ToolSpec:
    """一个工具的完整描述。"""

    name: str
    description: str
    args_model: type[BaseModel]
    func: ToolFunc

    @property
    def parameters(self) -> dict[str, Any]:
        """参数 JSON Schema（OpenAI tools.function.parameters 直接用）。"""
        schema = self.args_model.model_json_schema()
        schema.pop("title", None)
        schema.setdefault("type", "object")
        # 我们的参数模型都是扁平结构，理论上不会出现 $defs；
        # 真出现了说明有人用了嵌套模型，这里显式报警而不是悄悄发一份 LLM 可能看不懂的 Schema。
        if "$defs" in schema:
            logger.warning("工具 %s 的参数模型包含嵌套定义，部分模型服务商可能不支持 $ref", self.name)
        return schema

    def to_openai_tool(self) -> dict[str, Any]:
        """转为 Chat Completions 的 tools 数组元素。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def to_summary(self) -> dict[str, Any]:
        """对外暴露的可读描述（GET /api/agent/tools 用）。"""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


class ToolRegistry:
    """工具注册表，进程内单例（见文件末尾 tool_registry）。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    # ------------------------------------------------------------ 注册

    def register(self, spec: ToolSpec) -> None:
        """注册一个工具；重名直接报错，避免静默覆盖导致行为诡异。"""
        if spec.name in self._tools:
            raise ValueError(f"工具名重复注册：{spec.name}")
        self._tools[spec.name] = spec
        logger.debug("注册工具 %s（参数：%s）", spec.name, list(spec.args_model.model_fields))

    def tool(
        self,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> Callable[[ToolFunc], ToolFunc]:
        """装饰器：把函数变成工具。

        用法：
            @registry.tool(description="列出目录内容")
            def list_dir(args: ListDirArgs, ctx: ToolContext) -> ToolResult: ...

        Args:
            name: 工具名，默认取函数名（Function Calling 里对 LLM 可见的名字）。
            description: 工具描述，缺省取函数 docstring 首段——LLM 选不选这个工具，
                几乎完全取决于这段文字写得清不清楚。
        """

        def decorator(func: ToolFunc) -> ToolFunc:
            hints = inspect.get_annotations(func, eval_str=True)
            params = list(hints.items())
            if not params:
                raise TypeError(f"工具函数 {func.__name__} 缺少类型注解")
            args_model = params[0][1]
            if not (isinstance(args_model, type) and issubclass(args_model, BaseModel)):
                raise TypeError(
                    f"工具函数 {func.__name__} 的第一个参数必须是 Pydantic BaseModel 子类，"
                    f"实际是 {args_model!r}"
                )

            tool_name = name or func.__name__
            doc = (description or inspect.getdoc(func) or "").strip()
            if not doc:
                raise ValueError(f"工具 {tool_name} 必须提供 description（LLM 靠它决定是否调用）")

            self.register(
                ToolSpec(
                    name=tool_name,
                    description=doc.split("\n\n")[0].strip(),
                    args_model=args_model,
                    func=func,
                )
            )
            return func

        return decorator

    # ------------------------------------------------------------ 查询

    def get(self, name: str) -> ToolSpec | None:
        """按名称取工具规格；不存在返回 None。"""
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def specs(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def openai_tools(self) -> list[dict[str, Any]]:
        """交给 LLM 的工具定义列表。"""
        return [spec.to_openai_tool() for spec in self._tools.values()]

    def describe(self) -> list[dict[str, Any]]:
        return [spec.to_summary() for spec in self._tools.values()]

    # ------------------------------------------------------------ 执行

    def execute(
        self,
        name: str,
        arguments: str | dict[str, Any] | None,
        ctx: ToolContext,
    ) -> ToolResult:
        """按名称执行工具，任何异常都转成 ToolResult，绝不向上抛。

        为什么吞异常：LLM 可能编造工具名、传错参数、让程序去读不存在的文件。
        这些都属于「Agent 运行中的正常意外」，应该把错误信息反馈给模型让它自我纠正，
        而不是让整个请求 500。

        Args:
            name: 工具名。
            arguments: LLM 给出的参数（JSON 字符串或已解析的 dict）。
            ctx: 工具执行上下文。

        Returns:
            ToolResult：成功或失败都在这里表达。
        """
        started = time.perf_counter()

        spec = self._tools.get(name)
        if spec is None:
            return ToolResult.failure(
                tool=name or "<empty>",
                code=ERR_UNKNOWN_TOOL,
                message=f"不存在名为 {name!r} 的工具，可用工具：{', '.join(self._tools)}",
                duration_ms=0,
            )

        # 1) 参数解析：模型有时返回空字符串或非法 JSON
        parsed: dict[str, Any]
        if arguments is None or (isinstance(arguments, str) and not arguments.strip()):
            parsed = {}
        elif isinstance(arguments, str):
            try:
                loaded = json.loads(arguments)
            except json.JSONDecodeError as exc:
                return ToolResult.failure(
                    tool=name,
                    code=ERR_INVALID_ARGUMENTS,
                    message=f"参数不是合法 JSON：{exc.msg}（位置 {exc.pos}）",
                    detail=arguments[:200],
                    duration_ms=_ms(started),
                )
            if not isinstance(loaded, dict):
                return ToolResult.failure(
                    tool=name,
                    code=ERR_INVALID_ARGUMENTS,
                    message=f"参数必须是 JSON 对象，实际是 {type(loaded).__name__}",
                    duration_ms=_ms(started),
                )
            parsed = loaded
        else:
            parsed = dict(arguments)

        # 2) 参数校验（含类型转换与范围检查）
        try:
            args = spec.args_model.model_validate(parsed)
        except ValidationError as exc:
            details = [
                {"field": ".".join(str(p) for p in err["loc"]) or "<root>", "reason": err["msg"]}
                for err in exc.errors()
            ]
            return ToolResult.failure(
                tool=name,
                code=ERR_INVALID_ARGUMENTS,
                message=f"参数校验失败：{_format_details(details)}",
                detail=details,
                duration_ms=_ms(started),
            )

        # 3) 真正执行（安全拦截与业务异常都在这里收敛）
        try:
            result = spec.func(args, ctx)
        except SafetyViolation as exc:
            logger.warning("工具 %s 触发安全策略：%s", name, exc.message)
            return ToolResult.failure(tool=name, code=exc.code, message=exc.message, duration_ms=_ms(started))
        except Exception as exc:  # noqa: BLE001 - 工具内部异常必须兜住
            logger.exception("工具 %s 执行异常", name)
            return ToolResult.failure(
                tool=name,
                code=ERR_TOOL_ERROR,
                message=f"工具执行异常：{type(exc).__name__}: {exc}",
                duration_ms=_ms(started),
            )

        result.meta.setdefault("duration_ms", _ms(started))
        return result


def _ms(started: float) -> int:
    """耗时毫秒数（保留整数即可，精度不是重点）。"""
    return int((time.perf_counter() - started) * 1000)


def _format_details(details: list[dict[str, str]]) -> str:
    """把校验错误拼成一句人类可读的话。"""
    return "；".join(f"{item['field']} {item['reason']}" for item in details) or "未知原因"


# 进程内单例：工具模块 import 时通过装饰器自动填充
tool_registry = ToolRegistry()
