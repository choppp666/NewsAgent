"""用户相关请求 / 响应模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints, field_validator

# 昵称：去首尾空格、1~50 字符
Nickname = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]
# 身份描述：去首尾空格、1~200 字符
Identity = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class UserCreate(BaseModel):
    """创建用户请求体。"""

    nickname: Nickname = Field(..., description="用户昵称", examples=["小林"])
    identity: Identity = Field(
        ..., description="身份或职业描述，用于后续生成个性化简报", examples=["后端开发实习生"]
    )
    email: EmailStr | None = Field(default=None, description="新闻接收邮箱（选填）")

    @field_validator("email", mode="before")
    @classmethod
    def _empty_email_to_none(cls, value: object) -> object:
        """空字符串视为「未填写」，避免前端传 '' 触发格式错误。"""
        if isinstance(value, str) and not value.strip():
            return None
        return value


class UserUpdate(BaseModel):
    """修改用户请求体（PATCH 语义：只更新传入的字段）。

    显式传 "email": null 表示清空邮箱；不传该字段则保持原值。
    """

    nickname: Nickname | None = Field(default=None, description="用户昵称")
    identity: Identity | None = Field(default=None, description="身份或职业描述")
    email: EmailStr | None = Field(default=None, description="新闻接收邮箱（传 null 可清空）")

    @field_validator("email", mode="before")
    @classmethod
    def _empty_email_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value


class UserOut(BaseModel):
    """用户信息响应体。"""

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(..., description="用户 ID")
    nickname: str = Field(..., description="用户昵称")
    identity: str = Field(..., description="身份或职业描述")
    email: str | None = Field(default=None, description="新闻接收邮箱")
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime = Field(..., description="更新时间")


class UserListData(BaseModel):
    """用户列表响应体（前端用于演示 / 找回已创建的用户）。"""

    total: int = Field(..., description="用户总数")
    items: list[UserOut] = Field(default_factory=list, description="用户列表")
