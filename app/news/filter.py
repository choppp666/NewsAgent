"""新闻筛选：时间窗口、关键词匹配与相关度打分。

这一层只做**机械判断**，不做「这条新闻对用户重要吗」这种语义决策：
- 时间窗口：新闻是否落在指定日期范围内（发布时间无法确认的如何处理，见下）；
- 关键词命中：用于把源里几百条无关新闻快速裁到几十条候选，避免把整个 Feed 塞给模型；
- 排除词：用户明确不看的内容直接挡掉。

真正的「相关性」判断留给大模型——因为用户关心的是「AI Agent 落地」，
而标题里可能一个「Agent」都没有（比如「某公司发布新工具，可自动完成多步任务」）。
工具层做得越「聪明」，越容易把该给模型的信息提前筛掉。

关于发布时间缺失：
资讯源里约 5%~10% 的条目没有可用时间戳。默认**不放行**（这是「今日简报」，
放进一条时间不明的新闻会污染结论），但会把条数如实回报，让模型知道
「有 N 条无法确认时间，已排除」，而不是悄悄消失。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

# 关键词切分：空格、中英文逗号顿号分号、竖线、加号
_QUERY_SPLIT_RE = re.compile(r"[\s,，、;；|/]+")
# 英文词边界：让 "agent" 不匹配 "agentship"，但允许复数形式
_ASCII_TERM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 \-_.+]*$")


class DateRangeError(ValueError):
    """时间范围参数无法解析。"""


@dataclass(slots=True)
class DateWindow:
    """一个左闭右开的时间窗口。"""

    start: datetime | None
    end: datetime
    label: str
    spec: str

    def contains(self, moment: datetime | None) -> bool:
        """判断某个时间点是否在窗口内；None（时间未知）一律返回 False。"""
        if moment is None:
            return False
        if self.start is not None and moment < self.start:
            return False
        return moment <= self.end

    def to_dict(self) -> dict[str, str | None]:
        return {
            "spec": self.spec,
            "label": self.label,
            "start": self.start.isoformat(timespec="minutes") if self.start else None,
            "end": self.end.isoformat(timespec="minutes"),
        }


def _day_start(moment: datetime) -> datetime:
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def _parse_absolute(text: str) -> datetime | None:
    """解析 YYYY-MM-DD，可选带 HH:MM。"""
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def parse_date_range(spec: str | None, *, now: datetime | None = None) -> DateWindow:
    """把自然语言/日期字符串解析成时间窗口。

    支持的写法（不区分大小写）：
        today / 今日 / 今天            今天 00:00 ~ 现在
        yesterday / 昨日 / 昨天        昨天 00:00 ~ 今天 00:00
        last_24h / 24h / 近24小时      现在-24h ~ 现在
        3d / 3天 / last_3_days        最近 3 天
        week / 近7天                   最近 7 天
        2026-11-18                    当天
        2026-11-17:2026-11-18         闭区间

    Args:
        spec: 上述任意写法；空值等价于 today。
        now: 用于计算的「当前时间」，测试可注入。

    Returns:
        DateWindow。

    Raises:
        DateRangeError: 无法识别的时间范围写法。
    """
    current = (now or datetime.now()).replace(microsecond=0)
    text = (spec or "today").strip()
    lowered = text.lower().replace(" ", "")

    if lowered in ("today", "今日", "今天", "now", "当前"):
        return DateWindow(start=_day_start(current), end=current, label="今天", spec=text)

    if lowered in ("yesterday", "昨日", "昨天"):
        start = _day_start(current) - timedelta(days=1)
        return DateWindow(start=start, end=_day_start(current), label="昨天", spec=text)

    if lowered in ("last_24h", "24h", "近24小时", "最近24小时"):
        return DateWindow(start=current - timedelta(hours=24), end=current, label="最近 24 小时", spec=text)

    if lowered in ("week", "last_week", "7d", "近7天", "最近7天", "本周"):
        return DateWindow(start=_day_start(current) - timedelta(days=6), end=current, label="最近 7 天", spec=text)

    # 3d / 3天 / last_3_days
    days_match = re.fullmatch(r"(?:last_)?(\d{1,2})d", lowered) or re.fullmatch(r"(?:最近|近)?(\d{1,2})天", lowered)
    if days_match:
        days = int(days_match.group(1))
        if not 1 <= days <= 30:
            raise DateRangeError(f"天数需在 1~30 之间，收到 {days}")
        return DateWindow(
            start=_day_start(current) - timedelta(days=days - 1),
            end=current,
            label=f"最近 {days} 天",
            spec=text,
        )

    # 区间：2026-11-17:2026-11-18 / 2026-11-17~2026-11-18 / 2026-11-17至2026-11-18
    if any(sep in text for sep in (":", "~", "至")) and re.search(r"\d{4}", text):
        parts = re.split(r"[:~至]", text, maxsplit=1)
        if len(parts) == 2:
            left = _parse_absolute(parts[0].strip())
            right = _parse_absolute(parts[1].strip())
            if left and right:
                end = right if right.hour or right.minute else right + timedelta(days=1)
                if end <= left:
                    raise DateRangeError(f"时间区间为空：{text}")
                return DateWindow(start=left, end=end, label=f"{left:%Y-%m-%d} 至 {right:%Y-%m-%d}", spec=text)

    single = _parse_absolute(text)
    if single:
        end = single if (single.hour or single.minute) else single + timedelta(days=1)
        return DateWindow(start=single, end=end, label=f"{single:%Y-%m-%d}", spec=text)

    raise DateRangeError(
        f"无法识别的时间范围：{text!r}。可用写法：today / yesterday / last_24h / 3d / 7d / 2026-11-18 / 2026-11-17:2026-11-18"
    )


# ---------------------------------------------------------------- 关键词匹配


def _term_matches(text: str, term: str) -> bool:
    """单个词条是否出现在文本中。

    英文词用词边界匹配（避免 `ai` 命中 `said`），并容忍复数；
    中文直接子串匹配（中文没有词边界，正则 \\b 对中文无效）。
    """
    if not text or not term:
        return False
    lowered = text.casefold()
    needle = term.casefold()

    if _ASCII_TERM_RE.match(term):
        pattern = r"(?<![a-z0-9])" + re.escape(needle) + r"(?:s|es)?(?![a-z0-9])"
        return re.search(pattern, lowered) is not None
    return needle in lowered


@dataclass(slots=True)
class Query:
    """一个搜索关键词的解析结果。

    `tokens` 是对原始关键词的切分（「AI Agent 开发」→ ["AI","Agent","开发"]），
    命中判定采用「过半命中即算相关」：既能容忍模型写长一点的自然语言，
    又不至于像「任一命中」那样把无关新闻全放进来。
    """

    raw: str
    tokens: list[str]

    @property
    def required_hits(self) -> int:
        if len(self.tokens) <= 1:
            return 1
        return max(1, math.ceil(len(self.tokens) / 2))


def build_query(raw: str) -> Query:
    """把关键词字符串切成词元。"""
    text = (raw or "").strip()
    tokens = [token for token in _QUERY_SPLIT_RE.split(text) if token]
    return Query(raw=text, tokens=tokens or ([text] if text else []))


@dataclass(slots=True)
class MatchResult:
    """一条新闻相对某个关键词的匹配结果。"""

    score: int
    matched_tokens: list[str]
    title_hit: bool

    @property
    def ok(self) -> bool:
        return bool(self.matched_tokens)


def match_item(
    query: Query,
    *,
    title: str,
    summary: str = "",
    categories: list[str] | None = None,
) -> MatchResult:
    """计算一条新闻与关键词的匹配度。

    打分权重（标题最重要，符合「新闻看标题」的直觉）：
        标题命中 +5 / 分类命中 +3 / 摘要命中 +2
    """
    category_text = " ".join(categories or [])
    score = 0
    matched: list[str] = []
    title_hit = False

    for token in query.tokens:
        hit = False
        if _term_matches(title, token):
            score += 5
            hit = True
            title_hit = True
        if category_text and _term_matches(category_text, token):
            score += 3
            hit = True
        if summary and _term_matches(summary, token):
            score += 2
            hit = True
        if hit:
            matched.append(token)

    if len(matched) < query.required_hits:
        return MatchResult(score=0, matched_tokens=[], title_hit=False)

    return MatchResult(score=score, matched_tokens=matched, title_hit=title_hit)


def find_excluded(text: str, exclude_terms: list[str]) -> str | None:
    """返回命中的第一个排除词（没有命中返回 None）。"""
    for term in exclude_terms or []:
        if _term_matches(text, term):
            return term
    return None
