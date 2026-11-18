"""工具包入口。

**只在这里 import 具体工具模块**——装饰器 `@tool_registry.tool(...)` 在模块被导入时
才会执行注册。新增工具的正确姿势：
1. 在本目录下新建模块（如 `web.py`）；
2. 用 `@tool_registry.tool(description=...)` 装饰函数，参数用 Pydantic 模型；
3. 在下面的 import 列表里加上该模块名。

主循环、路由、Schema 生成都不需要任何改动。
"""

from app.agent.tools import filesystem, shell  # noqa: F401  导入即注册

__all__ = ["filesystem", "shell"]
