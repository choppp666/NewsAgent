"""新闻去重。

同一条新闻在真实世界里通常以三种形式重复出现，必须分别处理：

| 重复形态 | 例子 | 判定手段 |
| --- | --- | --- |
| 同 URL 不同参数 | `?utm_source=rss`、`#comments`、`http` vs `https` | URL 规范化后字符串相等 |
| 同链接不同写法 | 结尾多了 `/`、`www.` 前缀、大小写不同 | 同上 |
| 不同链接同内容 | 转载、聚合站镜像、同一稿多端发布 | 标题归一化后相似度 |

第四种（中英文对同一事件的不同报道）**不做机械去重**：
两篇标题字面完全不同，硬做相似度会误杀。这种「同事件不同报道」的合并
交给大模型在写简报时判断——工具只负责把明显的重复挡掉，并如实统计挡了多少。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# 这些查询参数只影响渠道统计，不影响内容本身
_TRACKING_PREFIXES: tuple[str, ...] = ("utm_", "spm", "from", "ref", "referrer", "share_", "scene", "src")
_TRACKING_KEYS: frozenset[str] = frozenset({"fbclid", "gclid", "igshid", "mc_cid", "mc_eid"})

_PUNCT_RE = re.compile(r"[\s\-_·|/\\:：,，.。!！?？\"'“”‘’()（）【】\[\]<>《》~`@#$%^&*+=]+")
_DECORATION_RE = re.compile(r"^\s*(【[^】]{0,8}】|\[[^\]]{0,8}\]|独家|快讯|重磅|突发|首发)\s*")


def canonical_url(url: str) -> str:
    """把 URL 归一化成用于比较的键。

    处理内容：协议与小写化、去 www、去 fragment、去追踪参数、参数排序、去尾斜杠。
    注意结果**只用于比较**，对外展示与写进简报的仍然是原始链接。
    """
    text = (url or "").strip()
    if not text:
        return ""

    parsed = urlparse(text)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if parsed.port and parsed.port not in (80, 443):
        host = f"{host}:{parsed.port}"

    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=False)
        if key.lower() not in _TRACKING_KEYS and not key.lower().startswith(_TRACKING_PREFIXES)
    ]
    query = urlencode(sorted(query_pairs))

    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    return urlunparse(("https", host, path, "", query, ""))


def title_key(title: str) -> str:
    """标题归一化：去装饰性前缀、去标点空白、统一大小写。"""
    text = _DECORATION_RE.sub("", (title or "").strip())
    text = text.casefold()
    return _PUNCT_RE.sub("", text)


def title_similarity(left: str, right: str) -> float:
    """两个标题的相似度（0~1）。

    先用「字符二元组重合率」做廉价预筛，再对通过预筛的候选算编辑相似度——
    几百条新闻两两比较时，这一步能把开销从 O(n²) 的编辑距离降到几乎可忽略。
    """
    a, b = title_key(left), title_key(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0

    # 长度差太大直接判否，避免「短标题被长标题包含」造成误杀
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(shorter) / len(longer) < 0.6:
        return 0.0

    grams_a = {shorter[i : i + 2] for i in range(len(shorter) - 1)}
    grams_b = {longer[i : i + 2] for i in range(len(longer) - 1)}
    if not grams_a or not grams_b:
        return 0.0
    overlap = len(grams_a & grams_b) / len(grams_a | grams_b)
    if overlap < 0.55:
        return 0.0

    return SequenceMatcher(None, a, b).ratio()


@dataclass(slots=True)
class DuplicateHit:
    """一次去重命中的记录（用于向模型解释「哪些被挡掉了」）。"""

    title: str
    url: str
    reason: str  # same_url / same_title / near_title
    duplicate_of: str
    source: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "title": self.title,
            "url": self.url,
            "reason": self.reason,
            "duplicate_of": self.duplicate_of,
            "source": self.source,
        }


@dataclass(slots=True)
class _Seen:
    url_key: str
    title: str
    source: str


@dataclass(slots=True)
class NewsDeduplicator:
    """有状态的去重器：一次运行内的所有搜索结果共用一份「已见」集合。

    为什么要有状态：Agent 往往会分多次搜索（先搜 topic，再搜同义说法），
    如果每次搜索都重新去重，同一条新闻会在不同轮次里反复出现，
    模型就会误以为「今天有很多相关新闻」。
    """

    threshold: float = 0.9
    _by_url: dict[str, _Seen] = field(default_factory=dict)
    _by_title: list[_Seen] = field(default_factory=list)

    def check(self, title: str, url: str, *, source: str = "") -> DuplicateHit | None:
        """判断是否重复；不重复则登记为已见。"""
        url_key = canonical_url(url)
        if not url_key:
            return None

        if url_key in self._by_url:
            seen = self._by_url[url_key]
            return DuplicateHit(title, url, "same_url", seen.title, source)

        for seen in self._by_title:
            if title_similarity(title, seen.title) >= self.threshold:
                return DuplicateHit(title, url, "near_title", seen.title, source)

        record = _Seen(url_key=url_key, title=title, source=source)
        self._by_url[url_key] = record
        self._by_title.append(record)
        return None

    def remember(self, title: str, url: str, *, source: str = "") -> None:
        """只登记不判断（用于把「已抓过正文」的条目计入去重池）。"""
        url_key = canonical_url(url)
        if not url_key or url_key in self._by_url:
            return
        record = _Seen(url_key=url_key, title=title, source=source)
        self._by_url[url_key] = record
        self._by_title.append(record)

    @property
    def size(self) -> int:
        return len(self._by_url)
