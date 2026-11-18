"""简报生成编排（第四阶段）。

职责：把「任务状态机」和「第三阶段的 Agent 生成能力」接起来。

```
定时/手动触发
   │  task_service.get_or_create()   ← 幂等：DB 唯一键 (user_id, date)
   ▼
任务入队（generation_worker）
   │  task_service.claim()           ← 原子领取：条件 UPDATE
   ▼
读用户画像 → run_briefing(...)      ← 完全复用第三阶段：同一个 AgentRunner、同一套工具
   ▼
记录 success / failed / skipped
   ▼
push_service.push_for_task()        ← 推送失败不影响生成状态
```

**这里没有第二套新闻逻辑**：搜索、筛选、写作、入库仍然由
`app/agent/briefing_task.run_briefing` 完成，本模块只负责「什么时候跑、跑完怎么记」。

幂等与隔离：
- 同一用户同一天只会被执行一次（状态机 + 唯一键 + 原子领取）；
- 单个用户失败只影响自己的任务行，异常在这里被收敛，不会冒泡到调度循环；
- 生成与推送用两个数据库会话，推送挂掉不会回滚已入库的简报。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from app.agent.briefing_task import run_briefing
from app.core.config import settings
from app.core.exceptions import AppError
from app.crud import task as task_crud
from app.models.task import GenerationTask, TaskStatus
from app.services import briefing_service, push_service, task_service

logger = logging.getLogger(__name__)

__all__ = ["ExecutionResult", "execute_task"]


@dataclass(slots=True)
class ExecutionResult:
    """一次任务执行的结论（供 worker 日志与测试断言）。"""

    task_id: int
    status: str
    message: str
    briefing_id: int | None = None
    push_status: str | None = None
    news_count: int = 0

    @property
    def ok(self) -> bool:
        return self.status == TaskStatus.SUCCESS.value


async def execute_task(task_id: int) -> ExecutionResult:
    """执行一条已登记的任务（幂等：抢不到执行权就直接返回）。

    Args:
        task_id: 任务 ID。

    Returns:
        ExecutionResult。**不抛异常**——所有失败都落成 `status=failed`，
        这样调度器遍历用户时不会被单个用户拖垮。
    """
    from app.core.database import SessionLocal

    with SessionLocal() as db:
        task = task_service.claim(db, task_id)
        if task is None:
            logger.info("任务 %s 未被领取（可能已由其它进程执行或已完成）", task_id)
            return ExecutionResult(task_id=task_id, status="not_claimed", message="任务已被其它执行者领取或已完成")

        user_id = task.user_id
        briefing_date = task.briefing_date
        stop_heartbeat = asyncio.Event()
        heartbeat_task = asyncio.create_task(_heartbeat_loop(task_id, stop_heartbeat))

        try:
            result = await _run_agent(db, task_id=task_id, user_id=user_id, briefing_date=briefing_date)
        except asyncio.CancelledError:
            # 服务关闭时被打断：保留 running 状态，等下次启动按心跳过期重新领取
            logger.warning("任务 %s 被取消（服务正在关闭）", task_id)
            raise
        except Exception as exc:  # noqa: BLE001 - 兜底，不能把异常抛给调度器
            logger.exception("任务 %s 执行时出现未预期异常", task_id)
            task_row = _reload(db, task_id)
            if task_row is not None:
                task_service.fail(db, task_row, message=f"{type(exc).__name__}: {exc}")
            return ExecutionResult(task_id=task_id, status=TaskStatus.FAILED.value, message=str(exc))
        finally:
            stop_heartbeat.set()
            try:
                await heartbeat_task
            except Exception:  # noqa: BLE001 - 心跳失败不影响主流程
                logger.debug("心跳协程结束异常", exc_info=True)

    # ---------------- 推送（独立会话，失败不影响生成结论） ----------------
    push_status: str | None = None
    if result.ok and briefing_date == date.today():
        try:
            push_result = await asyncio.to_thread(push_service.push_for_task, task_id)
            push_status = push_result.status
            result.push_status = push_status
            logger.info("任务 %s 推送结论：%s（%s）", task_id, push_result.status, push_result.message)
        except Exception as exc:  # noqa: BLE001 - 推送异常只记录
            logger.exception("任务 %s 推送时出现异常", task_id)
            push_status = "error"
            result.push_status = push_status
            result.message = f"{result.message}；推送异常：{exc}"
    elif result.ok:
        logger.info("任务 %s 属于历史日期（%s），跳过推送", task_id, briefing_date)

    return result


# ---------------------------------------------------------------- 内部实现


async def _run_agent(db: Session, *, task_id: int, user_id: int, briefing_date: date) -> ExecutionResult:
    """跑一次 Agent 并落状态。"""
    task = _reload(db, task_id)
    if task is None:  # pragma: no cover - 领取成功后不可能取不到
        return ExecutionResult(task_id=task_id, status="not_claimed", message="任务记录丢失")

    # 1) 读画像：没有偏好就没法个性化，直接跳过（定时任务不该天天报错）
    try:
        profile = briefing_service.get_user_profile(db, user_id)
    except AppError as exc:
        task_service.fail(db, task, message=f"读取用户画像失败：{exc.message}")
        return ExecutionResult(task_id=task_id, status=TaskStatus.FAILED.value, message=exc.message)

    if not profile.focus:
        reason = "用户尚未设置订阅偏好（关注话题 / 关键词为空），本次跳过"
        task_service.skip(db, task, reason=reason)
        return ExecutionResult(task_id=task_id, status=TaskStatus.SKIPPED.value, message=reason)

    # 2) 交给第三阶段的编排层：搜索策略、读不读原文、何时收笔全由模型决定
    try:
        brief_result = await run_briefing(db, profile, briefing_date=briefing_date)
    except AppError as exc:
        # 配置类错误（如未配置 LLM_API_KEY 却强制 openai）属于可修正问题
        task_service.fail(db, task, message=exc.message)
        return ExecutionResult(task_id=task_id, status=TaskStatus.FAILED.value, message=exc.message)
    except Exception as exc:  # noqa: BLE001 - 网络/解析等运行时异常
        logger.exception("任务 %s 运行 Agent 失败", task_id)
        task_service.fail(db, task, message=f"{type(exc).__name__}: {exc}")
        return ExecutionResult(task_id=task_id, status=TaskStatus.FAILED.value, message=str(exc))

    # 3) 没有产出简报 = 失败（模型可能没搜到新闻，也可能是 save_briefing 被拒）
    if brief_result.briefing is None:
        reason = "；".join(brief_result.warnings) or "本次运行没有产生入库的简报"
        task_service.fail(db, task, message=reason, warnings=brief_result.warnings, run_id=brief_result.record.run_id)
        return ExecutionResult(task_id=task_id, status=TaskStatus.FAILED.value, message=reason)

    briefing = brief_result.briefing
    task_service.succeed(
        db,
        task,
        briefing=briefing,
        run_id=brief_result.record.run_id,
        warnings=brief_result.warnings,
    )
    return ExecutionResult(
        task_id=task_id,
        status=TaskStatus.SUCCESS.value,
        message=f"简报已生成（{briefing.news_count} 条）",
        briefing_id=briefing.id,
        news_count=briefing.news_count,
    )


def _reload(db: Session, task_id: int) -> GenerationTask | None:
    """重新从库里取任务，避免使用过期对象。"""
    db.expire_all()
    return task_crud.get(db, task_id)


async def _heartbeat_loop(task_id: int, stop: asyncio.Event, interval: float = 60.0) -> None:
    """运行期间定期刷新心跳，避免长任务被误判为僵尸。

    真实模型的简报任务可能跑 2~5 分钟，如果 SCHEDULER_STALE_MINUTES 调得很小，
    没有心跳就会被另一个进程「抢走」重复执行。心跳让「还活着」这件事可观测。
    """
    from app.core.database import SessionLocal

    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return  # 收到停止信号
        except asyncio.TimeoutError:
            try:
                with SessionLocal() as db:
                    task_crud.heartbeat(db, task_id)
                logger.debug("任务 %s 心跳已刷新", task_id)
            except Exception:  # noqa: BLE001 - 心跳失败不应影响任务本身
                logger.warning("任务 %s 心跳刷新失败", task_id, exc_info=True)


def task_stale_minutes() -> int:
    """暴露配置值，便于接口与日志展示。"""
    return max(1, settings.SCHEDULER_STALE_MINUTES)
