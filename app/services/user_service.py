"""用户业务服务层。

职责：
- 组织数据访问层的调用；
- 控制事务边界（commit / rollback）；
- 执行业务规则校验（例如「用户不存在」「没有需要更新的字段」）。

接口层不直接接触 crud 与 session，只调用 service。
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError, ValidationError
from app.crud import user as user_crud
from app.models.user import User
from app.schemas.user import UserCreate, UserUpdate

logger = logging.getLogger(__name__)


def create_user(db: Session, payload: UserCreate) -> User:
    """创建用户。

    本阶段未引入登录鉴权，昵称允许重复（同名的真实用户是存在的）；
    若后续需要唯一标识，建议改用邮箱或手机号做唯一约束。
    """
    user = user_crud.create(
        db,
        nickname=payload.nickname,
        identity=payload.identity,
        email=payload.email,
    )
    db.commit()
    db.refresh(user)
    logger.info("创建用户成功: id=%s nickname=%s", user.id, user.nickname)
    return user


def get_user(db: Session, user_id: int) -> User:
    """获取用户，不存在则抛 404。"""
    user = user_crud.get(db, user_id)
    if user is None:
        raise NotFoundError(f"用户不存在（id={user_id}）")
    return user


def list_users(db: Session, *, page: int = 1, page_size: int = 20) -> tuple[list[User], int]:
    """分页获取用户列表（辅助接口，便于前端演示时切换已创建的用户）。"""
    offset = (page - 1) * page_size
    return user_crud.list_all(db, offset=offset, limit=page_size)


def update_user(db: Session, user_id: int, payload: UserUpdate) -> User:
    """更新用户信息（局部更新，只处理显式传入的字段）。"""
    user = get_user(db, user_id)

    # exclude_unset=True：只取前端真正传了的字段，
    # 这样 "email": null 会被保留（表示清空），未传 email 则不动原值。
    values = payload.model_dump(exclude_unset=True)
    if not values:
        raise ValidationError("请求体中没有任何需要更新的字段")

    # 业务层二次校验：昵称/身份即使传了也不能为空白（Pydantic 已处理，这里兜底）
    for field in ("nickname", "identity"):
        if field in values and not str(values[field]).strip():
            raise ValidationError(f"{field} 不能为空")

    user_crud.update(db, user, values)
    db.commit()
    db.refresh(user)
    logger.info("更新用户成功: id=%s fields=%s", user.id, list(values.keys()))
    return user
