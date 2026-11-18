"""新闻抓取占位模块（工作区示例文件）。

后续阶段会接入真实的新闻检索 API，当前只保留接口形状。
"""

from __future__ import annotations

# TODO: 接入真实新闻检索 API（博查 / NewsAPI / Tavily）
# TODO: 增加按订阅偏好过滤的逻辑
# FIXME: 错误重试目前是写死的 3 次，应改为指数退避


def fetch_news(topic: str, limit: int = 10) -> list[dict]:
    """按话题拉取新闻列表。

    Args:
        topic: 话题关键词。
        limit: 返回条数上限。

    Returns:
        新闻条目列表（当前为空实现）。
    """
    return []
