"""RSS / Atom / RDF 解析（仅用标准库）。

为什么不用 feedparser：本项目对依赖很克制（第二阶段刻意不引第三方库），
而资讯站点的 Feed 结构高度同质，标准库 `xml.etree.ElementTree` +
少量命名空间归一化就能覆盖 RSS 2.0 / Atom / RDF 三种主流格式。
好处是零依赖、解析行为可控（尤其是「截断的 XML 要明确报错」这一点）。

三种格式的差异（代码里要吃掉的地方）：
| 格式 | 条目节点 | 标题 | 链接 | 时间 | 摘要 |
| --- | --- | --- | --- | --- | --- |
| RSS 2.0 | channel/item | title | link | pubDate(RFC822) | description |
| RDF/RSS1.0 | item（根下）| title | link | dc:date(ISO8601) | description |
| Atom | feed/entry | title | link[@rel=alternate][@href] | published/updated | summary/content |
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

from app.news.feeds import FeedSource

logger = logging.getLogger(__name__)

# 摘要长度上限：Feed 里的 description 有时是整篇文章，直接塞给模型会浪费上下文
SUMMARY_MAX_CHARS = 400

_TAG_RE = re.compile(r"<[^>]+>")
# 行内标签（<b>、<a> 等）直接删掉，不能补空格——中文里会变成「这是 摘要 内容」
_INLINE_TAG_RE = re.compile(
    r"</?(?:b|i|em|strong|span|a|code|u|s|small|sub|sup|mark|font|label|abbr|cite|q|time)\b[^>]*>",
    re.IGNORECASE,
)
# 块级标签与换行替换成空格，保留段落边界
_BREAK_TAG_RE = re.compile(
    r"</?(?:p|div|br|li|ul|ol|dl|dt|dd|h[1-6]|tr|td|th|section|article|blockquote|figure)\b[^>]*>",
    re.IGNORECASE,
)
_WS_RE = re.compile(r"[ \t\u00a0]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")

# 各家 CMS 塞在摘要末尾的固定尾巴，对简报毫无信息量，统一清掉
_BOILERPLATE_RE = re.compile(
    r"(The post .{1,160}? appeared first on .{1,80}?\.|Continue reading .{0,120}?(»|>>)|Read [Mm]ore\s*[»›]?|"
    r"点击(阅读全文|查看原文)|阅读原文\s*[»›]?)",
    re.IGNORECASE,
)


class FeedParseError(Exception):
    """Feed 内容无法解析（不是 XML、结构不认识、被截断等）。"""


@dataclass(slots=True)
class FeedItem:
    """Feed 里的一条新闻条目（只做结构归一化，不做筛选）。"""

    title: str
    link: str
    summary: str
    published_at: datetime | None
    published_raw: str
    source_id: str
    source_name: str
    categories: list[str] = field(default_factory=list)
    guid: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "title": self.title,
            "url": self.link,
            "summary": self.summary,
            "published_at": self.published_at.isoformat(timespec="minutes") if self.published_at else None,
            "published_raw": self.published_raw,
            "source": self.source_name,
            "source_id": self.source_id,
            "categories": self.categories,
        }


# ---------------------------------------------------------------- 文本清洗


def clean_html(raw: str, *, max_chars: int | None = None) -> str:
    """把 Feed 里的 HTML 片段转成纯文本。

    只做「去标签 + 反转义 + 压空白」三步——资讯摘要通常是简单的 `<p>` 结构，
    上一整套 HTML 解析器收益有限，反而会把 `<` 之后的内容当标签吃掉。
    """
    if not raw:
        return ""
    text = _BREAK_TAG_RE.sub(" ", raw)
    text = _INLINE_TAG_RE.sub("", text)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = _BLANK_LINES_RE.sub("\n\n", text).strip()
    text = _BOILERPLATE_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    if max_chars and len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    return text


def parse_datetime(raw: str) -> datetime | None:
    """解析 Feed 里出现的时间字符串，统一成「本地时间、naive datetime」。

    为什么要转本地时间：项目全链路用 naive 本地时间（见 models/base.local_now），
    而 Feed 普遍给 UTC（`+0000` / `Z`）。不转换会导致「今天上午的新闻」被判成昨天，
    发布时间过滤直接失效。
    """
    text = (raw or "").strip()
    if not text:
        return None

    # RFC 822/2822：Wed, 18 Nov 2026 09:41:55 +0000 / GMT
    try:
        parsed = parsedate_to_datetime(text)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed.replace(microsecond=0)
    except (TypeError, ValueError):
        pass

    # ISO 8601：2026-11-18T09:41:55Z / +08:00 / 2026-11-18 09:41:55
    candidate = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed.replace(microsecond=0)
    except ValueError:
        pass

    # 兜底：只认日期（2026-11-18 / 2026/11/18）
    match = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text)
    if match:
        try:
            return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------- 元素遍历


def _localname(tag: str) -> str:
    """去掉命名空间，返回小写标签名（`{http://www.w3.org/2005/Atom}entry` → `entry`）。"""
    if "}" in tag:
        tag = tag.rsplit("}", 1)[1]
    return tag.lower()


def _find_text(element: ElementTree.Element, names: tuple[str, ...]) -> str:
    """按候选标签名找第一个非空文本。"""
    for child in element:
        if _localname(child.tag) in names:
            if child.text and child.text.strip():
                return child.text.strip()
            # 有些源把文本放在 CDATA / 子节点里，兜底取全部迭代文本
            joined = "".join(child.itertext()).strip()
            if joined:
                return joined
    return ""


def _atom_link(entry: ElementTree.Element) -> str:
    """Atom 的 link 是属性而不是文本，需要按 rel 优先取 alternate。"""
    fallback = ""
    for child in entry:
        if _localname(child.tag) != "link":
            continue
        href = (child.get("href") or "").strip()
        if not href:
            continue
        rel = (child.get("rel") or "alternate").lower()
        if rel == "alternate":
            return href
        fallback = fallback or href
    return fallback


def _entry_link(entry: ElementTree.Element, *, is_atom: bool) -> str:
    if is_atom:
        return _atom_link(entry)
    link = _find_text(entry, ("link",))
    if link:
        return link
    # 少数源把链接放在 guid 里且 isPermaLink="true"
    for child in entry:
        if _localname(child.tag) == "guid" and (child.get("isPermaLink") or "").lower() == "true":
            return (child.text or "").strip()
    return ""


def _entry_summary(entry: ElementTree.Element, *, is_atom: bool) -> str:
    names = ("summary", "content", "encoded") if is_atom else ("description", "encoded", "summary")
    raw = _find_text(entry, names)
    return clean_html(raw, max_chars=SUMMARY_MAX_CHARS)


# ---------------------------------------------------------------- 主入口


def parse_feed(xml_text: str, source: FeedSource, *, limit: int | None = None) -> list[FeedItem]:
    """解析一份 Feed 文本，返回归一化后的条目列表。

    Args:
        xml_text: HTTP 响应体（已解码为 str）。
        source: 源元信息，用于给每条新闻打上来源。
        limit: 最多返回多少条（防止超大源）。

    Returns:
        FeedItem 列表，顺序与 Feed 中一致。

    Raises:
        FeedParseError: 不是可解析的 XML，或结构无法识别。
    """
    head = (xml_text or "")[:400].lstrip().lower()
    if not head:
        raise FeedParseError("响应内容为空")
    if head.startswith("<!doctype html") or head.startswith("<html"):
        # 典型场景：源被反爬拦截、或 RSS 地址已失效重定向到网页
        raise FeedParseError("返回的是 HTML 页面而不是 RSS/Atom，可能是源地址失效或被反爬拦截")

    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise FeedParseError(f"XML 解析失败：{exc}") from exc

    root_name = _localname(root.tag)
    entries: list[ElementTree.Element] = []
    is_atom = root_name == "feed"

    if is_atom:
        entries = [child for child in root if _localname(child.tag) == "entry"]
    elif root_name == "rss":
        channels = [child for child in root if _localname(child.tag) == "channel"]
        for channel in channels:
            entries.extend(child for child in channel if _localname(child.tag) == "item")
    else:
        # RDF (RSS 1.0)：item 直接挂在根节点下
        entries = [child for child in root if _localname(child.tag) == "item"]

    if not entries:
        raise FeedParseError(f"未在 Feed 中找到任何条目（根节点 {root_name}）")

    items: list[FeedItem] = []
    for entry in entries:
        if limit is not None and len(items) >= limit:
            break

        title = clean_html(_find_text(entry, ("title",)))
        link = _entry_link(entry, is_atom=is_atom)
        if not title or not link:
            continue

        date_raw = _find_text(entry, ("pubdate", "published", "updated", "date", "created", "modified"))
        categories = [
            html.unescape((child.text or "").strip())
            for child in entry
            if _localname(child.tag) in ("category", "subject") and (child.text or "").strip()
        ]

        items.append(
            FeedItem(
                title=title,
                link=link.strip(),
                summary=_entry_summary(entry, is_atom=is_atom),
                published_at=parse_datetime(date_raw),
                published_raw=date_raw,
                source_id=source.id,
                source_name=source.name,
                categories=categories[:6],
                guid=_find_text(entry, ("guid", "id")),
            )
        )

    return items
