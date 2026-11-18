"""数据库连接统一管理模块。

对外提供：
- engine      : 全局唯一的数据库引擎
- SessionLocal : 会话工厂
- Base         : 所有 ORM 模型的基类
- get_db()     : FastAPI 依赖，负责每个请求的会话创建与释放
- init_db()    : 建表（本阶段用 create_all；后续可切换 Alembic 迁移）
"""

from __future__ import annotations

import logging
from collections.abc import Generator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import DATA_DIR, settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 引擎创建

# SQLite 需要 check_same_thread=False：FastAPI 的同步依赖在线程池中执行，
# 连接可能被不同线程复用。连接池配合 poolclass 使用默认即可。
_connect_args: dict = {}
if settings.is_sqlite:
    _connect_args["check_same_thread"] = False
    DATA_DIR.mkdir(parents=True, exist_ok=True)

engine: Engine = create_engine(
    settings.DATABASE_URL,
    echo=settings.SQL_ECHO,          # SQL 日志由配置控制，生产环境建议关闭
    future=True,
    pool_pre_ping=True,              # 取连接前探活，避免长连接失效
    connect_args=_connect_args,
)

# ---------------------------------------------------------- SQLite 外键开关

if settings.is_sqlite:

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record) -> None:  # noqa: ANN001
        """SQLite 默认不启用外键约束，这里显式开启，保证级联删除生效。"""
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            # WAL 模式：读写并发更友好，本项目虽然并发很低但便于后续推送任务写入
            cursor.execute("PRAGMA journal_mode=WAL")
        finally:
            cursor.close()


# ---------------------------------------------------------------- 会话与基类

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,       # 由业务代码显式 flush，避免隐式写库导致难以排查的问题
    autocommit=False,
    expire_on_commit=False,  # commit 后对象仍可直接序列化（返回响应时不再触发查询）
    class_=Session,
)


class Base(DeclarativeBase):
    """ORM 基类，所有模型继承它。"""


# ---------------------------------------------------------------- 依赖与建表


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖：为每个请求提供独立会话，请求结束后自动关闭。

    异常时回滚，保证不会把半途失败的事务残留到连接池。
    """
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    """创建所有数据表（幂等，表已存在时不会重复创建）。

    必须先 import 模型模块，让它们注册到 Base.metadata 上。
    """
    from app import models  # noqa: F401  # 触发模型注册

    Base.metadata.create_all(bind=engine)
    logger.info("数据库初始化完成：%s", settings.DATABASE_URL)
