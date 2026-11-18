"""简报结构校验与解析。

大模型写 Markdown 有两个高频缺陷，必须在入库前挡住：
1. **结构缺失**：漏掉「行业动态总结」「今日值得关注」，或者干脆没写标题；
2. **无源之谈**：摘要写得头头是道，但条目里没有来源、没有链接，
   甚至链接是拼出来的（把域名和日期拼在一起）。

本模块只做「检查 + 提取」，不改写内容。这样职责清晰：
模型负责写，工具负责验，接口负责报告——出问题时能一眼看出是哪一环的问题。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 三个必需章节：(内部键, 展示名, 命中用的候选关键词)
REQUIRED_SECTIONS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("highlights", "今日重点新闻", ("今日重点新闻", "重点新闻", "今日要闻")),
    ("trends", "行业动态总结", ("行业动态总结", "行业动态", "趋势总结", "行业总结")),
    ("watch", "今日值得关注", ("今日值得关注", "值得关注", "延伸阅读")),
)

# 简报头部的三个字段（允许写成「- 简报日期：xxx」或表格）
HEADER_FIELDS: tuple[tuple[str, str], ...] = (
    ("date", "简报日期"),
    ("focus", "关注方向"),
    ("count", "新闻数量"),
)

_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\((https?://[^)\s]+)\)")
_BARE_URL_RE = re.compile(r"(?<!\()(https?://[^\s<>\"'）)】\]]+)")
_H1_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
_H3_RE = re.compile(r"^###\s+(.+)$", re.MULTILINE)
_DATE_RE = re.compile(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})")

# 最少字数：低于这个值基本可以断定模型输出被截断
MIN_CONTENT_CHARS = 200


@dataclass(slots=True)
class BriefingReport:
    """一次简报内容检查的结果。"""

    ok: bool = False
    title: str = ""
    briefing_date: str | None = None
    declared_count: int | None = None
    news_count: int = 0
    sections: dict[str, bool] = field(default_factory=dict)
    missing_sections: list[str] = field(default_factory=list)
    missing_headers: list[str] = field(default_factory=list)
    items_without_link: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    char_count: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "title": self.title,
            "briefing_date": self.briefing_date,
            "news_count": self.news_count,
            "declared_count": self.declared_count,
            "sections": self.sections,
            "missing_sections": self.missing_sections,
            "missing_headers": self.missing_headers,
            "items_without_link": self.items_without_link,
            "link_count": len(self.links),
            "issues": self.issues,
            "char_count": self.char_count,
        }

    def to_error_message(self) -> str:
        """把问题清单拼成给模型看的修复指引。"""
        parts: list[str] = []
        if self.missing_sections:
            parts.append(f"缺少章节：{'、'.join(self.missing_sections)}")
        if self.missing_headers:
            parts.append(f"缺少头部信息：{'、'.join(self.missing_headers)}")
        if self.items_without_link:
            preview = "；".join(self.items_without_link[:3])
            parts.append(f"以下新闻条目缺少来源链接：{preview}")
        if self.news_count == 0:
            parts.append("未解析到任何新闻条目（请用「### 标题」逐条列出重点新闻）")
        if self.char_count < MIN_CONTENT_CHARS:
            parts.append(f"内容过短（{self.char_count} 字），可能被截断")
        return "；".join(parts) or "简报结构不完整"


def extract_links(text: str) -> list[str]:
    """提取正文中的所有 http(s) 链接（Markdown 链接与裸链接都算）。"""
    links: list[str] = []
    seen: set[str] = set()
    for match in _MD_LINK_RE.finditer(text or ""):
        url = match.group(2).strip()
        if url not in seen:
            seen.add(url)
            links.append(url)
    for match in _BARE_URL_RE.finditer(text or ""):
        url = match.group(1).strip().rstrip(".,;，。；")
        if url not in seen:
            seen.add(url)
            links.append(url)
    return links


def _section_slices(content: str) -> dict[str, str]:
    """按二级标题把正文切成若干块，键为标题文本。"""
    slices: dict[str, str] = {}
    matches = list(re.finditer(r"^##\s+(.+)$", content, re.MULTILINE))
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        slices[match.group(1).strip()] = content[start:end]
    return slices


def _find_section(slices: dict[str, str], keywords: tuple[str, ...]) -> str | None:
    """在切好的章节里找命中关键词的那一块（容忍「一、今日重点新闻」这种写法）。"""
    for heading, body in slices.items():
        if any(keyword in heading for keyword in keywords):
            return body
    return None


def _count_items(section_body: str) -> tuple[int, list[str]]:
    """统计重点新闻条目，并返回「缺链接的条目标题」列表。"""
    headings = _H3_RE.findall(section_body)
    blocks: list[tuple[str, str]] = []

    if headings:
        positions = [match.start() for match in _H3_RE.finditer(section_body)]
        positions.append(len(section_body))
        for index, title in enumerate(headings):
            blocks.append((title.strip(), section_body[positions[index] : positions[index + 1]]))
    else:
        # 退而求其次：编号列表或加粗标题
        pattern = re.compile(r"^\s*(?:\d+[.、)]|[-*])\s+\*{0,2}(.{4,80}?)\*{0,2}\s*$", re.MULTILINE)
        matches = list(pattern.finditer(section_body))
        for index, match in enumerate(matches):
            start = match.start()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(section_body)
            blocks.append((match.group(1).strip(), section_body[start:end]))

    without_link = [title for title, block in blocks if not _MD_LINK_RE.search(block) and not _BARE_URL_RE.search(block)]
    return len(blocks), without_link


def parse_briefing(content: str) -> BriefingReport:
    """检查简报结构，提取标题 / 日期 / 条目数 / 链接。

    Args:
        content: Markdown 全文。

    Returns:
        BriefingReport；`ok` 表示「结构与溯源要求都满足」。
    """
    text = (content or "").strip()
    report = BriefingReport(char_count=len(text))

    if not text:
        report.issues.append("内容为空")
        return report

    h1 = _H1_RE.search(text)
    report.title = h1.group(1).strip() if h1 else ""
    if not report.title:
        report.issues.append("缺少一级标题（# 每日 AI 新闻简报）")

    date_match = _DATE_RE.search(text)
    if date_match:
        report.briefing_date = f"{date_match.group(1)}-{int(date_match.group(2)):02d}-{int(date_match.group(3)):02d}"

    slices = _section_slices(text)
    for key, label, keywords in REQUIRED_SECTIONS:
        body = _find_section(slices, keywords)
        report.sections[key] = body is not None
        if body is None:
            report.missing_sections.append(label)

    for _, label in HEADER_FIELDS:
        # 头部字段只要求关键字出现即可（各家模型写的格式差异太大）
        if label not in text and not (label == "关注方向" and ("关注领域" in text or "关注" in text)):
            report.missing_headers.append(label)

    count_match = re.search(r"(?:今日新闻数量|新闻数量|共)\s*[:：]?\s*(\d{1,3})\s*(?:条|篇)?", text)
    if count_match:
        report.declared_count = int(count_match.group(1))

    highlight_body = _find_section(slices, REQUIRED_SECTIONS[0][2]) or ""
    report.news_count, report.items_without_link = _count_items(highlight_body)

    report.links = extract_links(text)

    if not report.sections["highlights"]:
        report.issues.append("缺少「今日重点新闻」章节，无法定位新闻条目")
    if report.news_count == 0:
        report.issues.append("未解析到新闻条目：请用「### 标题」为每条重点新闻起小标题")
    if report.items_without_link:
        report.issues.append(f"{len(report.items_without_link)} 条新闻缺少原文链接")
    if report.missing_sections:
        report.issues.append(f"缺少章节：{'、'.join(report.missing_sections)}")
    if report.missing_headers:
        report.issues.append(f"缺少头部字段：{'、'.join(report.missing_headers)}")
    if report.char_count < MIN_CONTENT_CHARS:
        report.issues.append(f"内容过短（{report.char_count} 字），可能被截断")

    report.ok = not report.issues
    return report


def count_news_items(content: str) -> int:
    """只统计条目数（入库 news_count 用，校验失败时也能拿到一个值）。"""
    return parse_briefing(content).news_count
