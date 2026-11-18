"""极简 schema 迁移。

问题：`Base.metadata.create_all()` **只建表、不改表**。
第四阶段给 `users` 增加了 `push_enabled` 字段，已有开发库里这张表早就存在，
`create_all` 会直接跳过，于是运行时报 `no such column: users.push_enabled`。

为什么不直接上 Alembic：
本项目的迁移需求只有「加列」一种，且只用 SQLite；引入 Alembic 会带来
`alembic.ini`、`env.py`、versions 目录和一套 autogenerate 流程，
对一个教学项目来说是过度设计。这里用一个显式的「补列清单」解决问题：

- **只做加法**（ADD COLUMN），不做改名/删列/改类型——这些操作 SQLite 也不支持；
- **幂等**：先 PRAGMA 查现有列，已存在就跳过，可反复执行；
- **显式**：需要补哪些列写在 `COLUMN_PATCHES` 里，而不是靠反射猜，
  避免「猜错默认值把数据写坏」。

正式产品演进仍应切 Alembic，README 的「已知限制」里写明了这一点。
"""

from __future__ import annotations

import logging

from sqlalchemy import Engine, inspect, text

logger = logging.getLogger(__name__)

# (表名, 列名, 列定义) —— 列定义必须是 SQLite ADD COLUMN 能接受的写法：
# 有 NOT NULL 就必须带 DEFAULT，否则已有行无法补值。
COLUMN_PATCHES: tuple[tuple[str, str, str], ...] = (
    ("users", "push_enabled", "BOOLEAN NOT NULL DEFAULT 1"),
)


def sync_schema(engine: Engine) -> list[str]:
    """为已存在的表补上模型里新增的列。

    Args:
        engine: 数据库引擎。

    Returns:
        本次实际执行的补丁列表（形如 ``["users.push_enabled"]``）；
        全部已存在时返回空列表。

    Raises:
        无。单条补丁失败只记录错误日志并继续，避免因为一个历史遗留问题
        导致整个服务无法启动——真出错时业务日志会明确指向缺失的列。
    """
    if engine.dialect.name != "sqlite":
        # 其他数据库请交给 Alembic：本模块的 DDL 语法是按 SQLite 写的
        logger.info("数据库方言为 %s，跳过内置补列（请使用正式迁移工具）", engine.dialect.name)
        return []

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    applied: list[str] = []

    for table, column, ddl in COLUMN_PATCHES:
        if table not in tables:
            # 表还没建（全新库）——create_all 已经带着新列建好了，无需补
            continue
        existing = {item["name"] for item in inspector.get_columns(table)}
        if column in existing:
            continue
        try:
            with engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {ddl}'))
            applied.append(f"{table}.{column}")
            logger.info("schema 补列完成：%s.%s (%s)", table, column, ddl)
        except Exception:  # noqa: BLE001 - 迁移失败不应阻断启动
            logger.exception("schema 补列失败：%s.%s", table, column)

    if not applied:
        logger.debug("schema 检查完成：无需补列")
    return applied
