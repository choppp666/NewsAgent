"""ORM 模型公共基类。

统一主键与时间戳字段，避免每个模型重复声明：
- id         : 自增主键
- created_at : 创建时间
- updated_at : 更新时间（ORM 层 onupdate 自动维护）

时间统一使用「服务器本地时间（naive datetime）」。若后续需要多时区支持，
可改为 UTC 存储 + 展示层转换。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer
from sqlalchemy.orm import Mapped, mapped_column


def local_now() -> datetime:
    """当前本地时间（精确到秒，便于前端直接展示）。"""
    return datetime.now().replace(microsecond=0)


class BaseEntityMixin:
    """通用实体字段：主键 + 创建/更新时间。"""

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="主键")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=local_now, comment="创建时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=local_now, onupdate=local_now, comment="更新时间"
    )
