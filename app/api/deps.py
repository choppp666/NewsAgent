"""接口层公共依赖。

把「重复出现在多个接口里的前置动作」抽成依赖：
- get_db        : 请求级数据库会话
- get_user_or_404 : 路径参数 user_id -> User 对象，不存在直接 404
这样可以避免每个接口都写一遍「查用户 + 判空 + 抛异常」。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Path
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.user import User
from app.services import user_service

# 统一别名，接口签名更简洁
DbSession = Annotated[Session, Depends(get_db)]
UserIdPath = Annotated[int, Path(ge=1, description="用户 ID", examples=[1])]


def get_user_or_404(user_id: UserIdPath, db: DbSession) -> User:
    """根据路径中的 user_id 取用户，不存在时抛 NotFoundError（全局处理器转 404）。"""
    return user_service.get_user(db, user_id)


CurrentUser = Annotated[User, Depends(get_user_or_404)]
