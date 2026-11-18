"""Mock 模式下的「新闻简报」决策策略。

⚠️ 定位（必须说清楚）：这不是大模型，是**状态机 + 模板**。
它存在的唯一价值是：在没有 LLM_API_KEY 的环境里，把
「读偏好 → 搜索 → 读原文 → 写文件 → 入库」这条链路真正跑通，
并且产出**基于真实检索结果**的简报，从而验证工具、去重、校验、入库全都正确。

它与真正自主决策的区别也很明确：
- 它按固定的优先级挑下一步（偏好 → 搜索 → 补读 → 写作），不会「想到换个角度」；
- 摘要与「重要性分析」由模板从真实正文里截取/归纳，不做语义理解；
- 因此产出物会显式标注「离线策略生成」，不会被误当成模型能力。

所有内容都来自工具返回值，模板本身不造任何事实——这一点和真模型的要求完全一致。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

from app.news.dedupe import title_similarity

logger = logging.getLogger(__name__)

_MAX_FOCUS_TERMS = 3
_DEFAULT_MAX_ITEMS = 5

# 指代式开头的句子离开上文读不通（「That's what ... aims to find out.」），不能当事件描述
_ANAPHORIC_STARTERS = frozenset({"that", "this", "these", "those", "it", "they", "such"})
_CJK_ANAPHORA = ("这", "那", "它", "他", "她", "此", "该")


class Observation(Protocol):
    """已完成的一次工具调用（与 mock_client.Observation 结构一致，鸭子类型）。"""

    name: str
    arguments: dict[str, Any]
    ok: bool
    summary: str
    data: dict[str, Any]


@dataclass(slots=True)
class Plan:
    """下一步动作。"""

    kind: str  # "call" / "final"
    tool: str | None = None
    arguments: dict[str, Any] | None = None
    answer: str = ""


def _field(task: str, *labels: str) -> str:
    """从任务文本里取「- 标签：值」形式的一行。"""
    for label in labels:
        match = re.search(rf"{label}\s*[：:]\s*(.+)", task)
        if match:
            value = match.group(1).strip()
            if value and not value.startswith("（"):
                return value
    return ""


def _terms(raw: str) -> list[str]:
    return [part.strip() for part in re.split(r"[、,，;；/]", raw) if part.strip()]


def _briefing_date(task: str) -> str:
    match = re.search(r"生成\s*(20\d{2}-\d{2}-\d{2})", task) or re.search(
        r"简报日期\s*[：:]\s*(20\d{2}-\d{2}-\d{2})", task
    )
    return match.group(1) if match else date.today().isoformat()


class NewsPolicy:
    """按「已有观察」决定下一步的新闻简报策略。"""

    def __init__(
        self,
        *,
        max_searches: int = _MAX_FOCUS_TERMS,
        max_fetches: int = 2,
        max_items: int = _DEFAULT_MAX_ITEMS,
    ) -> None:
        self.max_searches = max_searches
        self.max_fetches = max_fetches
        self.max_items = max_items

    # ------------------------------------------------------------ 入口

    def decide(self, task: str, observations: list[Observation], available: set[str]) -> Plan:
        """返回下一步动作。"""
        user_id = self._user_id(task, observations)
        focus, excludes = self._focus(task, observations)
        searches = [obs for obs in observations if obs.name == "search_news"]
        fetches = [obs for obs in observations if obs.name == "fetch_article" and obs.ok]
        wrote = any(obs.name == "write_file" and obs.ok for obs in observations)
        saved = [obs for obs in observations if obs.name == "save_briefing"]

        # ① 先读真实偏好（如果任务里给了 user_id）
        if user_id and "get_user_preferences" in available and not any(
            obs.name == "get_user_preferences" for obs in observations
        ):
            return Plan("call", "get_user_preferences", {"user_id": user_id})

        # ② 逐个关注方向搜索
        if focus and len(searches) < min(self.max_searches, len(focus)):
            term = focus[len(searches)]
            args: dict[str, Any] = {
                "keyword": term,
                "date_range": "today",
                "max_items": 6,
            }
            if excludes:
                args["exclude_keywords"] = excludes
            return Plan("call", "search_news", args)

        candidates = self._candidates(searches)

        # ③ 当天结果太少时放宽时间窗（模拟「信息不足就换个策略」的行为）
        if candidates and len(candidates) < 3 and not any(
            obs.arguments.get("date_range") == "3d" for obs in searches
        ):
            return Plan(
                "call",
                "search_news",
                {"keyword": focus[0] if focus else "AI", "date_range": "3d", "max_items": 6},
            )

        # ④ 至少读一篇原文；摘要太薄的条目再多补读几篇
        attempted = {
            str(obs.arguments.get("url"))
            for obs in observations
            if obs.name == "fetch_article"
        }
        # 只统计成功的抓取，但「试过的」都要排除，否则会对同一个失败链接反复重试
        pending = [item for item in candidates if str(item.get("url")) not in attempted]
        if pending and not fetches:
            return Plan("call", "fetch_article", {"url": pending[0]["url"], "max_chars": 3000})

        thin = [item for item in pending if len(str(item.get("summary") or "")) < 60]
        if thin and len(fetches) < self.max_fetches:
            return Plan("call", "fetch_article", {"url": thin[0]["url"], "max_chars": 3000})

        # ⑤ 没有任何候选 → 如实结束，绝不编造
        if not candidates:
            return Plan("final", answer=self._empty_answer(task, observations))

        # ⑥ 写文件 → 入库
        selected = self._select(candidates, fetches)
        markdown = self._compose(task, selected, fetches, focus, excludes)

        path = self._output_path(task, user_id)
        if not wrote:
            return Plan("call", "write_file", {"path": path, "content": markdown, "mode": "overwrite"})

        if not any(obs.ok for obs in saved):
            # 入库失败过一次就不再重试（失败原因已经回填给模型/日志）
            if saved:
                return Plan("final", answer=self._save_failed_answer(saved[-1].summary))
            return Plan(
                "call",
                "save_briefing",
                {
                    "user_id": user_id or 1,
                    "content": markdown,
                    "file_path": path,
                    "briefing_date": _briefing_date(task),
                },
            )

        return Plan("final", answer=self._success_answer(selected, path, saved[-1], _briefing_date(task)))

    # ------------------------------------------------------------ 任务解析

    @staticmethod
    def _user_id(task: str, observations: list[Observation]) -> int | None:
        for obs in observations:
            if obs.name == "get_user_preferences" and obs.ok:
                value = obs.data.get("user_id")
                if isinstance(value, int):
                    return value
        match = re.search(r"user_id\s*[：:]\s*(\d+)", task)
        return int(match.group(1)) if match else None

    @staticmethod
    def _focus(task: str, observations: list[Observation]) -> tuple[list[str], list[str]]:
        """关注方向优先用工具返回的真实偏好，其次用任务文本。"""
        focus: list[str] = []
        excludes: list[str] = []

        for obs in observations:
            if obs.name == "get_user_preferences" and obs.ok:
                for key in ("topics", "keywords"):
                    focus.extend(str(item) for item in obs.data.get(key) or [])
                excludes.extend(str(item) for item in obs.data.get("exclude_keywords") or [])

        if not focus:
            focus = _terms(_field(task, "关注话题")) + _terms(_field(task, "关键词"))
        if not excludes:
            excludes = _terms(_field(task, "排除关键词"))

        # 去重保序
        seen: set[str] = set()
        unique: list[str] = []
        for term in focus:
            key = term.casefold()
            if key and key not in seen:
                seen.add(key)
                unique.append(term)
        return unique, excludes

    @staticmethod
    def _output_path(task: str, user_id: int | None) -> str:
        match = re.search(r"`([^`]+\.md)`", task)
        if match:
            return match.group(1)
        return f"briefings/{_briefing_date(task)}-user{user_id or 0}.md"

    # ------------------------------------------------------------ 候选整理

    @staticmethod
    def _candidates(searches: list[Observation]) -> list[dict[str, Any]]:
        """汇总所有搜索结果，按 news_id 去重并做一次标题近似去重。"""
        seen_ids: set[str] = set()
        merged: list[dict[str, Any]] = []
        for obs in searches:
            if not obs.ok:
                continue
            for item in obs.data.get("items") or []:
                news_id = str(item.get("news_id") or item.get("url"))
                if news_id in seen_ids:
                    continue
                seen_ids.add(news_id)
                merged.append(dict(item))

        merged.sort(key=lambda entry: (int(entry.get("match_score") or 0), str(entry.get("published") or "")), reverse=True)

        result: list[dict[str, Any]] = []
        for item in merged:
            if any(title_similarity(str(item.get("title") or ""), str(kept.get("title") or "")) >= 0.9 for kept in result):
                continue
            result.append(item)
        return result

    def _select(self, candidates: list[dict[str, Any]], fetches: list[Observation]) -> list[dict[str, Any]]:
        """挑出最终写进简报的条目：优先有正文的、相关度高的，并限制单一来源占比。

        为什么要限制单一来源：离线策略只能做字面匹配，某个源如果当天发的
        英文「AI agent」新闻特别多，就会把整份简报占满，读起来像单一媒体摘要。
        每个来源最多 2 条，能让简报更像「今天行业的横截面」。
        """
        fetched: dict[str, dict[str, Any]] = {}
        for obs in fetches:
            url = str(obs.arguments.get("url") or "")
            if url:
                fetched[url] = obs.data

        enriched = [{**item, "_article": fetched.get(str(item.get("url")))} for item in candidates]
        enriched.sort(
            key=lambda item: (
                1 if item["_article"] else 0,
                int(item.get("match_score") or 0),
                str(item.get("published") or ""),
            ),
            reverse=True,
        )

        selected: list[dict[str, Any]] = []
        per_source: dict[str, int] = {}
        for item in enriched:
            source = str(item.get("source") or "未知来源")
            if per_source.get(source, 0) >= 2:
                continue
            per_source[source] = per_source.get(source, 0) + 1
            selected.append(item)
            if len(selected) >= self.max_items:
                break
        return selected

    # ------------------------------------------------------------ 文本生成

    @staticmethod
    def _sentences(text: str, limit: int = 2) -> str:
        """从正文里取前几句（不加工、不改写）。

        只在「标点 + 空白」处断句，避免把 `$7.5M`、`0.720` 这类小数切断。
        """
        cleaned = re.sub(r"\s+", " ", (text or "").strip())
        cleaned = cleaned.replace("## ", "")
        if not cleaned:
            return ""
        parts = re.split(r"(?<=[。！？!?])\s+|(?<=\.)\s+", cleaned)
        picked = "".join(parts[:limit]).strip()
        # 拼接后英文标点会和下一个单词黏在一起（「stylist?That's」），补回空格
        picked = re.sub(r"(?<=[.!?])(?=[A-Z])", " ", picked)
        return picked[:220] + ("…" if len(picked) > 220 else "")

    @classmethod
    def _event_sentence(cls, text: str, title: str = "") -> str:
        """挑一句适合当「核心事件」的句子；挑不出就返回空串。

        跳过三类句子：
        - 疑问句：英文报道常以「Can an AI agent serve as your personal stylist?」
          这类设问开场，它只是行文钩子，当成事件描述会让读者以为没讲事实；
        - 指代式开场：紧跟在设问后的「That's what ... aims to find out.」离开上文读不通；
        - 与标题高度相似的句子：等于复述标题，没有信息增量。
        """
        cleaned = re.sub(r"\s+", " ", (text or "").strip()).replace("## ", "")
        if not cleaned:
            return ""
        parts = [p.strip() for p in re.split(r"(?<=[。！？!?])\s+|(?<=\.)\s+", cleaned) if p.strip()]
        for part in parts[:4]:
            if part.endswith(("?", "？")):
                continue
            if len(part) < 20:
                continue
            if re.sub(r"['’]s?$", "", part.split(" ", 1)[0]).lower() in _ANAPHORIC_STARTERS:
                continue
            if part[:1] in _CJK_ANAPHORA:
                continue
            if title and title_similarity(part, title) >= 0.9:
                continue
            return part[:220]
        return ""

    @staticmethod
    def _first_clause(text: str) -> str | None:
        """取第一个逗号前的分句（模板避免摘要与核心事件完全重复）。

        只在分句足够长（≥30 字符）且占原句 35% 以上时才采用：
        否则会得到「With a team of just seven.」这种语义不完整、容易误导的碎片。
        """
        for separator in ("，", ",", "；", ";", "：", ":"):
            if separator not in text:
                continue
            head = text.split(separator, 1)[0].strip()
            if len(head) >= 30 and len(head) >= len(text) * 0.35:
                if head.endswith(("。", ".", "！", "？", "!", "?")):
                    return head
                return head + ("." if head[-1:].isascii() else "。")
        return None

    def _compose(
        self,
        task: str,
        selected: list[dict[str, Any]],
        fetches: list[Observation],
        focus: list[str],
        excludes: list[str],
    ) -> str:
        """按简报模板拼装 Markdown（内容全部来自工具结果）。"""
        day = _briefing_date(task)
        nickname = _field(task, "昵称") or "用户"
        identity = _field(task, "身份/职业", "身份") or "未提供"
        focus_text = "、".join(focus) or "（未设置）"

        lines: list[str] = [
            "# 每日 AI 新闻简报",
            "",
            "> 生成方式：离线策略（Mock 模式）。条目、来源与链接均来自真实 RSS 检索结果，内容未经大模型润色；",
            "> 相关度仅按关键词字面匹配计算，不具备语义判断能力——配置 LLM_API_KEY 后由模型判断相关性。",
            "",
            f"- 简报日期：{day}",
            f"- 用户关注方向：{focus_text}",
            f"- 今日新闻数量：{len(selected)}",
            "",
            "## 今日重点新闻",
            "",
        ]

        for index, item in enumerate(selected, start=1):
            article = item.get("_article") or {}
            title = item.get("title") or "(无标题)"
            body = str(article.get("content") or "")
            feed_summary = re.sub(r"\s+", " ", str(item.get("summary") or "")).strip()

            if body:
                summary = self._sentences(body, 2)
                empty_hint = "未提炼出独立事件句，详见上方摘要"
            else:
                summary = feed_summary[:220] or "（该条仅有标题，未获取到摘要）"
                empty_hint = "未获取正文，核心事件以摘要为准"

            event = self._event_sentence(body or summary, title)
            if not event:
                event = empty_hint
            elif event == summary:
                # 正文只有一句时事件会和摘要完全重复，退一步取分句；取不出就如实标注
                event = self._first_clause(summary) or empty_hint

            matched = "、".join(item.get("matched_terms") or []) or "（无）"
            published = item.get("published") or article.get("published") or "未获取到"

            lines += [
                f"### {index}. {title}",
                f"- 新闻摘要：{summary}",
                f"- 核心事件：{event}",
                (
                    f"- 重要性分析：与「{nickname}」关注的 {matched} 直接相关，"
                    f"来源为 {item.get('source') or '未知来源'}；"
                    f"{'已读取正文' if article else '仅依据标题与订阅摘要，未获取正文'}。"
                ),
                f"- 新闻来源：{item.get('source') or article.get('source') or '未获取到'}",
                f"- 原文链接：{item.get('url') or article.get('url') or '未获取到'}",
                f"- 发布时间：{published}",
                "",
            ]

        lines += ["## 行业动态总结", ""]
        lines += self._trends(selected, identity)
        lines += ["", "## 今日值得关注", ""]
        lines += self._watchlist(selected, focus, identity)

        if excludes:
            lines += ["", f"> 已按用户设置过滤排除词：{'、'.join(excludes)}。"]

        return "\n".join(lines) + "\n"

    @staticmethod
    def _trends(selected: list[dict[str, Any]], identity: str) -> list[str]:
        sources = sorted({str(item.get("source") or "") for item in selected if item.get("source")})
        terms: list[str] = []
        for item in selected:
            for term in item.get("matched_terms") or []:
                if term not in terms:
                    terms.append(str(term))
        lines = [
            f"今天共筛出 {len(selected)} 条与「{identity}」关注方向相关的新闻，"
            f"分别来自 {len(sources)} 个来源（{'、'.join(sources) or '无'}）。",
        ]
        if terms:
            lines.append(f"共同话题集中在：{'、'.join(terms[:6])}。")
        lines.append(
            "以上归纳仅基于条目标题与已读正文的关键词分布，未做跨事件的因果推断；"
            "如需行业级结论，请结合原文阅读。"
        )
        return lines

    @staticmethod
    def _watchlist(selected: list[dict[str, Any]], focus: list[str], identity: str) -> list[str]:
        if not selected:
            return ["今天没有可推荐的条目。"]
        lines = []
        for item in selected[:3]:
            lines.append(
                f"- 《{item.get('title')}》：值得点开原文确认细节，"
                f"重点关注它与「{'、'.join((item.get('matched_terms') or focus)[:2])}」的关系。"
            )
        lines.append(f"- 结合「{identity}」的视角，可挑 1~2 条做动手验证或写进技术笔记。")
        return lines

    # ------------------------------------------------------------ 最终回答

    @staticmethod
    def _empty_answer(task: str, observations: list[Observation]) -> str:
        searched = [obs for obs in observations if obs.name == "search_news"]
        detail = "；".join(obs.summary for obs in searched[-3:]) if searched else "未执行搜索"
        return (
            "**（离线策略运行）** 今天没有检索到与用户关注方向相关的新闻，因此没有生成简报。\n\n"
            f"检索情况：{detail}\n\n"
            "按约定不为了凑数量而收录无关新闻；可稍后重试或放宽时间范围。"
        )

    @staticmethod
    def _success_answer(
        selected: list[dict[str, Any]], path: str, save: Observation, day: str
    ) -> str:
        return (
            f"**（离线策略运行）** 已生成 {day} 的简报，共 {len(selected)} 条。\n\n"
            f"- 文件：`{path}`\n"
            f"- 入库：{save.summary}\n"
            f"- 条目：{'；'.join(str(item.get('title'))[:30] for item in selected)}"
        )

    @staticmethod
    def _save_failed_answer(reason: str, ) -> str:
        return (
            "**（离线策略运行）** 简报已写入文件，但入库被拒绝：\n\n"
            f"{reason}\n\n"
            "离线策略不会自动改写内容，请人工确认简报结构或链接是否合规。"
        )


def dump_call(name: str, arguments: dict[str, Any]) -> str:
    """调试用：把一次调用参数打成单行 JSON。"""
    return f"{name}({json.dumps(arguments, ensure_ascii=False)[:200]})"
