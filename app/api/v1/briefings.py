"""新闻简报查询接口。

路由前缀：/api/v1/briefings

只做「读」：简报的生成由 Agent 完成（POST /api/agent/briefings），
这里负责把已入库的简报按用户分页列出来、按 ID 取详情。
把生成与查询分开的好处是权限与职责都清晰——
将来加 JWT 时，查询接口需要「本人可读」，生成接口需要「本人可触发」。
"""

from __future__ import annotations

from fastapi import APIRouter, Path, Query

from app.api.deps import DbSession
from app.core.response import ApiResponse, success
from app.schemas.briefing import BriefingListData, BriefingOut, BriefingSummaryOut
from app.services import briefing_service

router = APIRouter(prefix="/briefings", tags=["新闻简报"])


@router.get(
    "",
    response_model=ApiResponse[BriefingListData],
    summary="按用户列出简报",
    description="按简报日期倒序分页返回摘要（不含正文），用于历史回溯。",
)
def list_briefings(
    db: DbSession,
    user_id: int = Query(ge=1, description="用户 ID"),
    page: int = Query(default=1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(default=20, ge=1, le=100, description="每页数量"),
) -> dict:
    items, total = briefing_service.list_briefings(db, user_id, page=page, page_size=page_size)
    data = BriefingListData(
        total=total,
        page=page,
        page_size=page_size,
        items=[BriefingSummaryOut.from_model(item) for item in items],
    )
    return success(data)


@router.get(
    "/{briefing_id}",
    response_model=ApiResponse[BriefingOut],
    summary="获取简报详情",
    description="返回简报的 Markdown 全文、条数、生成时间与对应的 Agent 运行 ID。",
)
def get_briefing(briefing_id: int = Path(ge=1, description="简报 ID"), *, db: DbSession) -> dict:
    briefing = briefing_service.get_briefing(db, briefing_id)
    return success(BriefingOut.from_model(briefing))
