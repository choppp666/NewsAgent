"""新闻简报数据访问层。

只做增删改查，不做业务判断（校验、去重语义在 service 层）。
`upsert` 例外：它必须在一次查询里拿到「同一天是否已存在」再决定新增还是覆盖，
放在这里可以避免 service 层重复写查询语句。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.briefing import NewsBriefing


def get(db: Session, briefing_id: int) -> NewsBriefing | None:
    """按主键查询。"""
    return db.get(NewsBriefing, briefing_id)


def get_by_user_date(db: Session, user_id: int, briefing_date: date) -> NewsBriefing | None:
    """查询某用户某一天的简报（唯一键查询）。"""
    stmt = (
        select(NewsBriefing)
        .where(NewsBriefing.user_id == user_id, NewsBriefing.briefing_date == briefing_date)
        .limit(1)
    )
    return db.scalars(stmt).first()


def list_by_user(
    db: Session,
    user_id: int,
    *,
    limit: int = 20,
    offset: int = 0,
) -> tuple[Sequence[NewsBriefing], int]:
    """按日期倒序分页列出某用户的简报。"""
    base = select(NewsBriefing).where(NewsBriefing.user_id == user_id)
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    stmt = base.order_by(NewsBriefing.briefing_date.desc(), NewsBriefing.id.desc()).limit(limit).offset(offset)
    return db.scalars(stmt).all(), int(total)


def create(db: Session, **fields: object) -> NewsBriefing:
    """新建一条简报（未提交）。"""
    briefing = NewsBriefing(**fields)
    db.add(briefing)
    db.flush()
    return briefing
