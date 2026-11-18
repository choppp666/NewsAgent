"""极简 Markdown 渲染（服务端）。

为什么不用第三方库：
1. 简报的 Markdown 格式是我们自己定的（`briefing_service.build_briefing_task`
   里写死了章节结构），只用到标题、列表、引用、粗体、行内代码、链接；
2. 邮件里塞一份不完整的 HTML 比塞纯文本更糟，所以渲染器宁可小而可读；
3. **安全**：正文来自大模型，先 `html.escape` 再插标签，杜绝 `<script>` 注入。
   第三方库多数默认允许 raw HTML，反而要额外配置 sanitize。

前端 `app.js` 里有一份等价的 JS 实现（页面要求零依赖、可离线打开），
两边的语法支持范围必须保持一致，改动时请同步。
"""

from __future__ import annotations

import re
from html import escape

__all__ = ["markdown_to_html", "markdown_to_text", "strip_markdown"]

_HEADING = re.compile(r"^(#{1,3})\s+(.*)$")
_HR = re.compile(r"^(-{3,}|\*{3,})$")
_ORDERED = re.compile(r"^(\d+)\.\s+(.*)$")
_UNORDERED = re.compile(r"^[-*+]\s+(.*)$")
_INLINE_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_BARE_LINK = re.compile(r"(^|[\s\u3000（(【\[：:，,、；;])(https?://[^\s<>\"'）)】\]，。；、]+)")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")


def _render_inline(text: str) -> str:
    """行内语法：**粗体**、`代码`、[文字](链接)、裸链接。

    顺序很重要：先转义 → 先把 Markdown 链接换成占位符 → 处理其它行内语法
    → 最后处理裸链接 → 还原链接占位符。否则裸链接规则会把已经生成的
    `<a href="...">` 里的 URL 再包一层，产出嵌套标签。
    """
    placeholders: list[str] = []
    out = escape(text, quote=False)

    def _stash(match: re.Match[str]) -> str:
        label, url = match.group(1), match.group(2)
        placeholders.append(
            f'<a href="{escape(url)}" target="_blank" rel="noopener noreferrer">{label}</a>'
        )
        return f"\x01{len(placeholders) - 1}\x01"

    out = _MD_LINK.sub(_stash, out)
    out = _INLINE_CODE.sub(r"<code>\1</code>", out)
    out = _BOLD.sub(r"<strong>\1</strong>", out)
    out = _BARE_LINK.sub(
        lambda m: f'{m.group(1)}<a href="{m.group(2)}" target="_blank" rel="noopener noreferrer">{m.group(2)}</a>',
        out,
    )
    return re.sub(r"\x01(\d+)\x01", lambda m: placeholders[int(m.group(1))], out)


def markdown_to_html(markdown: str) -> str:
    """把简报 Markdown 转成安全的 HTML 片段（不含 `<html>` 外壳）。"""
    lines = (markdown or "").replace("\r\n", "\n").split("\n")
    html: list[str] = []
    list_tag: str | None = None
    quote_lines: list[str] = []

    def close_list() -> None:
        nonlocal list_tag
        if list_tag:
            html.append(f"</{list_tag}>")
            list_tag = None

    def flush_quote() -> None:
        if quote_lines:
            inner = "<br />".join(_render_inline(line) for line in quote_lines)
            html.append(f"<blockquote><p>{inner}</p></blockquote>")
            quote_lines.clear()

    for raw_line in lines:
        line = raw_line.rstrip()

        if not line.strip():
            close_list()
            flush_quote()
            continue

        if line.startswith("> "):
            close_list()
            quote_lines.append(line[2:])
            continue
        flush_quote()

        heading = _HEADING.match(line)
        if heading:
            close_list()
            level = len(heading.group(1))
            html.append(f"<h{level}>{_render_inline(heading.group(2))}</h{level}>")
            continue

        if _HR.match(line.strip()):
            close_list()
            html.append("<hr />")
            continue

        ordered = _ORDERED.match(line)
        unordered = _UNORDERED.match(line)
        if ordered or unordered:
            tag = "ol" if ordered else "ul"
            if list_tag != tag:
                close_list()
                html.append(f"<{tag}>")
                list_tag = tag
            body = ordered.group(2) if ordered else unordered.group(1)
            html.append(f"<li>{_render_inline(body)}</li>")
            continue

        close_list()
        html.append(f"<p>{_render_inline(line)}</p>")

    close_list()
    flush_quote()
    return "\n".join(html)


def strip_markdown(text: str) -> str:
    """去掉行内 Markdown 标记，保留可读文字（用于纯文本邮件与摘要）。"""
    out = _IMAGE.sub("", text or "")
    out = _MD_LINK.sub(r"\1（\2）", out)
    out = _INLINE_CODE.sub(r"\1", out)
    out = _BOLD.sub(r"\1", out)
    out = re.sub(r"^\s*#{1,6}\s*", "", out, flags=re.MULTILINE)
    out = re.sub(r"^\s*[-*+]\s+", "· ", out, flags=re.MULTILINE)
    return out.strip()


def markdown_to_text(markdown: str) -> str:
    """Markdown → 纯文本（邮件 text/plain 分部）。

    纯文本版不是「多余的一步」：很多邮件客户端默认不渲染 HTML，
    只发 HTML 会导致部分用户看到空白；同时纯文本也是反垃圾评分的一个加分项。
    """
    lines: list[str] = []
    for raw_line in (markdown or "").replace("\r\n", "\n").split("\n"):
        line = raw_line.rstrip()
        if not line.strip():
            lines.append("")
            continue
        heading = _HEADING.match(line)
        if heading:
            title = strip_markdown(heading.group(2))
            lines.append("")
            lines.append(title)
            lines.append("─" * min(48, max(8, len(title) * 2)))
            continue
        if line.startswith("> "):
            lines.append("  " + strip_markdown(line[2:]))
            continue
        ordered = _ORDERED.match(line)
        if ordered:
            lines.append(f"{ordered.group(1)}. {strip_markdown(ordered.group(2))}")
            continue
        unordered = _UNORDERED.match(line)
        if unordered:
            lines.append(f"· {strip_markdown(unordered.group(1))}")
            continue
        if _HR.match(line.strip()):
            lines.append("-" * 48)
            continue
        lines.append(strip_markdown(line))

    # 折叠连续空行，避免纯文本邮件出现大段空白
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
