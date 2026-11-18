"""FastAPI 应用入口。

启动方式：
    uvicorn app.main:app --reload

职责：
1. 创建应用实例并配置文档；
2. 注册 CORS、全局异常处理器、v1 路由；
3. 挂载前端静态资源（SPA 首页）；
4. 启动时自动建表、补齐新增列、补建任务记录，并拉起任务 worker 与定时调度器。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.agent import router as agent_router
from app.api.v1.router import api_router
from app.core.config import STATIC_DIR, settings
from app.core.database import SessionLocal, init_db
from app.core.exceptions import register_exception_handlers
from app.core.response import ApiResponse, success
from app.schemas.common import HealthOut
from app.scheduler.manager import scheduler_manager
from app.services.generation_worker import generation_worker

# ---------------------------------------------------------------- 日志配置
logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def _backfill_task_records() -> None:
    """为第三阶段的历史简报补建任务记录（升级对齐，幂等）。"""
    from app.services import task_service

    try:
        with SessionLocal() as db:
            task_service.backfill_from_briefings(db)
    except Exception:  # noqa: BLE001 - 升级对齐失败不阻断启动
        logger.warning("补建历史任务记录失败（不影响新任务）", exc_info=True)


# ---------------------------------------------------------------- 生命周期
@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动 / 关闭钩子。

    启动顺序是有讲究的：
    1. 建表 + 补列 —— 后面所有查询都依赖新字段（users.push_enabled）；
    2. 补建历史任务 —— 让老简报在第四阶段的历史列表里可见；
    3. 启动 worker —— 先有消费者，再由调度器提交任务，避免任务入队后无人处理；
    4. 启动调度器 + 启动补跑 —— 补跑在后台执行，不阻塞「服务已就绪」的信号。
    """
    logger.info("正在启动 %s v%s ...", settings.APP_NAME, settings.APP_VERSION)
    init_db()
    _backfill_task_records()

    await generation_worker.start()
    await scheduler_manager.start()

    if settings.SCHEDULER_ENABLED and settings.SCHEDULER_CATCH_UP:
        # 不 await：补跑可能要遍历几十个用户并登记任务，不该让启动卡在这里
        asyncio.create_task(scheduler_manager.run_catch_up(), name="scheduler-catch-up")

    logger.info("启动完成 | 前端首页: / | 接口文档: /docs | 配置默认监听: %s:%s", settings.HOST, settings.PORT)
    logger.info(
        "Agent 已就绪 | 模式=%s | 工作区=%s | 轮数上限=%s | 工具次数上限=%s",
        settings.AGENT_MODE,
        settings.agent_workspace_path,
        settings.AGENT_MAX_STEPS,
        settings.AGENT_MAX_TOOL_CALLS,
    )
    logger.info(
        "定时任务 | 启用=%s 时间=%02d:%02d 时区=%s 并发=%s | 邮件推送=%s（SMTP %s）",
        settings.SCHEDULER_ENABLED,
        *settings.scheduler_time_parts,
        settings.SCHEDULER_TIMEZONE,
        settings.SCHEDULER_MAX_CONCURRENCY,
        "启用" if settings.PUSH_ENABLED else "关闭",
        "已配置" if settings.smtp_configured else "未配置，推送将记为 skipped",
    )

    yield

    # 关闭顺序与启动相反：先停调度器（不再产生新任务），再停 worker
    await scheduler_manager.shutdown()
    await generation_worker.stop()
    logger.info("应用已关闭")



# ---------------------------------------------------------------- 应用实例
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description=(
        "每日 AI 新闻助手 —— 第四阶段：定时任务、邮件推送与历史简报管理。\n\n"
        "第一阶段提供用户与订阅偏好管理（/api/v1/users、/preferences）；"
        "第二阶段提供 Agent 核心与调试接口（/api/agent/*）；"
        "第三阶段新增新闻工具（search_news / fetch_article / get_user_preferences / save_briefing）"
        "与简报生成（POST /api/agent/briefings）；"
        "第四阶段把整条链路串成可长期运行的形态：APScheduler 每日定时派发（GET /api/v1/scheduler）、"
        "任务状态机与幂等控制（POST /api/v1/users/{id}/briefings/trigger、GET /api/v1/tasks）、"
        "SMTP 邮件推送与失败重试（/api/v1/users/{id}/push-settings、/push-records/{id}/retry）。"
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
