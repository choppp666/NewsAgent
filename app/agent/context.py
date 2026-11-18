"""工具执行上下文。

为什么要显式传一个 ctx 而不是用全局变量：
1. 可测试：测试里可以构造一个指向临时目录的 ctx，不会污染真实 workspace；
2. 无隐式状态：工具函数是纯函数（args + ctx → result），行为可预测；
3. 限额集中：最大读取字节、输出截断长度等参数只有一处定义。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.core.config import settings
from app.agent.safety import resolve_in_workspace, to_workspace_relative


@dataclass(slots=True)
class ToolContext:
    """一次 Agent 运行内的工具执行环境。"""

    workspace: Path
    bash_timeout: int = 10
    bash_timeout_max: int = 60
    bash_output_limit: int = 8000
    read_max_bytes: int = 200_000
    list_max_entries: int = 200
    search_max_matches: int = 50
    write_max_bytes: int = 200_000

    @classmethod
    def from_settings(cls) -> ToolContext:
        """从全局配置构造，并用 mkdir 兜底保证 workspace 存在。"""
        workspace = settings.agent_workspace_path
        workspace.mkdir(parents=True, exist_ok=True)
        return cls(
            workspace=workspace,
            bash_timeout=settings.AGENT_BASH_TIMEOUT,
            bash_timeout_max=settings.AGENT_BASH_TIMEOUT_MAX,
            bash_output_limit=settings.AGENT_BASH_OUTPUT_LIMIT,
            read_max_bytes=settings.AGENT_READ_MAX_BYTES,
            list_max_entries=settings.AGENT_LIST_MAX_ENTRIES,
            search_max_matches=settings.AGENT_SEARCH_MAX_MATCHES,
            write_max_bytes=settings.AGENT_WRITE_MAX_BYTES,
        )

    # ------------------------------------------------------------ 便捷方法

    def resolve(self, raw_path: str | None, **kwargs: object) -> Path:
        """见 safety.resolve_in_workspace，这里只是绑定 workspace 的语法糖。"""
        return resolve_in_workspace(self.workspace, raw_path, **kwargs)  # type: ignore[arg-type]

    def rel(self, path: Path) -> str:
        """绝对路径 → 相对 workspace 的展示路径。"""
        return to_workspace_relative(self.workspace, path)
