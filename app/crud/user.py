"""用户数据访问层（CRUD）。

约定（重要）：
- 本层只做纯粹的数据库操作，不 commit、不抛业务异常；
- 事务边界由 services 层控制，需要主键时使用 flush() 提前拿 ID；
- 查询统一使用 SQLAlchemy 2.0 的 select() 风格。
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.user import User


def create(db: Session, *, nickname: str, identity: str, email: str | None) -> User:
    """新增用户（未提交）。"""
    user = User(nickname=nickname, identity=identity, email=email)
    db.add(user)
    db.flush()  # 立即写库以拿到自增 ID，事务仍由上层控制
    return user


def get(db: Session, user_id: int) -> User | None:
    """按主键查询用户，不存在返回 None。"""
    return db.get(User, user_id)


def get_by_nickname(db: Session, nickname: str) -> User | None:
    """按昵称查询（预留给后续「重名提示」等功能，本阶段未接入接口）。"""
    stmt = select(User).where(User.nickname == nickname).limit(1)
    return db.scalars(stmt).first()


def list_all(db: Session, *, offset: int = 0, limit: int = 100) -> tuple[list[User], int]:
    """分页查询用户列表，返回 (数据, 总数)。"""
    total = db.scalar(select(func.count()).select_from(User)) or 0
    stmt = select(User).order_by(User.id.desc()).offset(offset).limit(limit)
    return list(db.scalars(stmt).all()), total


def update(db: Session, user: User, values: dict) -> User:
    """按字段字典更新用户（未提交）。"""
    for field, value in values.items():
        setattr(user, field, value)
    db.flush()
    return user


def delete(db: Session, user: User) -> None:
    """删除用户（级联删除其订阅与词条）。本阶段未开放接口，保留给后续管理功能。"""
    db.delete(user)
    db.flush()
