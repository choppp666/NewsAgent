"""跨模块共用的响应结构。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class HealthOut(BaseModel):
    """健康检查返回数据。"""

    status: str = Field(..., description="服务状态", examples=["ok"])
    app_name: str = Field(..., description="应用名称")
    version: str = Field(..., description="应用版本")
    database: str = Field(..., description="数据库类型")
