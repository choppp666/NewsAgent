"""订阅偏好相关请求 / 响应模型。"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import BaseModel, Field, StringConstraints, field_validator

from app.models.subscription import TermType

if TYPE_CHECKING:
    from app.models.subscription import Subscription

# ---------------- 数量与长度限制（集中定义，方便调整） ----------------
MAX_TOPICS: int = 20          # 关注话题最多 20 个
MAX_KEYWORDS: int = 50        # 关键词最多 50 个
MAX_EXCLUDE_KEYWORDS: int = 50  # 排除关键词最多 50 个
MAX_TERM_LENGTH: int = 30     # 单个词条最大长度

# 单个词条：去首尾空格、不允许空串、最长 30 字符
Term = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_TERM_LENGTH),
]

# 兼容用户手动输入时的多种分隔符
_SEPARATORS = (",", "，", ";", "；", "\n", "、", "|")


def _normalize_terms(raw: Any) -> list[str]:
    """把前端传入的任意形态清洗为标准字符串列表。

    处理内容：
    1. None / 空 -> 空列表；
    2. 单个字符串 -> 按中英文逗号、分号、顿号等切分（方便 Postman 调试）；
    3. 逐项 strip、丢弃空值、保持原始顺序去重（忽略大小写与全半角空格）；
    4. 超长词条直接截断到 MAX_TERM_LENGTH，避免因个别脏数据整体报错。

    注意：去重后再交给 Pydantic 做长度/项数校验，因此这里的清洗不会掩盖错误。
    """
    if raw is None:
        return []

    if isinstance(raw, str):
        text = raw
        for sep in _SEPARATORS:
            text = text.replace(sep, ",")
        raw = text.split(",")

    if not isinstance(raw, (list, tuple, set)):
        return []

    result: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if item is None:
            continue
        value = str(item).strip()
        if not value:
            continue
        if len(value) > MAX_TERM_LENGTH:
            value = value[:MAX_TERM_LENGTH]
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


class SubscriptionUpsert(BaseModel):
    """创建或更新订阅偏好请求体（PUT 全量覆盖语义）。

    三个字段均允许为空列表，表示该维度不设置任何词条；
    未传的字段按空列表处理（因为 PUT 是覆盖，不是局部更新）。
    """

    topics: Annotated[
        list[Term],
        Field(default_factory=list, max_length=MAX_TOPICS, description=f"关注话题，最多 {MAX_TOPICS} 个"),
    ]
    keywords: Annotated[
        list[Term],
        Field(default_factory=list, max_length=MAX_KEYWORDS, description=f"关键词，最多 {MAX_KEYWORDS} 个"),
    ]
    exclude_keywords: Annotated[
        list[Term],
        Field(default_factory=list, max_length=MAX_EXCLUDE_KEYWORDS, description="排除关键词，命中则不推送"),
    ]

    @field_validator("topics", "keywords", "exclude_keywords", mode="before")
    @classmethod
    def _clean_terms(cls, value: Any) -> list[str]:
        return _normalize_terms(value)


class SubscriptionOut(BaseModel):
    """订阅偏好响应体。"""

    user_id: int = Field(..., description="用户 ID")
    topics: list[str] = Field(default_factory=list, description="关注话题")
    keywords: list[str] = Field(default_factory=list, description="关键词")
    exclude_keywords: list[str] = Field(default_factory=list, description="排除关键词")
    created_at: datetime = Field(..., description="订阅创建时间")
    updated_at: datetime = Field(..., description="订阅最后更新时间")

    @classmethod
    def from_model(cls, subscription: Subscription) -> SubscriptionOut:
        """把 ORM 对象（含词条明细）拍平成前端友好的结构。

        词条表是一行一个词，这里按 term_type 分组聚合，
        前端拿到的就是三个数组，不需要自己再过滤。
        """
        grouped: dict[str, list[str]] = {t.value: [] for t in TermType}
        for term in subscription.terms:
            grouped.setdefault(term.term_type, []).append(term.term_value)

        return cls(
            user_id=subscription.user_id,
            topics=grouped[TermType.TOPIC.value],
            keywords=grouped[TermType.KEYWORD.value],
            exclude_keywords=grouped[TermType.EXCLUDE_KEYWORD.value],
            created_at=subscription.created_at,
            updated_at=subscription.updated_at,
        )
