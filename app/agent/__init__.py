"""Agent 包。

导入本包即完成「工具自注册」：`app.agent.tools` 里的每个 `@tool_registry.tool`
装饰器会在模块导入时把工具写入注册表。因此任何用到 Agent 的代码，
只要 `from app.agent import tool_registry`，就能拿到一份完整的工具清单。
"""

from app.agent import tools  # noqa: F401  必须先导入工具，触发注册
from app.agent.registry import ToolRegistry, ToolSpec, tool_registry

__all__ = ["ToolRegistry", "ToolSpec", "tool_registry", "tools"]
