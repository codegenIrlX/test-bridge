"""Постановка заданий в BullMQ и запуск очистки архивов."""

import asyncio
import logging
from pathlib import Path
from shutil import disk_usage

from bullmq import Job, Queue
from redis.exceptions import RedisError
from sqlalchemy.exc import DBAPIError as DatabaseError

from metrics_srv.config import Settings
from metrics_srv.domains.imports.cleanup import ArchiveCleanup
from metrics_srv.domains.imports.services import ImportService
from metrics_srv.workers.runtime import QUEUE_NAME, shutdown_event

logger = logging.getLogger(__name__)


async def _wait_for_next_cycle(stop: asyncio.Event, interval: int) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=interval)
    except TimeoutError:
        pass


async def _schedule_imports(
    service: ImportService, queue: Queue, stop: asyncio.Event, interval: int,
) -> None:
    while not stop.is_set():
        try:
            for import_id in await asyncio.to_thread(service.pending_ids):
                job_id = str(import_id)
                existing = await Job.fromId(queue, job_id)
                if existing is not None and await existing.getState() in ("completed", "failed"):
                    await existing.remove()
                await queue.add(
                    "parse", {"importId": job_id},
                    {"jobId": job_id, "attempts": 3,
                     "backoff": {"type": "fixed", "delay": 5000},
                     "removeOnComplete": {"count": 100},
                     "removeOnFail": {"count": 100}},
                )
        except (DatabaseError, RedisError, OSError) as exc:
            logger.error("Не удалось поставить задания в очередь: %s", type(exc).__name__)
        await _wait_for_next_cycle(stop, interval)


async def _cleanup_archives(
    archive_cleanup: ArchiveCleanup, stop: asyncio.Event, interval: int,
) -> None:
    while not stop.is_set():
        for cleanup in (archive_cleanup.cleanup_due_archives, archive_cleanup.cleanup_orphans):
            try:
                await asyncio.to_thread(cleanup)
            except (DatabaseError, OSError) as exc:
                logger.error("Ошибка очистки архивов: %s", type(exc).__name__)
        await _wait_for_next_cycle(stop, interval)


async def _report_health(service: ImportService, storage_path: Path, stop: asyncio.Event) -> None:
    while not stop.is_set():
        oldest_age, free_bytes = None, None
        try:
            oldest_age = await asyncio.to_thread(service.oldest_unfinished_age_seconds)
        except DatabaseError as exc:
            logger.warning("Не удалось измерить возраст импорта: %s", type(exc).__name__)
        try:
            free_bytes = (await asyncio.to_thread(disk_usage, storage_path)).free
        except OSError as exc:
            logger.warning("Не удалось измерить свободное место архивов: %s", type(exc).__name__)
        logger.info(
            "Состояние импорта: oldest_unfinished_age_seconds=%s archive_free_bytes=%s",
            oldest_age, free_bytes,
        )
        await _wait_for_next_cycle(stop, 60)


async def run_scheduler(
    settings: Settings, service: ImportService, cleanup: ArchiveCleanup,
) -> None:
    """Восстанавливает задания из БД и независимо очищает архивы."""
    stop = shutdown_event()
    interval = settings.scheduler_interval_seconds
    queue = Queue(QUEUE_NAME, {"connection": f"redis://{settings.redis_host}:{settings.redis_port}"})
    try:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(_cleanup_archives(cleanup, stop, interval))
            tasks.create_task(_report_health(service, settings.storage_path, stop))
            scheduling = tasks.create_task(_schedule_imports(service, queue, stop, interval))
            await stop.wait()
            # Ожидание Redis можно прервать, а начатой очистке даём завершиться.
            scheduling.cancel()
    finally:
        await queue.close()
