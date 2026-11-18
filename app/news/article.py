"""文章正文抽取（不依赖 BeautifulSoup）。

目标很朴素：给一个新闻详情页，尽可能拿到「标题 / 来源 / 发布时间 / 正文」四要素，
拿不到就如实标注拿不到——**绝不拼接、绝不脑补**。

为什么不用正文提取库（readability / trafilatura）：
项目对依赖敏感，而这些库体积大、规则黑盒（模型很难解释「为什么这段被丢掉」）。
这里用「去噪 → 取段落 → 密度兜底」三层策略，规则全部可读、可测、可调：

1. 去噪：删掉 script/style/nav/footer/aside/form/iframe 等整块非正文节点；
2. 取段落：抓 `<p>` / `<h2>` / `<h3>`，按长度与黑名单词过滤导航行；
3. 兜底：段落总量不足时退化为整页可见文本（标记 excerpt），
   仍然不足则只给 meta description（标记 summary）。

三种内容形态会明确写进返回值 `content_kind`，让模型知道
「这是全文 / 这是页面摘录 / 这只是站点摘要」，从而在写简报时如实表述。
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

from app.news.rss import clean_html, parse_datetime

logger = logging.getLogger(__name__)

_META_RE = re.compile(r"<meta\s+([^>]+?)/?>", re.IGNORECASE | re.DOTALL)
_ATTR_RE = re.compile(r"([\w:-]+)\s*=\s*(\"([^\"]*)\"|'([^']*)'|([^\s\"'>]+))", re.DOTALL)
_JSONLD_RE = re.compile(
    r"<script[^>]+type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
_TIME_RE = re.compile(r"<time[^>]*datetime\s*=\s*[\"']([^\"']+)[\"'][^>]*>", re.IGNORECASE)
# 没有 <time> 也没有 meta 时的兜底：带 date/time/publish 类名的元素里的文本
_DATE_ELEMENT_RE = re.compile(
    r"<(?:span|div|p|em|time)\b[^>]*(?:class|id)\s*=\s*[\"'][^\"']*(?:date|time|publish)[^\"']*[\"'][^>]*>"
    r"([^<]{6,40})<",
    re.IGNORECASE,
)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
_PARA_RE = re.compile(r"<(p|h2|h3)\b[^>]*>(.*?)</\1>", re.IGNORECASE | re.DOTALL)
_TAG_STRIP_RE = re.compile(r"<[^>]+>")

# 整块删除的非正文节点（DOTALL + 非贪婪；这些标签内部一般不会再嵌同名标签）
_NOISE_BLOCKS: tuple[str, ...] = (
    r"<script\b.*?</script>",
    r"<style\b.*?</style>",
    r"<noscript\b.*?</noscript>",
    r"<svg\b.*?</svg>",
    r"<iframe\b.*?</iframe>",
    r"<form\b.*?</form>",
    r"<nav\b.*?</nav>",
    r"<header\b.*?</header>",
    r"<footer\b.*?</footer>",
    r"<aside\b.*?</aside>",
    r"<!--.*?-->",
)
_NOISE_RE = re.compile("|".join(_NOISE_BLOCKS), re.IGNORECASE | re.DOTALL)

# 导航/版权/互动类行，出现在正文段落里会污染摘要
_JUNK_HINTS: tuple[str, ...] = (
    "责任编辑",
    "本文来自",
    "扫码关注",
    "关注公众号",
    "点击关注",
    "分享到",
    "转载请注明",
    "版权所有",
    "免责声明",
    "登录后",
    "注册登录",
    "相关阅读",
    "上一篇",
    "下一篇",
    "热门推荐",
    "广告",
    "订阅我们",
    "阅读原文",
    "返回顶部",
)

# 正文最短长度：低于这个值认为「只抓到了页面骨架」，走兜底策略。
# 取 120 是因为资讯站短讯正文常只有两三句话；再低就容易被导航文字骗过。
_MIN_BODY_CHARS = 120
# 单段最短长度：低于这个值的 <p> 多为按钮、时间戳、图注
_MIN_PARAGRAPH_CHARS = 12


@dataclass(slots=True)
class Article:
    """一篇文章的抽取结果。"""

    url: str
    title: str = ""
    site_name: str = ""
    author: str = ""
    published_at: datetime | None = None
    published_raw: str = ""
    content: str = ""
    content_kind: str = "none"  # full / excerpt / summary / none
    word_count: int = 0
    description: str = ""
    truncated: bool = False
    warnings: list[str] = field(default_factory=list)


def _parse_meta(tags_html: str) -> dict[str, str]:
    """把整页的 <meta> 收集成 {小写键: 内容}。"""
    metas: dict[str, str] = {}
    for match in _META_RE.finditer(tags_html):
        attrs = match.group(1)
        pairs = _ATTR_RE.findall(attrs)
        key = ""
        value = ""
        for name, _, dq, sq, bare in pairs:
            lowered = name.lower()
            if lowered in ("property", "name", "itemprop"):
                key = (dq or sq or bare).strip().lower()
            elif lowered == "content":
                value = html_lib.unescape(dq or sq or bare or "")
        if key and value and key not in metas:
            metas[key] = value.strip()
    return metas


def _parse_jsonld(html_text: str) -> dict[str, str]:
    """从 JSON-LD 里挖 headline / datePublished / author。"""
    found: dict[str, str] = {}
    for match in _JSONLD_RE.finditer(html_text):
        payload = match.group(1).strip()
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            # 有些站点一个 script 里塞多个对象，直接正则兜底
            for key in ("headline", "datePublished"):
                hit = re.search(rf'"{key}"\s*:\s*"([^"]+)"', payload)
                if hit and key not in found:
                    found[key] = html_lib.unescape(hit.group(1))
            continue

        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                for key in ("headline", "datePublished"):
                    value = node.get(key)
                    if isinstance(value, str) and value.strip() and key not in found:
                        found[key] = value.strip()
                author = node.get("author")
                if "author" not in found:
                    if isinstance(author, dict) and isinstance(author.get("name"), str):
                        found["author"] = author["name"].strip()
                    elif isinstance(author, list) and author:
                        first = author[0]
                        if isinstance(first, dict) and isinstance(first.get("name"), str):
                            found["author"] = first["name"].strip()
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
    return found


def _looks_like_junk(line: str) -> bool:
    return any(hint in line for hint in _JUNK_HINTS)


def _strip_site_suffix(title: str, site_name: str) -> str:
    """去掉标题里的站点后缀（`xxx - 量子位` / `xxx_IT之家` / `xxx | 极客公园`）。"""
    if not title:
        return ""
    if site_name:
        for sep in (" - ", " — ", " | ", "_", "－"):
            candidate = f"{sep}{site_name}"
            if title.endswith(candidate):
                return title[: -len(candidate)].strip()
    # 通用兜底：最后一段很短且像站名时截掉
    for sep in (" | ", " - ", " — ", "_"):
        head, _, tail = title.rpartition(sep)
        if head and 0 < len(tail) <= 12:
            return head.strip()
    return title.strip()


def _collect_paragraphs(body_html: str) -> tuple[list[str], list[str]]:
    """抽取正文段落与标题行。

    Returns:
        (段落列表, 小标题列表)。段落已去重、去导航行。
    """
    paragraphs: list[str] = []
    headings: list[str] = []
    seen: set[str] = set()

    for match in _PARA_RE.finditer(body_html):
        tag = match.group(1).lower()
        raw = match.group(2)
        text = clean_html(raw)
        text = _TAG_STRIP_RE.sub("", text).strip()
        if not text:
            continue
        # 图片说明段落常被包在 pgc-img 里，含字但很短，直接跳过
        if tag == "p" and len(text) < _MIN_PARAGRAPH_CHARS:
            continue
        if _looks_like_junk(text):
            continue
        key = text[:60]
        if key in seen:
            continue
        seen.add(key)
        if tag in ("h2", "h3"):
            headings.append(text)
            paragraphs.append(f"## {text}")
        else:
            paragraphs.append(text)

    return paragraphs, headings


def _plain_text(body_html: str) -> str:
    """兜底：把整页 HTML 变成带换行的纯文本。"""
    text = _TAG_STRIP_RE.sub("\n", body_html)
    text = html_lib.unescape(text)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _count_words(text: str) -> int:
    """中英混排的字数统计（汉字按字、英文按词）。"""
    chinese = len(re.findall(r"[\u4e00-\u9fff]", text))
    english = len(re.findall(r"[A-Za-z0-9]+", text))
    return chinese + english


def extract_article(html_text: str, url: str, *, max_chars: int = 6000) -> Article:
    """从 HTML 中抽取文章要素。

    Args:
        html_text: 页面源码。
        url: 页面地址（写进结果，便于溯源）。
        max_chars: 正文最大字符数，超出截断。

    Returns:
        Article。`content_kind` 明确区分 full / excerpt / summary，
        三者都拿不到时 content 为空字符串（由调用方判定失败）。
    """
    article = Article(url=url)
    if not html_text or not html_text.strip():
        article.warnings.append("页面内容为空")
        return article

    metas = _parse_meta(html_text)
    jsonld = _parse_jsonld(html_text)

    # ---- 标题 ----
    site_name = (metas.get("og:site_name") or metas.get("application-name") or "").strip()
    title = (
        metas.get("og:title")
        or metas.get("twitter:title")
        or jsonld.get("headline")
        or (_H1_RE.search(html_text).group(1) if _H1_RE.search(html_text) else "")
        or (_TITLE_RE.search(html_text).group(1) if _TITLE_RE.search(html_text) else "")
    )
    title = clean_html(_TAG_STRIP_RE.sub("", title or ""))
    article.title = _strip_site_suffix(title, site_name)
    article.site_name = site_name

    # ---- 作者 ----
    article.author = (
        (metas.get("article:author") or metas.get("author") or jsonld.get("author") or "").strip()
    )[:40]

    # ---- 发布时间 ----
    published_raw = (
        metas.get("article:published_time")
        or metas.get("og:article:published_time")
        or metas.get("pubdate")
        or metas.get("publishdate")
        or metas.get("date")
        or metas.get("datepublished")
        or jsonld.get("datePublished")
        or ""
    )
    if not published_raw:
        time_hit = _TIME_RE.search(html_text)
        if time_hit:
            published_raw = time_hit.group(1)
    if not published_raw:
        # 只在页面头部（前 12000 字符）找，避免把正文里提到的历史日期当成发布时间
        for candidate in _DATE_ELEMENT_RE.findall(html_text[:12000]):
            text = html_lib.unescape(candidate).strip()
            if re.search(r"20\d{2}[-/年]\d{1,2}[-/月]\d{1,2}", text) and parse_datetime(text):
                published_raw = text
                break
    article.published_raw = published_raw
    if published_raw:
        parsed = parse_datetime(published_raw)
        if parsed is None:
            article.warnings.append(f"发布时间格式无法解析：{published_raw[:40]}")
        article.published_at = parsed

    # ---- 摘要（meta description 常被用作兜底正文）----
    article.description = clean_html(
        metas.get("og:description") or metas.get("description") or metas.get("twitter:description") or "",
        max_chars=500,
    )

    # ---- 正文 ----
    body_html = _NOISE_RE.sub(" ", html_text)
    paragraphs, _headings = _collect_paragraphs(body_html)
    body = "\n\n".join(paragraphs)
    body_chars = len(body.strip())

    if body_chars >= _MIN_BODY_CHARS:
        # 结构化段落拿到的就是正文，长度差异交给 truncated / word_count 表达，
        # 不用「字数多少」去猜它是不是全文（短讯也是全文）。
        article.content_kind = "full"
    else:
        # 兜底 1：整页可见文本（部分站点正文不在 <p> 里）
        fallback = _plain_text(body_html)
        if len(fallback) >= _MIN_BODY_CHARS:
            body = fallback
            article.content_kind = "excerpt"
            article.warnings.append("未找到结构化正文段落，已退化为整页文本")
        # 兜底 2：站点摘要（只能当作「摘要」用，不能当正文）
        elif article.description and len(article.description) >= 30:
            body = article.description
            article.content_kind = "summary"
            article.warnings.append("未能获取正文，仅返回站点摘要")
        else:
            article.content_kind = "none"
            article.warnings.append("未能从页面中提取到正文或摘要")

    if article.content_kind != "none" and len(body) > max_chars:
        body = body[:max_chars].rstrip() + "…"
        article.truncated = True

    article.content = body.strip()
    article.word_count = _count_words(article.content)
    return article
