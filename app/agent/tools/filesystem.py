"""文件系统类工具：list_dir / read_file / search_content / write_file。

四个工具都遵守三条约定：
1. 路径参数一律相对 workspace 根目录解释，结果里的路径也统一用相对路径回报，
   这样 LLM 在下一步能直接拿它当参数用，不会因为绝对路径变形而出错；
2. 所有路径都必须经过 `ctx.resolve()`，越界直接抛 SafetyViolation；
3. 输出有上限（条目数 / 行数 / 字节数 / 命中数），并在 meta 里如实标注是否被截断。
"""

from __future__ import annotations

import fnmatch
import os
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from app.agent.context import ToolContext
from app.agent.registry import tool_registry
from app.agent.result import ERR_LIMIT_EXCEEDED, ToolResult

# 这些目录几乎不可能包含用户想看的业务内容，递归时默认跳过，
# 否则一次 list_dir 就会被 .venv / node_modules 淹没（也慢）。
IGNORED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".idea",
        ".vscode",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "dist",
        "build",
        ".next",
    }
)

# 解码候选顺序：UTF-8 优先（覆盖绝大多数现代代码文件），GBK 兜底 Windows 中文文件
_ENCODINGS: tuple[str, ...] = ("utf-8", "gbk", "utf-16", "latin-1")


# ---------------------------------------------------------------- 参数模型


class ListDirArgs(BaseModel):
    """list_dir 的参数。"""

    path: str = Field(default=".", description="要列出的目录，相对工作区根目录；'.' 表示根目录本身")
    recursive: bool = Field(default=False, description="是否递归列出子目录内容")
    max_depth: int = Field(default=2, ge=1, le=5, description="递归时的最大深度，1 表示只看直接子项")
    show_hidden: bool = Field(default=False, description="是否显示以点开头的隐藏文件")


class ReadFileArgs(BaseModel):
    """read_file 的参数。"""

    path: str = Field(description="要读取的文件路径，相对工作区根目录，例如 'src/main.py'")
    start_line: int = Field(default=1, ge=1, description="从第几行开始读（行号从 1 开始）")
    max_lines: int = Field(default=400, ge=1, le=2000, description="最多读取多少行，超出部分会被截断")


class SearchContentArgs(BaseModel):
    """search_content 的参数。"""

    keyword: str = Field(min_length=1, max_length=100, description="要搜索的关键词或子串")
    dir: str = Field(default=".", description="在哪个目录下搜索，相对工作区根目录")
    glob: str = Field(default="*", description="文件名匹配模式，例如 '*.py'、'*.md'；默认匹配所有文件")
    case_sensitive: bool = Field(default=False, description="是否区分大小写")


class WriteFileArgs(BaseModel):
    """write_file 的参数。"""

    path: str = Field(description="目标文件路径，相对工作区根目录；父目录不存在时会自动创建")
    content: str = Field(description="要写入的完整文本内容")
    mode: Literal["overwrite", "append"] = Field(
        default="overwrite", description="overwrite 覆盖写；append 追加到文件末尾"
    )


# ---------------------------------------------------------------- 公共辅助


def _decode(raw: bytes) -> tuple[str, str] | None:
    """尝试按候选编码解码。

    Returns:
        (文本, 实际使用的编码)；全部失败返回 None（视为二进制文件）。
    """
    for encoding in _ENCODINGS:
        try:
            return raw.decode(encoding), encoding
        except (UnicodeDecodeError, LookupError):
            continue
    return None


def _looks_binary(raw: bytes) -> bool:
    """含 NUL 字节基本可以断定是二进制（图片、压缩包、可执行文件等）。"""
    return b"\x00" in raw[:8192]


def _format_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")


def _human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / 1024 / 1024:.1f} MB"


# ---------------------------------------------------------------- list_dir


