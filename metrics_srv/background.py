"""Постановка и выполнение заданий импорта через BullMQ."""

from __future__ import annotations

import asyncio
import logging
import signal
from uuid import UUID

from bullmq import Job, Queue, Worker
from redis.exceptions import RedisError
from sqlalchemy.exc import DBAPIError

from metrics_srv.config import Settings
from metrics_srv.import_processing import ImportProcessor
from metrics_srv.repository import Repository

logger = logging.getLogger(__name__)
QUEUE_NAME = "ALLURE_IMPORTS_QUEUE"


def _shutdown_event() -> asyncio.Event:
    event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, event.set)
        except NotImplementedError:
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(event.set))
    return event


async def run_scheduler(settings: Settings) -> None:
    """Восстанавливать задания из БД и очищать обработанные архивы."""
    repository = Repository(settings)
    queue = Queue(QUEUE_NAME, {"connection": f"redis://{settings.redis_host}:{settings.redis_port}"})
    stop = _shutdown_event()
    try:
        while not stop.is_set():
            try:
                for import_id in await asyncio.to_thread(repository.pending_ids):
                    existing = await Job.fromId(queue, str(import_id))
                    if existing is not None:
                        state = await existing.getState()
                        if state in ("completed", "failed"):
                            await existing.remove()
                    await queue.add(
                        "parse", {"importId": str(import_id)},
                        {"jobId": str(import_id), "attempts": 3,
                         "backoff": {"type": "fixed", "delay": 5000},
                         "removeOnComplete": {"count": 100},
                         "removeOnFail": {"count": 100}},
                    )
                await asyncio.to_thread(repository.cleanup_completed)
                await asyncio.to_thread(repository.cleanup_orphans)
            except (DBAPIError, RedisError, OSError) as exc:
                logger.error("Ошибка цикла Scheduler: %s", type(exc).__name__)
            try:
                await asyncio.wait_for(stop.wait(), timeout=settings.scheduler_interval_seconds)
            except TimeoutError:
                pass
    finally:
        await queue.close()


async def run_processor(settings: Settings) -> None:
    """Принимать задания очереди и сохранять результаты импорта."""
    repository = Repository(settings)
    processor = ImportProcessor(settings, repository)
    stop = _shutdown_event()

    async def process(job: Job, token: str) -> None:
        import_id = UUID(job.data["importId"])
        await asyncio.to_thread(processor.process_import, import_id)
        result = await asyncio.to_thread(repository.get_import, import_id)
        if result is not None and result["status"] == "failed":
            raise RuntimeError("Ошибка обработки архива Allure")

    worker = Worker(
        QUEUE_NAME, process,
        {"connection": f"redis://{settings.redis_host}:{settings.redis_port}"},
    )
    try:
        await stop.wait()
    finally:
        await worker.close()
