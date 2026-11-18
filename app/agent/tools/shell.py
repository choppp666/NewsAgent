"""shell 工具：在沙箱工作区内执行命令。

这是五个工具里最危险的一个，所有防护都集中在这里：
1. 命令黑名单（safety.ensure_safe_command）：删除/格式化/注册表/服务/提权/下载执行等一律拒绝；
2. 工作目录锁定：`cwd` 强制为 workspace，并拒绝 `cd ..` 这类显式逃逸；
3. 超时：`subprocess.run(timeout=...)`，超时杀掉子进程并如实回报；
4. 输出上限：stdout/stderr 各自截断，防止把上下文窗口撑爆；
5. 环境变量清洗：敏感变量不传给子进程（见 safety.build_safe_env）。
"""

from __future__ import annotations

import subprocess
import time
from typing import Any

from pydantic import BaseModel, Field

from app.agent.context import ToolContext
from app.agent.registry import tool_registry
from app.agent.result import ToolResult
from app.agent.safety import build_safe_env, ensure_safe_command


class BashArgs(BaseModel):
    """bash 的参数。"""

    command: str = Field(
        min_length=1,
        max_length=2000,
        description="要执行的单行 shell 命令，工作目录固定为工作区根目录；不支持多行脚本",
    )
    timeout: int = Field(
        default=10,
        ge=1,
        le=60,
        description="执行超时秒数，默认 10 秒，最大 60 秒",
    )


def _decode_output(raw: bytes) -> str:
    """控制台输出解码：优先 UTF-8，Windows 中文环境兜底 GBK。"""
    if not raw:
        return ""
    for encoding in ("utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    """保留头 70% + 尾 30%：开头的命令回显与结尾的错误信息通常最有价值。"""
    if len(text) <= limit:
        return text, False
    head_len = int(limit * 0.7)
    tail_len = limit - head_len
    return f"{text[:head_len]}\n...[中间 {len(text) - limit} 字符已截断]...\n{text[-tail_len:]}", True


@tool_registry.tool(
    description="在工作区内执行单行 shell 命令并返回退出码与输出。用于统计文件数量、查看 git 状态、运行脚本等。禁止破坏性命令。"
)
def bash(args: BashArgs, ctx: ToolContext) -> ToolResult:
    """执行 shell 命令。

    Args:
        args: 命令字符串与超时秒数。
        ctx: 执行上下文（工作区、超时与输出上限）。

    Returns:
        ToolResult，data 含 exit_code / stdout / stderr / timed_out。

    Raises:
        SafetyViolation: 命中危险命令规则或命令格式非法。
    """
    ensure_safe_command(args.command)

    timeout = min(args.timeout, ctx.bash_timeout_max)
    started = time.perf_counter()

    try:
        completed = subprocess.run(  # noqa: S602 - 已通过白名单外置的黑名单 + 目录约束 + 超时加固
            args.command,
            shell=True,
            cwd=str(ctx.workspace),
            capture_output=True,
            timeout=timeout,
            env=build_safe_env(ctx.workspace),
        )
    except subprocess.TimeoutExpired as exc:
        stdout, stdout_cut = _truncate(_decode_output(exc.stdout or b""), ctx.bash_output_limit)
        stderr, stderr_cut = _truncate(_decode_output(exc.stderr or b""), ctx.bash_output_limit)
        elapsed = int((time.perf_counter() - started) * 1000)
        return ToolResult.failure(
            tool="bash",
            code="timeout",
            message=f"命令执行超过 {timeout} 秒被强制终止，请缩小范围或拆分命令",
            data={
                "command": args.command,
                "timed_out": True,
                "stdout": stdout,
                "stderr": stderr,
                "truncated": stdout_cut or stderr_cut,
            },
            duration_ms=elapsed,
        )
    except OSError as exc:
        return ToolResult.failure(
            tool="bash",
            code="tool_error",
            message=f"命令无法启动：{exc}",
            data={"command": args.command},
        )

    stdout, stdout_cut = _truncate(_decode_output(completed.stdout), ctx.bash_output_limit)
    stderr, stderr_cut = _truncate(_decode_output(completed.stderr), ctx.bash_output_limit)
    elapsed = int((time.perf_counter() - started) * 1000)

    data: dict[str, Any] = {
        "command": args.command,
        "exit_code": completed.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "timed_out": False,
        "truncated": stdout_cut or stderr_cut,
    }

    # 退出码非 0 也算「命令跑完了但没成功」，用 ok=False 让 LLM 明确知道要换策略；
    # 但仍把 stdout/stderr 完整回传，方便它根据错误信息自行修正。
    if completed.returncode != 0:
        return ToolResult.failure(
            tool="bash",
            code="command_failed",
            message=f"命令退出码 {completed.returncode}：{(stderr or stdout).strip()[:300] or '无输出'}",
            data=data,
            duration_ms=elapsed,
        )

    preview = stdout.strip().splitlines()
    head = preview[0][:120] if preview else "（无输出）"
    summary = f"命令执行成功（{len(preview)} 行输出，{elapsed} ms）：{head}"

    return ToolResult.success(tool="bash", summary=summary, data=data, duration_ms=elapsed)
