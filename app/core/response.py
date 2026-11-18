"""统一返回数据结构。

所有接口（含异常响应）都遵循同一信封格式，便于前端统一处理：

    {
        "code": 0,                # 0 = 成功；非 0 = 失败（与 HTTP 状态码保持一致）
        "message": "success",     # 人类可读的提示信息
        "data": {...}             # 业务数据，失败时为 null
    }
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    """通用响应体，用于 OpenAPI 文档声明（response_model=ApiResponse[UserOut]）。"""

    code: int = Field(default=0, description="业务状态码，0 表示成功")
    message: str = Field(default="success", description="提示信息")
    data: T | None = Field(default=None, description="业务数据")


def success(data: Any = None, message: str = "success", code: int = 0) -> dict[str, Any]:
    """构造成功响应字典。

    直接返回 dict 而非 Pydantic 对象，避免额外序列化开销；
    字段顺序与 ApiResponse 保持一致。
    """
    return {"code": code, "message": message, "data": data}


def error(code: int, message: str, data: Any = None) -> dict[str, Any]:
    """构造失败响应字典。"""
    return {"code": code, "message": message, "data": data}
