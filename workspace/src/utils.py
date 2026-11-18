"""通用小工具（工作区示例文件，不含 Agent / TODO 关键字）。"""

from __future__ import annotations


def chunk(items: list, size: int) -> list[list]:
    """把列表按固定大小切块。"""
    return [items[i : i + size] for i in range(0, len(items), size)]


def short(text: str, limit: int = 60) -> str:
    """截断长文本并加省略号。"""
    return text if len(text) <= limit else text[: limit - 1] + "…"
