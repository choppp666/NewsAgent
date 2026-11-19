"""新闻简报业务服务。

职责边界：
- 读：把「用户 + 订阅偏好」拼成一份**语义完整**的用户画像交给 Agent（而不是给一串关键词）；
- 写：把 Agent 产出的简报做「同用户同日去重」后入库；
- 任务书：把交付要求写成自然语言任务，**只给目标与约束，不给执行步骤**——
  一旦在这里写「第一步搜 A、第二步搜 B」，Agent 就退化成了固定流程脚本。

为什么画像要带 identity：
「后端开发实习生」和「投资经理」关注同一个话题（比如 RAG）时，
需要的新闻颗粒度完全不同。身份是让模型判断「这条新闻对他有没有用」的关键上下文，
比多给三个关键词有效得多。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import NotFoundError
from app.crud import briefing as briefing_crud
from app.crud import user as user_crud
from app.models.briefing import NewsBriefing
from app.models.base import local_now
from app.models.subscription import TermType
from app.services import subscription_service

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class UserProfile:
    """交给 Agent 的用户画像。"""

    user_id: int
    nickname: str
    identity: str
    email: str | None = None
    topics: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)
    focus: list[str] = field(default_factory=list)  # topics + keywords 的合并视图（给模型看的「关注方向」）

    def to_dict(self) -> dict[str, object]:
        return {
            "user_id": self.user_id,
            "nickname": self.nickname,
            "identity": self.identity,
            "email": self.email,
            "topics": self.topics,
            "keywords": self.keywords,
            "exclude_keywords": self.exclude_keywords,
            "focus": self.focus,
        }


def get_user_profile(db: Session, user_id: int) -> UserProfile:
    """读取用户与其订阅偏好。

    Args:
        db: 数据库会话。
        user_id: 用户 ID。

    Returns:
        UserProfile。用户存在但未配置偏好时，三个词条列表为空。

    Raises:
        NotFoundError: 用户不存在。
    """
    user = user_crud.get(db, user_id)
    if user is None:
        raise NotFoundError(f"用户不存在（id={user_id}）")

    profile = UserProfile(user_id=user.id, nickname=user.nickname, identity=user.identity, email=user.email)

    subscription = subscription_service.get_subscription(db, user_id)
    if subscription is not None:
        grouped: dict[str, list[str]] = {term.value: [] for term in TermType}
        for term in subscription.terms:
            grouped.setdefault(term.term_type, []).append(term.term_value)
        profile.topics = grouped.get(TermType.TOPIC.value, [])
        profile.keywords = grouped.get(TermType.KEYWORD.value, [])
        profile.exclude_keywords = grouped.get(TermType.EXCLUDE_KEYWORD.value, [])

    # 关注方向 = 话题 + 关键词（去重保序），既不丢信息也不重复展示
    seen: set[str] = set()
    for term in [*profile.topics, *profile.keywords]:
        if term.casefold() not in seen:
            seen.add(term.casefold())
            profile.focus.append(term)

    return profile


def briefing_relative_path(user_id: int, briefing_date: date) -> str:
    """简报 Markdown 在工作区内的相对路径。"""
    directory = (settings.BRIEFING_OUTPUT_DIR or "briefings").strip("/\\")
    return f"{directory}/{briefing_date.isoformat()}-user{user_id}.md"


def build_briefing_task(
    profile: UserProfile,
    briefing_date: date,
    *,
    focus: str | None = None,
    today: date | None = None,
    llm_label: str | None = None,
) -> str:
    """把「用户画像 + 交付要求」写成给 Agent 的自然语言任务。

    注意：这里刻意不写「先调用 X，再调用 Y」。
    模型需要自己决定搜几次、搜什么词、读不读原文、够不够写。

    Args:
        profile: 用户画像。
        briefing_date: 简报日期。
        focus: 本次额外关注点。
        today: 当前日期（默认取系统日期），用于提示模型「今天」是哪天。
        llm_label: 真实模型标识（如 `gpt-4o-mini`）。给了就在简报抬头写一条
            「生成方式：Agent 模式」，让读者一眼看出这份简报是谁写的、相关性怎么判断。
            离线策略（mock）传 None —— 那种模式下抬头由 `mock_news` 自己拼，
            文案里会明确写「离线策略」，两种模式的说明不会互相冒充。
    """
    current = today or date.today()
    relative = briefing_relative_path(profile.user_id, briefing_date)

    # 覆盖时段 = 「过去 24 小时」，而不是「今天 00:00 之后」。
    # 原因是资讯站按滚动 24 小时更新，而简报默认在早上 08:00 生成——那个时点
    # 「今天」这个窗口只覆盖 0:00~8:00，几乎抓不到东西，模型只能反复换关键词硬试
    # （实测一轮搜了 19~29 次，命中率极低）。按 24 小时算，08:00 的窗口正好是
    # 昨天 08:00 → 今天 08:00，是一个完整的新闻日。
    now = datetime.now().replace(microsecond=0)
    if briefing_date == now.date():
        window_end = now
    else:  # 补生成历史日期时，用当天的收尾时刻，避免「用现在的窗口写昨天的简报」
        window_end = datetime.combine(briefing_date, time(hour=23, minute=59))
    window_start = window_end - timedelta(hours=24)
    coverage = (
        f"{window_start:%Y-%m-%d %H:%M} ~ {window_end:%Y-%m-%d %H:%M}"
        f"（过去 24 小时，检索请用 date_range=last_24h）"
    )

    topics = "、".join(profile.topics) or "（未设置）"
    keywords = "、".join(profile.keywords) or "（未设置）"
    excludes = "、".join(profile.exclude_keywords) or "（无）"

    structure: list[str] = ["# 每日 AI 新闻简报"]
    if llm_label:
        structure.append(
            f"> 生成方式：Agent 模式（{llm_label}）。条目、来源与链接均来自真实 RSS 检索结果，"
            "相关性由模型按语义判断，摘要与解读由模型撰写。"
        )
    structure += [
        f"- 简报日期：{briefing_date.isoformat()}",
        f"- 覆盖时段：{window_start:%Y-%m-%d %H:%M} ~ {window_end:%Y-%m-%d %H:%M}（过去 24 小时）",
        "- 用户关注方向：…",
        "- 今日新闻数量：N",
        "## 今日重点新闻",
        "### 新闻标题",
        "- 新闻摘要：",
        "- 核心事件：",
        "- 重要性分析：",
        "- 新闻来源：",
        "- 原文链接：",
        "- 发布时间：",
        "## 行业动态总结",
        "## 今日值得关注",
    ]

    lines: list[str] = [
        f"请为我生成 {briefing_date.isoformat()} 的每日 AI 新闻简报。",
        "",
        "【用户信息】",
        f"- user_id：{profile.user_id}",
        f"- 昵称：{profile.nickname}",
        f"- 身份/职业：{profile.identity}",
        f"- 关注话题：{topics}",
        f"- 关键词：{keywords}",
        f"- 排除关键词：{excludes}",
        f"- 今天是：{current.isoformat()}（生成时刻 {now:%H:%M}）",
        f"- 简报覆盖时段：{coverage}",
    ]
    if focus:
        lines.append(f"- 本次额外关注：{focus}")

    lines += [
        "",
        "【交付物】",
        "调用一次 save_briefing，把完整 Markdown 正文作为 content 传进去："
        f"它会校验结构、登记入库，并把同一份内容导出到工作区 `{relative}`。",
        "**不要再用 write_file 写一遍同样的内容**——那样等于把几千字正文重复生成两次，"
        "既慢又可能两份不一致。save_briefing 的返回里会告诉你是否导出成功。",
        "",
        "【简报结构】",
        *structure,
        "",
        "【判断原则】（这些是约束，不是步骤；具体怎么搜、搜几次、读不读原文由你决定）",
        "- 先看全貌再下钻：**先调用一次 `search_news` 且不传 keyword**（浏览模式），"
        "一次拿到时间窗内全部候选清单，从里面挑 3~6 条最相关的读正文；"
        "只有清单里明显缺某个方向时，再用关键词补搜，且补搜不超过 2 次。"
        "不要用十几个关键词反复试探——清单已经把「今天有什么」摆在你面前了。",
        "- 时间窗口用「过去 24 小时」（`date_range=last_24h`）：早上生成时「今天 0 点之后」"
        "几乎没有内容，只挑当天会漏掉昨天下午的高价值报道；确实需要更早的再放宽到 3d 并说明原因。",
        "- 相关性按语义判断：用户的关注点是「他想做什么」，不要只做字面关键词匹配，"
        "也要识别同义说法、上位/下位概念与英文表述。",
        "- 只写工具真实返回的新闻。来源、链接、发布时间必须有出处；确实拿不到的写「未获取到」，不许推测。",
        "- 同一条新闻在多个来源出现时只保留信息最完整的一条，但来源和链接必须保留。",
        "- 宁少勿滥：相关新闻不足就如实写实际条数并说明今天信息量偏少，绝不用无关新闻凑数。",
        "- 摘要必须来自真实抓取到的标题/摘要/正文，不得编造细节、数字、引语。",
        "- 判断信息不足时可以继续搜索或读取原文；确认足够后直接交付，不要为凑工具调用而反复搜索。",
        "- 正文用中文；外文来源请翻译标题与要点，但原文链接保持原样。",
    ]
    return "\n".join(lines)


def upsert_briefing(
    db: Session,
    *,
    user_id: int,
    briefing_date: date,
    title: str,
    content: str,
    news_count: int,
    focus_topics: list[str] | None = None,
    file_path: str | None = None,
    run_id: str | None = None,
) -> tuple[NewsBriefing, bool]:
    """写入或覆盖某用户某天的简报。

    Args:
        db: 会话（本函数内部 commit）。
        user_id: 用户 ID。
        briefing_date: 简报日期（自然日）。
        title: 简报标题。
        content: Markdown 全文。
        news_count: 条目数。
        focus_topics: 生成时的关注方向快照。
        file_path: 导出的 Markdown 相对路径。
        run_id: 生成该简报的 Agent 运行 ID。

    Returns:
        (简报对象, 是否新建)。`created=False` 表示覆盖了当天已有简报。

    Raises:
        NotFoundError: 用户不存在（外键约束前先给出可读错误）。
    """
    if user_crud.get(db, user_id) is None:
        raise NotFoundError(f"用户不存在（id={user_id}），无法保存简报")

    existing = briefing_crud.get_by_user_date(db, user_id, briefing_date)
    created = existing is None

    if existing is None:
        briefing = briefing_crud.create(
            db,
            user_id=user_id,
            briefing_date=briefing_date,
            title=title,
            content=content,
            news_count=news_count,
            focus_topics=focus_topics or [],
            file_path=file_path,
            run_id=run_id,
            generated_at=local_now(),
        )
    else:
        briefing = existing
        briefing.title = title
        briefing.content = content
        briefing.news_count = news_count
        briefing.focus_topics = focus_topics or briefing.focus_topics
        briefing.file_path = file_path or briefing.file_path
        briefing.run_id = run_id or briefing.run_id
        # 覆盖即视为「重新生成」：刷新生成时间，updated_at 由 ORM onupdate 维护
        briefing.generated_at = local_now()

    db.commit()
    db.refresh(briefing)
    logger.info(
        "简报落库 %s: user_id=%s date=%s 条数=%s run_id=%s",
        "新建" if created else "覆盖",
        user_id,
        briefing_date,
        news_count,
        run_id,
    )
    return briefing, created


def list_briefings(db: Session, user_id: int, *, page: int = 1, page_size: int = 20) -> tuple[list[NewsBriefing], int]:
    """分页列出某用户的简报（按日期倒序）。"""
    if user_crud.get(db, user_id) is None:
        raise NotFoundError(f"用户不存在（id={user_id}）")
    items, total = briefing_crud.list_by_user(db, user_id, limit=page_size, offset=(page - 1) * page_size)
    return list(items), total


def get_briefing(db: Session, briefing_id: int) -> NewsBriefing:
    """取单份简报。

    Raises:
        NotFoundError: 简报不存在。
    """
    briefing = briefing_crud.get(db, briefing_id)
    if briefing is None:
        raise NotFoundError(f"简报不存在（id={briefing_id}）")
    return briefing


def get_briefing_by_date(db: Session, user_id: int, briefing_date: date) -> NewsBriefing | None:
    """按用户 + 日期取简报（不存在返回 None）。"""
    return briefing_crud.get_by_user_date(db, user_id, briefing_date)
