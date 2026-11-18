"""FastAPI 应用入口。

启动方式：
    uvicorn app.main:app --reload

职责：
1. 创建应用实例并配置文档；
2. 注册 CORS、全局异常处理器、v1 路由；
3. 挂载前端静态资源（SPA 首页）；
4. 启动时自动建表。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.agent import router as agent_router
from app.api.v1.router import api_router
from app.core.config import STATIC_DIR, settings
from app.core.database import init_db
from app.core.exceptions import register_exception_handlers
from app.core.response import ApiResponse, success
from app.schemas.common import HealthOut

# ---------------------------------------------------------------- 日志配置
logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- 生命周期
@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动 / 关闭钩子。

    启动时建表（幂等）。生产环境表结构变更建议改用 Alembic 迁移，
    本阶段为轻量化考虑，直接使用 create_all。
    """
    logger.info("正在启动 %s v%s ...", settings.APP_NAME, settings.APP_VERSION)
    init_db()
    logger.info("启动完成 | 前端首页: / | 接口文档: /docs | 配置默认监听: %s:%s", settings.HOST, settings.PORT)
    logger.info(
        "Agent 已就绪 | 模式=%s | 工作区=%s | 轮数上限=%s | 工具次数上限=%s",
        settings.AGENT_MODE,
        settings.agent_workspace_path,
        settings.AGENT_MAX_STEPS,
        settings.AGENT_MAX_TOOL_CALLS,
    )
    yield
    logger.info("应用已关闭")


# ---------------------------------------------------------------- 应用实例
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description=(
        "每日 AI 新闻助手 —— 第三阶段：新闻采集、个性化筛选与简报生成。\n\n"
        "第一阶段提供用户与订阅偏好管理（/api/v1/*）；"
        "第二阶段提供 Agent 调试接口（/api/agent/*），支持文件与命令工具；"
        "第三阶段在同一个 Agent 上新增 search_news / fetch_article / get_user_preferences / save_briefing，"
        "由模型自行决定搜索策略并生成简报（POST /api/agent/briefings），简报可按用户查询（/api/v1/briefings）。\n"
        "定时任务与消息推送在后续阶段实现。"
    ),
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

# ---------------------------------------------------------------- 中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    # 通配来源时不能开启 credentials（浏览器规范限制），否则跨域会失败
    allow_credentials=settings.cors_origin_list != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------- 异常处理器
register_exception_handlers(app)

# ---------------------------------------------------------------- 路由
app.include_router(api_router, prefix=settings.API_V1_PREFIX)
# Agent 调试接口：按第二阶段要求固定为 /api/agent/*（不在 v1 前缀下）
app.include_router(agent_router, prefix="/api")


@app.get("/api/health", response_model=ApiResponse[HealthOut], tags=["系统"], summary="健康检查")
def health() -> dict:
    """用于快速确认服务与数据库配置是否正常。"""
    return success(
        HealthOut(
            status="ok",
            app_name=settings.APP_NAME,
            version=settings.APP_VERSION,
            database="sqlite" if settings.is_sqlite else "other",
        )
    )


# ---------------------------------------------------------------- 静态资源
# 注意：必须放在所有接口之后挂载，FastAPI 按注册顺序匹配路由，
# 先注册 /api/v1/* 才不会被打到静态目录里。
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
    logger.info("前端静态资源已挂载: %s", STATIC_DIR)
else:  # pragma: no cover - 目录缺失时给出明确提示
    logger.warning("未找到前端静态资源目录: %s", STATIC_DIR)
