"""第三阶段演示脚本：为两组不同偏好的用户生成新闻简报。

用法（在项目根目录执行）：

    python -m scripts.briefing_demo                 # 真实的 RSS + 当前 AGENT_MODE
    python -m scripts.briefing_demo --mode mock     # 离线策略（无需 API Key）
    python -m scripts.briefing_demo --mode openai   # 强制真实模型
    python -m scripts.briefing_demo --date 2026-11-17

脚本会做三件事：
1. 幂等地准备两个演示用户（A：AI Agent / MCP；B：大模型 / AI 编程）；
2. 分别跑一次简报生成，把工具调用轨迹打到控制台；
3. 打印生成的 Markdown 路径与入库结果，便于直接对比两份简报的差异。

轨迹里能清楚看到「模型自己决定了什么」：搜了哪些词、搜了几次、
读了哪几篇原文、什么时候收笔。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date
from pathlib import Path

# 允许 `python scripts/briefing_demo.py` 直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.orm import Session  # noqa: E402

from app.agent.briefing_task import BriefingRunResult, run_briefing  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.database import SessionLocal, init_db  # noqa: E402
from app.schemas.subscription import SubscriptionUpsert  # noqa: E402
from app.schemas.user import UserCreate  # noqa: E402
from app.services import briefing_service, subscription_service, user_service  # noqa: E402

# ---------------------------------------------------------------- 演示用户

DEMO_USERS: list[dict] = [
    {
        "key": "A",
        "nickname": "林工（用户A）",
        "identity": "AI Agent 应用开发工程师，正在做基于 MCP 的工具调用与多智能体编排",
        "topics": ["AI Agent", "MCP", "Agent 框架"],
        "keywords": ["多智能体", "工具调用", "Model Context Protocol"],
        "exclude_keywords": ["融资", "股价", "招聘"],
    },
    {
        "key": "B",
        "nickname": "周工（用户B）",
        "identity": "后端开发工程师，关注大模型能力演进与 AI 编程工具在工程中的落地",
        "topics": ["大模型", "AI 编程"],
        "keywords": ["代码生成", "Copilot", "Coding Agent"],
        "exclude_keywords": ["融资", "股价", "招聘"],
    },
]


def ensure_user(db: Session, spec: dict) -> int:
    """按昵称幂等创建演示用户，并覆盖订阅偏好。"""
    users, _ = user_service.list_users(db, page=1, page_size=100)
    existing = next((user for user in users if user.nickname == spec["nickname"]), None)

    if existing is None:
        user = user_service.create_user(
            db,
            UserCreate(nickname=spec["nickname"], identity=spec["identity"]),
        )
        user_id = user.id
    else:
        user_id = existing.id

    subscription_service.upsert_subscription(
        db,
        user_id,
        SubscriptionUpsert(
            topics=spec["topics"],
            keywords=spec["keywords"],
            exclude_keywords=spec["exclude_keywords"],
        ),
    )
    return user_id


def print_trace(result: BriefingRunResult) -> None:
    """打印本次运行的工具轨迹。"""
    record = result.record
    print(f"  状态={record.status} | 轮数={record.steps} | 工具调用={record.tool_call_count} | "
          f"耗时={record.duration_ms} ms | 模型={record.llm_mode}/{record.model}")
    for item in record.tool_calls:
        status = "✓" if item.ok else f"✗[{item.error_code}]"
        args = item.arguments
        brief = ", ".join(f"{k}={str(v)[:36]}" for k, v in list(args.items())[:2] if k in
                          ("keyword", "date_range", "url", "path", "user_id", "briefing_date"))
        print(f"    {item.index:>2}. {item.tool}({brief}) {status} — {item.summary[:110]}")
    print(f"  最终回答：{record.answer.strip().splitlines()[0][:160] if record.answer else '（空）'}")
    if result.warnings:
        print(f"  质量提示：{'；'.join(result.warnings)}")


async def run_one(db: Session, spec: dict, day: date, mode: str | None) -> BriefingRunResult:
    """跑一个用户。"""
    user_id = ensure_user(db, spec)
    profile = briefing_service.get_user_profile(db, user_id)
    print(f"\n{'=' * 78}\n用户 {spec['key']}：{profile.nickname}（id={user_id}）")
    print(f"关注方向：{'、'.join(profile.focus)}")
    print(f"排除词：{'、'.join(profile.exclude_keywords) or '无'}")
    print("-" * 78)

    # 清掉上一轮遗留的简报文件：否则模型可能读到旧稿（甚至别的模式写的稿）而被带偏，
    # 让「本轮自主决策」这件事不干净。数据库里的旧简报仍保留，由同日去重逻辑覆盖。
    stale = settings.agent_workspace_path / briefing_service.briefing_relative_path(user_id, day)
    if stale.exists():
        stale.unlink()
        print(f"  （已清理上一轮遗留的 {stale.name}）")

    result = await run_briefing(db, profile, briefing_date=day, llm_mode=mode)
    print_trace(result)

    if result.briefing is not None:
        print(f"  入库：简报 #{result.briefing.id}，{result.briefing.news_count} 条，"
              f"文件 {result.file_path or result.briefing.file_path}")
        print(f"  候选新闻 {result.news_candidates} 条 / 抓取正文 {result.fetched_articles} 篇 / "
              f"搜索 {len(result.searches)} 次：{[s['keyword'] for s in result.searches]}")
    else:
        print("  未生成简报（见质量提示）")
    return result


async def main() -> None:
    parser = argparse.ArgumentParser(description="生成两组用户的每日 AI 新闻简报")
    parser.add_argument("--mode", default=None, choices=["auto", "openai", "mock"], help="覆盖 AGENT_MODE")
    parser.add_argument("--date", default=None, help="简报日期 YYYY-MM-DD，默认今天")
    parser.add_argument("--user", default=None, choices=["A", "B"], help="只跑其中一个用户")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="打印 app.* 的 INFO 日志（缓存命中、上下文压缩、RSS 耗时等排查信息）",
    )
    args = parser.parse_args()

    # 脚本不经过 app/main.py 的日志初始化，默认什么都看不到；
    # 加 --verbose 才能看到「缓存命中 / 上下文压缩」这类关键观测点
    if args.verbose:
        import logging

        logging.basicConfig(
            level=logging.WARNING,
            format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
            datefmt="%H:%M:%S",
        )
        for name in ("app.agent", "app.news", "app.services"):
            logging.getLogger(name).setLevel(logging.INFO)

    day = date.fromisoformat(args.date) if args.date else date.today()
    init_db()

    print(f"工作区：{settings.agent_workspace_path}")
    print(f"简报日期：{day} | 模式：{args.mode or settings.AGENT_MODE}")

    db = SessionLocal()
    try:
        specs = [spec for spec in DEMO_USERS if args.user in (None, spec["key"])]
        for spec in specs:
            await run_one(db, spec, day, args.mode)
    finally:
        db.close()

    print(f"\n{'=' * 78}\n完成。生成的 Markdown 位于 {settings.agent_workspace_path / settings.BRIEFING_OUTPUT_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