@tool_registry.tool(description="列出指定目录下的文件与子目录（含类型、大小、修改时间）。不确定工作区里有什么时先调用它。")
def list_dir(args: ListDirArgs, ctx: ToolContext) -> ToolResult:
    """列出目录内容。

    典型用途：Agent 拿到一个陌生任务时，第一步先「看看有什么文件」，
    再决定搜索还是直接读取——这也是它区别于固定流程脚本的地方。

    Args:
        args: 目录路径、是否递归、深度、是否显示隐藏项。
        ctx: 执行上下文（提供工作区与条目上限）。

    Returns:
        ToolResult，data.entries 为条目列表。

    Raises:
        SafetyViolation: 路径越界或不存在。
    """
    target = ctx.resolve(args.path, must_exist=True, expect="dir")

    entries: list[dict[str, object]] = []
    truncated = False

    def walk(current: Path, depth: int) -> None:
        """深度优先遍历，超过上限立即停止（避免大目录拖死请求）。"""
        nonlocal truncated
        if truncated:
            return
        try:
            scanned = sorted(os.scandir(current), key=lambda e: (not e.is_dir(), e.name.lower()))
        except OSError as exc:
            entries.append({"name": ctx.rel(current), "type": "error", "error": str(exc)})
            return

        for entry in scanned:
            if truncated:
                return
            if not args.show_hidden and entry.name.startswith("."):
                continue
            if entry.is_dir() and entry.name in IGNORED_DIRS:
                continue
            if len(entries) >= ctx.list_max_entries:
                truncated = True
                return

            item: dict[str, object] = {
                "name": entry.name,
                "path": ctx.rel(Path(entry.path)),
                "type": "dir" if entry.is_dir() else "file",
            }
            if entry.is_file():
                try:
                    stat = entry.stat()
                    item["size"] = stat.st_size
                    item["size_text"] = _human_size(stat.st_size)
                    item["modified"] = _format_time(stat.st_mtime)
                except OSError:
                    pass
            entries.append(item)

            if entry.is_dir() and args.recursive and depth < args.max_depth:
                walk(Path(entry.path), depth + 1)

    walk(target, 1)

    file_count = sum(1 for item in entries if item.get("type") == "file")
    dir_count = sum(1 for item in entries if item.get("type") == "dir")
    summary = f"目录 {ctx.rel(target)} 下共 {len(entries)} 项（{dir_count} 个目录 / {file_count} 个文件）"
    if truncated:
        summary += f"，已达 {ctx.list_max_entries} 条上限并截断"

    return ToolResult.success(
        tool="list_dir",
        summary=summary,
        data={
            "path": ctx.rel(target),
            "entries": entries,
            "total": len(entries),
            "truncated": truncated,
        },
    )


# ---------------------------------------------------------------- read_file


@tool_registry.tool(description="读取指定文件的文本内容，支持指定起始行与最大行数。适合在确认目标文件后查看其内容。")
def read_file(args: ReadFileArgs, ctx: ToolContext) -> ToolResult:
    """读取文件内容（带编码探测与大小限制）。

    Args:
        args: 文件路径、起始行、最大行数。
        ctx: 执行上下文。

    Returns:
        ToolResult，data.content 为文本内容。

    Raises:
        SafetyViolation: 路径越界、文件不存在或不是文件。
    """
    target = ctx.resolve(args.path, must_exist=True, expect="file")

    size = target.stat().st_size
    with target.open("rb") as handle:
        raw = handle.read(ctx.read_max_bytes + 1)
    byte_truncated = len(raw) > ctx.read_max_bytes
    raw = raw[: ctx.read_max_bytes]

    if _looks_binary(raw):
        return ToolResult.failure(
            tool="read_file",
            code=ERR_LIMIT_EXCEEDED,
            message=f"{ctx.rel(target)} 看起来是二进制文件（含 NUL 字节），无法按文本读取",
            data={"path": ctx.rel(target), "size": size},
        )

    decoded = _decode(raw)
    if decoded is None:
        return ToolResult.failure(
            tool="read_file",
            code=ERR_LIMIT_EXCEEDED,
            message=f"{ctx.rel(target)} 无法解码为文本（已尝试 {', '.join(_ENCODINGS)}）",
        )
    text, encoding = decoded
    lines = text.splitlines()

    start_index = args.start_line - 1
    selected = lines[start_index : start_index + args.max_lines]
    line_truncated = start_index + len(selected) < len(lines)

    if not selected and start_index >= len(lines):
        return ToolResult.success(
            tool="read_file",
            summary=f"{ctx.rel(target)} 共 {len(lines)} 行，起始行 {args.start_line} 已超出文件末尾",
            data={
                "path": ctx.rel(target),
                "content": "",
                "start_line": args.start_line,
                "returned_lines": 0,
                "total_lines": len(lines),
                "encoding": encoding,
            },
        )

    summary = f"读取 {ctx.rel(target)} 第 {args.start_line}-{args.start_line + len(selected) - 1} 行（共 {len(lines)} 行）"
    if byte_truncated or line_truncated:
        summary += "，内容已截断"

    return ToolResult.success(
        tool="read_file",
        summary=summary,
        data={
            "path": ctx.rel(target),
            "content": "\n".join(selected),
            "start_line": args.start_line,
            "returned_lines": len(selected),
            "total_lines": len(lines),
            "size": size,
            "encoding": encoding,
            "truncated": bool(byte_truncated or line_truncated),
        },
    )


# ---------------------------------------------------------------- search_content


