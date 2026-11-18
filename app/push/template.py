"""邮件内容模板。

推送的四项必需品（验收要求）在这里一次性组装：
1. **今日新闻简报标题** → 邮件主题 + HTML 头部；
2. **新闻摘要** → 从简报 Markdown 的 `- 新闻摘要：` 行提取（不是重写，而是摘录，
   保证邮件与详情页内容一致）；
3. **简报查看入口** → `PUSH_BASE_URL/#/briefing/{id}`；
4. **原始新闻链接** → 每条新闻的 `- 原文链接：`，并在末尾汇总一份链接清单。

为什么不直接把整份 Markdown 塞进邮件：
真实简报动辄 15~20 KB，全文进邮件会让移动端用户翻很久才看到重点，
也更容易被判为垃圾邮件。邮件只做「摘要 + 入口」，完整内容留在页面上看。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import escape

from app.models.briefing import NewsBriefing
from app.models.user import User
from app.push.markdown import markdown_to_text

__all__ = ["NewsItem", "EmailContent", "build_email_content", "parse_news_items", "collect_links"]

_HEADING_ITEM = re.compile(r"^###\s+(.+)$")
_KEY_VALUE = re.compile(r"^[-*]\s*([^：:]{1,12})[：:]\s*(.+)$")
_URL = re.compile(r"https?://[^\s<>\"'）)】\]]+")

# 摘要上限：太长会把邮件撑得很长，正文详情页能看到全文
_SUMMARY_LIMIT = 220
_MAX_ITEMS_IN_MAIL = 8
_MAX_LINKS = 12


@dataclass(slots=True)
class NewsItem:
    """简报里的一条新闻（从 Markdown 结构里解析出来）。"""

    title: str
    summary: str = ""
    source: str = ""
    url: str = ""
    published: str = ""


@dataclass(slots=True)
class EmailContent:
    """一封邮件的全部内容。"""

    subject: str
    text: str
    html: str
    items: list[NewsItem] = field(default_factory=list)
    links: list[str] = field(default_factory=list)


def _clean(text: str) -> str:
    """去掉行内 Markdown 标记并压掉多余空白。"""
    value = text.strip()
    value = re.sub(r"\*\*([^*]+)\*\*", r"\1", value)
    value = re.sub(r"`([^`]+)`", r"\1", value)
    return re.sub(r"\s{2,}", " ", value)


def _truncate(text: str, limit: int = _SUMMARY_LIMIT) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def parse_news_items(markdown: str) -> list[NewsItem]:
    """从简报 Markdown 中解析出新闻条目。

    兼容两种写法：
    - 标准结构：`### 标题` + 若干 `- 字段：值`；
    - 兜底结构：没有 `###` 时，退化为按 `- 新闻摘要：` 分块（避免邮件彻底没内容）。
    """
    lines = (markdown or "").replace("\r\n", "\n").split("\n")
    items: list[NewsItem] = []
    current: NewsItem | None = None

    for raw in lines:
        line = raw.rstrip()
        heading = _HEADING_ITEM.match(line)
        if heading:
            current = NewsItem(title=_clean(heading.group(1)))
            # 去掉「1. 」「2、」这类序号，邮件里不需要
            current.title = re.sub(r"^\d+[.、)]\s*", "", current.title)
            items.append(current)
            continue

        if line.startswith("## "):
            # 进入「行业动态总结」等非条目章节，停止解析
            current = None
            continue

        if current is None or not line.startswith(("-", "*")):
            continue

        matched = _KEY_VALUE.match(line)
        if not matched:
            continue
        key, value = matched.group(1).strip(), matched.group(2).strip()
        if "摘要" in key:
            current.summary = _clean(value)
        elif "来源" in key:
            current.source = _clean(value)
        elif "链接" in key:
            found = _URL.search(value)
            if found:
                current.url = found.group(0)
        elif "时间" in key or "发布" in key:
            current.published = _clean(value)

    items = [item for item in items if item.title]
    if items:
        return items

    # 兜底：只有摘要没有小标题的简报
    for matched in re.finditer(r"^[-*]\s*新闻摘要[：:]\s*(.+)$", markdown or "", re.MULTILINE):
        items.append(NewsItem(title="（未命名条目）", summary=_clean(matched.group(1))))
    return items


def collect_links(markdown: str) -> list[str]:
    """收集简报中出现的全部 http(s) 链接（去重保序）。"""
    seen: set[str] = set()
    links: list[str] = []
    for found in _URL.finditer(markdown or ""):
        url = found.group(0).rstrip("。，、；;:：")
        if url in seen:
            continue
        seen.add(url)
        links.append(url)
        if len(links) >= _MAX_LINKS:
            break
    return links


def _briefing_url(base_url: str, briefing_id: int) -> str:
    base = (base_url or "").rstrip("/")
    return f"{base}/#/briefing/{briefing_id}"


def _build_subject(briefing: NewsBriefing) -> str:
    title = (briefing.title or "每日 AI 新闻简报").strip()
    if len(title) > 120:
        title = title[:119] + "…"
    return f"【AI 新闻简报】{briefing.briefing_date} · {title}"


def build_email_content(
    briefing: NewsBriefing,
    user: User,
    *,
    base_url: str,
    item_limit: int = _MAX_ITEMS_IN_MAIL,
) -> EmailContent:
    """组装邮件主题、纯文本与 HTML 正文。

    Args:
        briefing: 简报（Markdown 全文来自 `briefing.content`）。
        user: 收件用户（用于称呼）。
        base_url: 站点根地址，用于拼「查看简报」入口。
        item_limit: 邮件正文最多列几条（其余引导到页面看）。

    Returns:
        EmailContent。
    """
    items = parse_news_items(briefing.content)
    links = collect_links(briefing.content)
    url = _briefing_url(base_url, briefing.id)
    subject = _build_subject(briefing)

    shown = items[:item_limit]
    hidden = max(0, len(items) - len(shown))
    nickname = (user.nickname or "你").strip()

    # ---------------------------------------------------------- 纯文本
    text_lines: list[str] = [
        f"{nickname}，你好：",
        "",
        f"这是 {briefing.briefing_date} 的 AI 新闻简报，共 {briefing.news_count} 条，"
        f"按你关注的方向筛选。",
        "",
        "查看完整简报：",
        url,
        "",
        "———— 今日重点 ————",
        "",
    ]
    if shown:
        for index, item in enumerate(shown, start=1):
            text_lines.append(f"{index}. {item.title}")
            if item.summary:
                text_lines.append(f"   摘要：{_truncate(item.summary)}")
            if item.source:
                text_lines.append(f"   来源：{item.source}")
            if item.url:
                text_lines.append(f"   原文：{item.url}")
            text_lines.append("")
    else:
        text_lines.extend(["（本期简报未解析出条目，请打开页面查看完整内容）", ""])

    if hidden:
        text_lines.extend([f"另有 {hidden} 条新闻，请打开页面查看。", ""])

    if links:
        text_lines.append("———— 原始新闻链接 ————")
        text_lines.extend(links)
        text_lines.append("")

    text_lines.extend(
        [
            "————",
            "本邮件由「每日 AI 新闻助手」自动生成，新闻摘要来自 Agent 实时检索的真实来源。",
            "如需停止接收，可在页面首页关闭「邮件推送」开关。",
        ]
    )
    text = "\n".join(text_lines)

    # ---------------------------------------------------------- HTML
    html_parts: list[str] = [
        '<div style="margin:0;padding:24px 12px;background:#f5f6fa;'
        'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',\'PingFang SC\','
        '\'Microsoft YaHei\',sans-serif;color:#1f2937;">',
        '<div style="max-width:640px;margin:0 auto;background:#fff;border:1px solid #e5e7eb;'
        'border-radius:14px;overflow:hidden;">',
        # 头部
        '<div style="padding:22px 26px;background:linear-gradient(135deg,#6366f1,#4338ca);color:#fff;">',
        f'<div style="font-size:12px;opacity:.85;letter-spacing:.5px;">{escape(briefing.briefing_date.isoformat())} · 每日 AI 新闻简报</div>',
        f'<h1 style="margin:6px 0 0;font-size:19px;line-height:1.45;">{escape(briefing.title)}</h1>',
        f'<div style="margin-top:8px;font-size:13px;opacity:.9;">共 {briefing.news_count} 条 · 为 {escape(nickname)} 定制</div>',
        "</div>",
        # 入口按钮
        '<div style="padding:20px 26px 0;">',
        f'<a href="{escape(url)}" style="display:inline-block;padding:11px 22px;background:#4f46e5;'
        'color:#fff;text-decoration:none;border-radius:9px;font-size:14px;font-weight:600;">'
        "查看完整简报 →</a>",
        f'<div style="margin-top:8px;font-size:12px;color:#9ca3af;word-break:break-all;">{escape(url)}</div>',
        "</div>",
        # 今日重点
        '<div style="padding:18px 26px 4px;">',
        '<h2 style="margin:0 0 10px;font-size:15px;color:#111827;">今日重点</h2>',
    ]

    if shown:
        for index, item in enumerate(shown, start=1):
            html_parts.append(
                '<div style="padding:12px 0;border-top:1px solid #f0f1f5;">'
                f'<div style="font-size:14px;font-weight:600;line-height:1.5;">'
                f'<span style="color:#4f46e5;">{index}.</span> {escape(item.title)}</div>'
            )
            if item.summary:
                html_parts.append(
                    f'<div style="margin-top:6px;font-size:13px;line-height:1.75;color:#4b5563;">'
                    f"{escape(_truncate(item.summary))}</div>"
                )
            meta: list[str] = []
            if item.source:
                meta.append(escape(item.source))
            if item.published:
                meta.append(escape(item.published))
            if meta:
                html_parts.append(
                    f'<div style="margin-top:6px;font-size:12px;color:#9ca3af;">{" · ".join(meta)}</div>'
                )
            if item.url:
                html_parts.append(
                    f'<div style="margin-top:6px;font-size:12px;">'
                    f'<a href="{escape(item.url)}" style="color:#4f46e5;text-decoration:none;'
                    f'word-break:break-all;">阅读原文 ↗</a></div>'
                )
            html_parts.append("</div>")
    else:
        html_parts.append(
            '<p style="font-size:13px;color:#6b7280;">本期简报未解析出条目，请打开页面查看完整内容。</p>'
        )

    html_parts.append("</div>")

    if hidden:
        html_parts.append(
            f'<div style="padding:0 26px 12px;font-size:12.5px;color:#6b7280;">'
            f"另有 {hidden} 条新闻，请打开页面查看完整简报。</div>"
        )

    # 原始链接清单
    if links:
        html_parts.append('<div style="padding:6px 26px 20px;">')
        html_parts.append(
            '<h2 style="margin:0 0 8px;font-size:14px;color:#111827;">原始新闻链接</h2>'
            '<ul style="margin:0;padding-left:18px;font-size:12.5px;line-height:1.9;color:#4b5563;">'
        )
        for link in links:
            html_parts.append(
                f'<li style="word-break:break-all;"><a href="{escape(link)}" '
                f'style="color:#4f46e5;text-decoration:none;">{escape(link)}</a></li>'
            )
        html_parts.append("</ul></div>")

    html_parts.extend(
        [
            '<div style="padding:16px 26px 22px;border-top:1px solid #f0f1f5;background:#fafbfd;'
            'font-size:12px;color:#9ca3af;line-height:1.7;">',
            "本邮件由「每日 AI 新闻助手」自动生成：新闻由 Agent 实时检索，摘要来自真实来源，"
            "未获取到正文的条目会如实标注。",
            "<br />如需停止接收，可在页面首页关闭「邮件推送」开关。",
            "</div>",
            "</div></div>",
        ]
    )

    return EmailContent(
        subject=subject,
        text=text,
        html="".join(html_parts),
        items=items,
        links=links,
    )


def build_plain_preview(briefing: NewsBriefing) -> str:
    """把简报压成一段纯文本（日志/调试用）。"""
    return markdown_to_text(briefing.content)[:500]
