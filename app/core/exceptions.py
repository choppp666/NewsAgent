"""业务异常定义与全局异常处理器。

分层原则：
- 数据访问层（crud）不抛业务异常，只做纯粹的数据库操作；
- 业务层（services）在规则不满足时抛 AppError 及其子类；
- 接口层（api）不写 try/except，由 main.py 注册的全局处理器统一转换。
这样接口函数可以保持干净，异常出口只有一个。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import settings
from app.core.response import error

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- 业务异常基类


class AppError(Exception):
    """所有业务异常的基类。

    Attributes:
        code:        业务状态码，约定与 HTTP 状态码一致（便于排查）
        status_code: HTTP 状态码
        message:     面向用户的错误描述
    """

    code: int = 400
    status_code: int = status.HTTP_400_BAD_REQUEST
    default_message: str = "请求处理失败"

    def __init__(
        self,
        message: str | None = None,
        *,
        code: int | None = None,
        status_code: int | None = None,
    ) -> None:
        self.message = message or self.default_message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        super().__init__(self.message)


class ValidationError(AppError):
    """参数校验失败（业务层二次校验，例如关键词数量超限）。"""

    code = 400
    status_code = status.HTTP_400_BAD_REQUEST
    default_message = "参数校验失败"


class NotFoundError(AppError):
    """资源不存在。"""

    code = 404
    status_code = status.HTTP_404_NOT_FOUND
    default_message = "请求的资源不存在"


class ConflictError(AppError):
    """资源冲突（例如重复创建）。"""

    code = 409
    status_code = status.HTTP_409_CONFLICT
    default_message = "资源冲突"


# ---------------------------------------------------------------- 异常处理器


def _json(code: int, message: str, status_code: int, data: Any = None) -> JSONResponse:
    """统一构造 JSON 错误响应。"""
    return JSONResponse(status_code=status_code, content=error(code, message, data))


# 字段名 -> 中文标签，用于把校验错误翻译成人话
_FIELD_LABELS: dict[str, str] = {
    "nickname": "昵称",
    "identity": "身份描述",
    "email": "邮箱",
    "topics": "关注话题",
    "keywords": "关键词",
    "exclude_keywords": "排除关键词",
    "user_id": "用户 ID",
    "page": "页码",
    "page_size": "每页数量",
}


def _friendly_validation_message(exc: RequestValidationError) -> str:
    """把 Pydantic 的英文校验错误转换为中文可读提示。

    只取第一条错误，避免一次返回一屏信息；同时保留字段名，方便定位。
    """
    errors = exc.errors()
    if not errors:
        return "参数校验失败"

    first = errors[0]
    # loc 形如 ("body", "email")，去掉前面的来源标识
    location = [str(part) for part in first.get("loc", []) if part not in ("body", "query", "path", "path_params", "query_params")]
    raw_field = ".".join(location)
    field = _FIELD_LABELS.get(raw_field, raw_field) or "请求参数"
    err_type = first.get("type", "")
    ctx = first.get("ctx") or {}

    if err_type == "string_too_short" and ctx.get("min_length") == 1:
        detail = "不能为空"
    elif err_type == "string_too_short":
        detail = f"长度不能少于 {ctx.get('min_length', 1)} 个字符"
    elif err_type == "string_too_long":
        detail = f"长度不能超过 {ctx.get('max_length', '')} 个字符"
    elif err_type == "too_long":
        detail = f"最多允许 {ctx.get('max_length', '')} 个"
    elif err_type == "value_error":
        detail = "格式不正确" if raw_field == "email" else "内容不合法"
    elif err_type == "greater_than_equal":
        detail = f"不能小于 {ctx.get('ge', '')}"
    elif err_type == "less_than_equal":
        detail = f"不能大于 {ctx.get('le', '')}"
    elif err_type == "list_type":
        detail = "必须是数组"
    elif err_type == "string_type":
        detail = "必须是字符串"
    elif err_type == "int_type" or err_type == "int_parsing":
        detail = "必须是整数"
    elif err_type == "missing":
        detail = "不能为空"
    else:
        detail = first.get("msg", "格式不正确")

    return f"{field}{detail}"


def register_exception_handlers(app: FastAPI) -> None:
    """把异常处理器注册到应用上（在 main.py 中调用一次）。"""

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        logger.warning("业务异常 [%s] %s %s: %s", exc.code, request.method, request.url.path, exc.message)
        return _json(exc.code, exc.message, exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        message = _friendly_validation_message(exc)
        logger.info("参数校验失败 %s %s: %s", request.method, request.url.path, exc.errors())
        return _json(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            message,
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {"errors": exc.errors()} if settings.DEBUG else None,
        )

    @app.exception_handler(IntegrityError)
    async def handle_integrity_error(request: Request, exc: IntegrityError) -> JSONResponse:
        # 唯一约束、外键约束等数据库层面的冲突
        logger.warning("数据库约束冲突 %s %s: %s", request.method, request.url.path, exc.orig)
        return _json(
            status.HTTP_409_CONFLICT,
            "数据冲突：记录已存在或存在关联数据",
            status.HTTP_409_CONFLICT,
        )

    @app.exception_handler(SQLAlchemyError)
    async def handle_db_error(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        logger.exception("数据库错误 %s %s", request.method, request.url.path)
        return _json(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "数据库操作失败，请稍后重试",
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # 覆盖 404 / 405 等框架级异常，保证返回格式统一
        default_messages = {
            status.HTTP_404_NOT_FOUND: "接口不存在",
            status.HTTP_405_METHOD_NOT_ALLOWED: "请求方法不被允许",
        }
        message = default_messages.get(exc.status_code) or str(exc.detail)
        return _json(exc.status_code, message, exc.status_code)

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("未捕获异常 %s %s", request.method, request.url.path)
        message = f"服务器内部错误：{exc}" if settings.DEBUG else "服务器内部错误，请稍后重试"
        return _json(status.HTTP_500_INTERNAL_SERVER_ERROR, message, status.HTTP_500_INTERNAL_SERVER_ERROR)