@tool_registry.tool(description="在指定目录下搜索包含关键词的文件内容，返回命中的文件路径、行号与所在行。用于按关键字定位文件，比逐个读取更快。")
def search_content(args: SearchContentArgs, ctx: ToolContext) -> ToolResult:
    """按关键词搜索文件内容。

    这里用 Python 自己遍历而不是调 `grep`：跨平台行为一致，
    也不会因为把用户关键词拼进命令行而产生注入风险。

    Args:
        args: 关键词、目录、文件名模式、是否区分大小写。
        ctx: 执行上下文。

    Returns:
        ToolResult，data.matches 为命中列表。

    Raises:
        SafetyViolation: 目录越界或不存在。
    """
    root = ctx.resolve(args.dir, must_exist=True, expect="dir")

    needle = args.keyword if args.case_sensitive else args.keyword.lower()
    matches: list[dict[str, object]] = []
    files_scanned = 0
    skipped: list[str] = []
    truncated = False

    for current_dir, dir_names, file_names in os.walk(root):
        # 原地过滤，os.walk 就不会再进入这些目录
        dir_names[:] = [name for name in dir_names if name not in IGNORED_DIRS and not name.startswith(".")]

        for file_name in sorted(file_names):
            if truncated:
                break
            if not fnmatch.fnmatch(file_name, args.glob):
                continue

            path = Path(current_dir) / file_name
            try:
                if path.stat().st_size > ctx.read_max_bytes:
                    skipped.append(ctx.rel(path))
                    continue
                raw = path.read_bytes()
            except OSError:
                skipped.append(ctx.rel(path))
                continue

            if _looks_binary(raw):
                skipped.append(ctx.rel(path))
                continue
            decoded = _decode(raw)
            if decoded is None:
                skipped.append(ctx.rel(path))
                continue

            files_scanned += 1
            text = decoded[0]
            for line_no, line in enumerate(text.splitlines(), start=1):
                haystack = line if args.case_sensitive else line.lower()
                if needle not in haystack:
                    continue
                if len(matches) >= ctx.search_max_matches:
                    truncated = True
                    break
                stripped = line.strip()
                matches.append(
                    {
                        "path": ctx.rel(path),
                        "line": line_no,
                        "text": stripped[:300] + ("…" if len(stripped) > 300 else ""),
                    }
                )

    hit_files = sorted({str(item["path"]) for item in matches})
    summary = (
        f"在 {ctx.rel(root)} 下搜索 {args.keyword!r}（文件模式 {args.glob}）："
        f"扫描 {files_scanned} 个文件，命中 {len(matches)} 处，涉及 {len(hit_files)} 个文件"
    )
    if truncated:
        summary += f"，已达 {ctx.search_max_matches} 条上限并截断"
    if not matches:
        summary += "；没有找到匹配内容"

    return ToolResult.success(
        tool="search_content",
        summary=summary,
        data={
            "keyword": args.keyword,
            "dir": ctx.rel(root),
            "matches": matches,
            "files": hit_files,
            "files_scanned": files_scanned,
            "skipped_files": skipped[:20],
            "truncated": truncated,
        },
    )


# ---------------------------------------------------------------- write_file


@tool_registry.tool(description="将文本内容写入工作区内的文件，支持覆盖或追加。仅当任务要求产出/保存文件时调用。")
def write_file(args: WriteFileArgs, ctx: ToolContext) -> ToolResult:
    """写入文件（父目录自动创建，大小受配置上限约束）。

    Args:
        args: 目标路径、内容、写入模式。
        ctx: 执行上下文。

    Returns:
        ToolResult，data.bytes_written 为实际写入字节数。

    Raises:
        SafetyViolation: 路径越界或目标是目录。
    """
    target = ctx.resolve(args.path)
    if target.is_dir():
        return ToolResult.failure(
            tool="write_file",
            code=ERR_LIMIT_EXCEEDED,
            message=f"{ctx.rel(target)} 是一个目录，不能直接写入",
        )

    payload = args.content.encode("utf-8")
    if len(payload) > ctx.write_max_bytes:
        return ToolResult.failure(
            tool="write_file",
            code=ERR_LIMIT_EXCEEDED,
            message=f"内容 {len(payload)} 字节超过上限 {ctx.write_max_bytes} 字节，请拆分写入",
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    existed = target.exists()
    with target.open("a" if args.mode == "append" else "w", encoding="utf-8", newline="\n") as handle:
        handle.write(args.content)

    action = "追加" if args.mode == "append" else "写入"
    if existed:
        effect = "追加到已有文件末尾" if args.mode == "append" else "覆盖已有文件"
    else:
        effect = "新建文件"
    summary = f"{action} {ctx.rel(target)} 成功（{len(payload)} 字节，{effect}）"

    return ToolResult.success(
        tool="write_file",
        summary=summary,
        data={
            "path": ctx.rel(target),
            "bytes_written": len(payload),
            "mode": args.mode,
            "created": not existed,
            "total_size": target.stat().st_size,
        },
    )
