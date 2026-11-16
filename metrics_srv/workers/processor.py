"""Выполнение заданий импорта из BullMQ."""

import asyncio
from uuid import UUID

from bullmq import Job, Worker

from metrics_srv.config import Settings
from metrics_srv.domains.imports.processing import ImportProcessor
from metrics_srv.domains.imports.services import ImportService
from metrics_srv.workers.runtime import QUEUE_NAME, shutdown_event


async def run_processor(
    settings: Settings, service: ImportService, processor: ImportProcessor,
) -> None:
    """Принимать задания очереди и сохранять результаты импорта."""
    stop = shutdown_event()

    async def process(job: Job, _token: str) -> None:
        import_id = UUID(job.data["importId"])
        await asyncio.to_thread(processor.process_import, import_id)
        result = await asyncio.to_thread(service.get_import, import_id)
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
