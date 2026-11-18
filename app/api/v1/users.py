"""用户相关接口。

路由前缀：/api/v1/users
"""

from __future__ import annotations

from fastapi import APIRouter, Query, status

from app.api.deps import CurrentUser, DbSession
from app.core.response import ApiResponse, success
from app.schemas.user import UserCreate, UserListData, UserOut, UserUpdate
from app.services import user_service

router = APIRouter(prefix="/users", tags=["用户管理"])


@router.post(
    "",
    response_model=ApiResponse[UserOut],
    status_code=status.HTTP_201_CREATED,
    summary="创建用户",
    description="填写昵称、身份描述（可选邮箱）创建用户，返回带 ID 的用户信息。",
)
def create_user(payload: UserCreate, db: DbSession) -> dict:
    user = user_service.create_user(db, payload)
    return success(UserOut.model_validate(user), message="用户创建成功")


@router.get(
    "",
    response_model=ApiResponse[UserListData],
    summary="获取用户列表（辅助）",
    description="分页返回已创建的用户，便于前端演示时切换 / 找回已保存的配置。",
)
def list_users(
    db: DbSession,
    page: int = Query(default=1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(default=20, ge=1, le=100, description="每页数量"),
) -> dict:
    users, total = user_service.list_users(db, page=page, page_size=page_size)
    data = UserListData(total=total, items=[UserOut.model_validate(u) for u in users])
    return success(data)


@router.get(
    "/{user_id}",
    response_model=ApiResponse[UserOut],
    summary="获取用户信息",
    description="根据用户 ID 查询用户基础信息。",
)
def get_user(user: CurrentUser) -> dict:
    return success(UserOut.model_validate(user))


@router.put(
    "/{user_id}",
    response_model=ApiResponse[UserOut],
    summary="修改用户信息",
    description="局部更新：只修改请求体中出现的字段；email 传 null 表示清空邮箱。",
)
def update_user(payload: UserUpdate, user: CurrentUser, db: DbSession) -> dict:
    updated = user_service.update_user(db, user.id, payload)
    return success(UserOut.model_validate(updated), message="用户信息已更新")
