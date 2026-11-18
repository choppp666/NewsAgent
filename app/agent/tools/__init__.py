"""工具包入口。

**只在这里 import 具体工具模块**——装饰器 `@tool_registry.tool(...)` 在模块被导入时
才会执行注册。新增工具的正确姿势：
1. 在本目录下新建模块（如 `web.py`）；
2. 用 `@tool_registry.tool(description=...)` 装饰函数，参数用 Pydantic 模型；
3. 在下面的 import 列表里加上该模块名。

主循环、路由、Schema 生成都不需要任何改动。

第三阶段新增 `news`：偏好读取 / 新闻搜索 / 正文抓取 / 简报入库四个业务工具。
它们与文件工具共享同一个注册表，因此同一个 Agent 可以在一次任务里
「先搜新闻、再读原文、最后写文件 + 入库」，主循环一行都不用改。
"""

from app.agent.tools import filesystem, news, shell  # noqa: F401  导入即注册

__all__ = ["filesystem", "news", "shell"]
