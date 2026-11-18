"""工具执行上下文。

为什么要显式传一个 ctx 而不是用全局变量：
1. 可测试：测试里可以构造一个指向临时目录的 ctx，不会污染真实 workspace；
2. 无隐式状态：工具函数是纯函数（args + ctx → result），行为可预测；
3. 限额集中：最大读取字节、输出截断长度等参数只有一处定义。

第三阶段在原有基础上增加了三样「运行期上下文」：
- `user_id`：新闻工具据此读取该用户的订阅偏好、写入他的简报；
- `news_state`：一次运行内的候选新闻池（跨调用去重 + 链接白名单）；
- `db_session_factory`：新闻工具需要落库，但不该依赖 FastAPI 的请求级会话
  （工具在线程池里执行，请求会话不能跨线程复用）。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.agent.safety import resolve_in_workspace, to_workspace_relative
from app.core.config import settings
from app.news.state import NewsRunState

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型标注
    from sqlalchemy.orm import Session


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

    # ---------- 第三阶段：新闻业务所需的运行期上下文 ----------
    user_id: int | None = None
    news_state: NewsRunState = field(default_factory=NewsRunState)
    # 数据库会话工厂；None 时使用 app.core.database.SessionLocal
    db_session_factory: Callable[[], Session] | None = None

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

    @contextmanager
    def db_session(self) -> Iterator[Session]:
        """获取一个独立的数据库会话（用完即关，事务由调用方提交）。

        为什么不复用 `get_db()`：工具在 `anyio.to_thread` 的线程里执行，
        SQLAlchemy 的 Session 不能跨线程共享；这里每次调用新建会话，
        生命周期严格限定在一次工具调用内。
        """
        factory: Callable[[], Any] | None = self.db_session_factory
        if factory is None:
            from app.core.database import SessionLocal  # 延迟导入，避免测试期循环依赖

            factory = SessionLocal

        session = factory()
        try:
            yield session
        finally:
            session.close()
