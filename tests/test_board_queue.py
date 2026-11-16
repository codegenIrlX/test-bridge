import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from metrics_srv.workers import processor, scheduler


def finish(coroutine):
    with pytest.raises(StopIteration):
        coroutine.send(None)


async def immediate_thread(function, *args):
    return function(*args)


@pytest.mark.parametrize(
    ("state", "should_remove"),
    [
        ("completed", True),
        ("failed", True),
        ("active", False),
        ("waiting", False),
        ("delayed", False),
        (None, False),
    ],
)
def test_scheduler_retains_history_and_requeues_terminal_jobs(
    monkeypatch: pytest.MonkeyPatch, state: str | None, should_remove: bool
) -> None:
    import_id = uuid4()
    service = MagicMock()
    service.pending_ids.return_value = [import_id]
    cleanup = MagicMock()
    queue = MagicMock()
    queue.add = AsyncMock()
    queue.close = AsyncMock()
    job = MagicMock()
    job.getState = AsyncMock(return_value=state)
    job.remove = AsyncMock()
    stop = asyncio.Event()
    queue.add.side_effect = lambda *args: stop.set()
    make_queue = MagicMock(return_value=queue)
    monkeypatch.setattr(scheduler, "Queue", make_queue)
    monkeypatch.setattr(scheduler.Job, "fromId", AsyncMock(return_value=job if state else None))
    monkeypatch.setattr(scheduler, "shutdown_event", lambda: stop)
    monkeypatch.setattr(scheduler.asyncio, "to_thread", immediate_thread)
    settings = SimpleNamespace(redis_host="redis", redis_port=6379, scheduler_interval_seconds=1)

    asyncio.run(asyncio.wait_for(scheduler.run_scheduler(settings, service, cleanup), timeout=2))

    assert make_queue.call_args.args[0] == "ALLURE_IMPORTS_QUEUE"
    assert job.remove.await_count == int(should_remove)
    name, payload, options = queue.add.await_args.args
    assert name == "parse"
    assert payload == {"importId": str(import_id)}
    assert options["jobId"] == str(import_id)
    assert options["removeOnComplete"] == {"count": 100}
    assert options["removeOnFail"] == {"count": 100}
    queue.close.assert_awaited_once()


@pytest.mark.parametrize("redis_stalls", [False, True], ids=["fails", "stalls"])
def test_scheduler_cleans_archives_while_redis_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, redis_stalls: bool
) -> None:
    stop = asyncio.Event()
    redis_requested = asyncio.Event()
    archives_cleaned = asyncio.Event()
    service = MagicMock()
    service.pending_ids.return_value = [uuid4()]
    cleanup = MagicMock()
    cleanup.cleanup_orphans.side_effect = archives_cleaned.set
    queue = MagicMock()
    queue.close = AsyncMock()

    async def load_job(_queue: object, _job_id: str | UUID) -> None:
        redis_requested.set()
        if redis_stalls:
            await asyncio.Event().wait()
        raise RedisConnectionError("Redis is unavailable")

    monkeypatch.setattr(scheduler, "Queue", lambda *args: queue)
    monkeypatch.setattr(scheduler.Job, "fromId", load_job)
    monkeypatch.setattr(scheduler, "shutdown_event", lambda: stop)
    monkeypatch.setattr(scheduler.asyncio, "to_thread", immediate_thread)
    settings = SimpleNamespace(redis_host="redis", redis_port=6379, scheduler_interval_seconds=60)

    async def run_until_cleaned() -> None:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(scheduler.run_scheduler(settings, service, cleanup))
            await redis_requested.wait()
            await archives_cleaned.wait()
            stop.set()

    asyncio.run(asyncio.wait_for(run_until_cleaned(), timeout=2))

    cleanup.cleanup_completed.assert_called_once()
    cleanup.cleanup_orphans.assert_called_once()
    queue.close.assert_awaited_once()


@pytest.mark.parametrize("status", ["completed", "failed"])
def test_processor_marks_failed_import_as_failed_queue_job(
    monkeypatch: pytest.MonkeyPatch, status: str,
) -> None:
    import_id = uuid4()
    service = MagicMock()
    service.get_import.return_value = {"status": status}
    import_processor = MagicMock()
    worker = MagicMock()
    worker.close = AsyncMock()
    captured = {}

    def make_worker(name, callback, options):
        captured["queue_name"] = name
        captured["callback"] = callback
        return worker

    stop = MagicMock()
    stop.wait = AsyncMock()
    monkeypatch.setattr(processor, "Worker", make_worker)
    monkeypatch.setattr(processor, "shutdown_event", lambda: stop)
    monkeypatch.setattr(processor.asyncio, "to_thread", immediate_thread)
    settings = SimpleNamespace(redis_host="redis", redis_port=6379)
    finish(processor.run_processor(settings, service, import_processor))

    assert captured["queue_name"] == "ALLURE_IMPORTS_QUEUE"
    callback = captured["callback"]
    job = SimpleNamespace(data={"importId": str(import_id)})
    if status == "failed":
        with pytest.raises(RuntimeError, match="Ошибка обработки"):
            callback(job, "token").send(None)
    else:
        finish(callback(job, "token"))
    import_processor.process_import.assert_called_once_with(import_id)
    worker.close.assert_awaited_once()
