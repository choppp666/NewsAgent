"""生成任务的后台执行器（worker 池）。

为什么需要一个池子而不是「每来一个任务就 `asyncio.create_task`」：

1. **并发上限**：每个简报任务都会打若干次 LLM 请求 + 抓十几个 RSS 源，
   定时任务一瞬间给 20 个用户发任务，直接开 20 个协程会把模型配额和网络打满。
   池子把并发压到 `SCHEDULER_MAX_CONCURRENCY`。
2. **队列即背压**：任务先进队列，用户看到的状态是 pending（等待执行），
   不会因为排队而超时失败。
3. **兜底执行**：如果 worker 没启动（例如未走 lifespan 的测试环境），
   自动退回「后台线程 + 独立事件循环」，功能仍然可用，只是不受并发上限约束。

重复提交在队列层被拦一次（`_submitted` 集合），在数据库层再被拦一次
（`task_service.claim` 的条件 UPDATE）。两层是故意的：
队列层省掉无谓的数据库往返，数据库层才是真正保证。
"""

from __future__ import annotations

import asyncio
import logging
import threading

from app.core.config import settings
from app.services.generation_service import ExecutionResult, execute_task

logger = logging.getLogger(__name__)

__all__ = ["GenerationWorker", "generation_worker"]


class GenerationWorker:
    """基于 `asyncio.Queue` 的固定并发 worker 池。"""

    def __init__(self, concurrency: int | None = None) -> None:
        self.concurrency = max(1, int(concurrency or settings.SCHEDULER_MAX_CONCURRENCY))
        self._queue: asyncio.Queue[int] | None = None
        self._workers: list[asyncio.Task[None]] = []
        self._submitted: set[int] = set()
        self._lock = threading.Lock()
        self._running = False

    # ---------------------------------------------------------- 生命周期

    @property
    def running(self) -> bool:
        return self._running

    @property
    def pending(self) -> int:
        """队列中尚未开始的任务数。"""
        return self._queue.qsize() if self._queue is not None else 0

    async def start(self) -> None:
        """启动 worker 池（幂等）。"""
        if self._running:
            return
        self._queue = asyncio.Queue()
        self._workers = [
            asyncio.create_task(self._worker_loop(index), name=f"generation-worker-{index}")
            for index in range(self.concurrency)
        ]
        self._running = True
        logger.info("生成任务 worker 已启动：并发 %s", self.concurrency)

    async def stop(self) -> None:
        """停止 worker 池：先取消协程，未完成的任务保持 running 状态等重启后按僵尸任务处理。"""
        if not self._running:
            return
        self._running = False
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        self._queue = None
        self._submitted.clear()
        logger.info("生成任务 worker 已停止")

    # ---------------------------------------------------------- 提交任务

    def submit(self, task_id: int) -> str:
        """提交任务执行。

        Args:
            task_id: 任务 ID。

        Returns:
            `queued`（已入队）/ `thread`（worker 未启动，改用后台线程）/
            `duplicate`（同一任务已在队列或执行中）。
        """
        with self._lock:
            if task_id in self._submitted:
                logger.info("任务 %s 已在执行队列中，忽略重复提交", task_id)
                return "duplicate"
            self._submitted.add(task_id)

        if self._running and self._queue is not None:
            try:
                self._queue.put_nowait(task_id)
                logger.info("任务 %s 已入队（当前排队 %s 个）", task_id, self._queue.qsize())
                return "queued"
            except Exception:  # noqa: BLE001 - 入队失败退回线程执行
                logger.exception("任务 %s 入队失败，改用后台线程", task_id)

        self._spawn_thread(task_id)
        return "thread"

    # ---------------------------------------------------------- 内部实现

    async def _worker_loop(self, index: int) -> None:
        assert self._queue is not None  # noqa: S101 - 启动时一定已创建
        while True:
            task_id = await self._queue.get()
            try:
                result: ExecutionResult = await execute_task(task_id)
                logger.info(
                    "[worker-%s] 任务 %s 结束：%s（%s）",
                    index,
                    task_id,
                    result.status,
                    result.message[:160],
                )
            except asyncio.CancelledError:
                logger.warning("[worker-%s] 任务 %s 被取消", index, task_id)
                raise
            except Exception:  # noqa: BLE001 - 单任务异常不能带崩 worker
                logger.exception("[worker-%s] 任务 %s 执行异常", index, task_id)
            finally:
                with self._lock:
                    self._submitted.discard(task_id)
                self._queue.task_done()

    def _spawn_thread(self, task_id: int) -> None:
        """worker 未启动时的兜底：独立线程 + 独立事件循环。"""

        def _runner() -> None:
            try:
                result = asyncio.run(execute_task(task_id))
                logger.info("[thread] 任务 %s 结束：%s", task_id, result.status)
            except Exception:  # noqa: BLE001 - 后台线程异常必须自己吞掉
                logger.exception("[thread] 任务 %s 执行异常", task_id)
            finally:
                with self._lock:
                    self._submitted.discard(task_id)

        thread = threading.Thread(target=_runner, name=f"generation-task-{task_id}", daemon=True)
        thread.start()
        logger.warning("worker 未启动，任务 %s 改由后台线程执行", task_id)


# 进程内单例：接口层与调度器共用同一个队列，才能保证「同一任务不会跑两次」
generation_worker = GenerationWorker()
