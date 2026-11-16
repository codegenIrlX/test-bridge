from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from metrics_srv import background


def finish(coroutine):
    with pytest.raises(StopIteration):
        coroutine.send(None)


async def immediate_thread(function, *args):
    return function(*args)


async def immediate_wait(awaitable, *, timeout):
    return await awaitable


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
def test_scheduler_retains_history_and_requeues_terminal_jobs(monkeypatch, state, should_remove):
    import_id = uuid4()
    repository = MagicMock()
    repository.pending_ids.return_value = [import_id]
    queue = MagicMock()
    queue.add = AsyncMock()
    queue.close = AsyncMock()
    job = MagicMock()
    job.getState = AsyncMock(return_value=state)
    job.remove = AsyncMock()
    stop = MagicMock()
    stop.is_set.side_effect = [False, True]
    stop.wait = AsyncMock()
    monkeypatch.setattr(background, "Repository", lambda settings: repository)
    make_queue = MagicMock(return_value=queue)
    monkeypatch.setattr(background, "Queue", make_queue)
    monkeypatch.setattr(background.Job, "fromId", AsyncMock(return_value=job if state else None))
    monkeypatch.setattr(background, "_shutdown_event", lambda: stop)
    monkeypatch.setattr(background.asyncio, "to_thread", immediate_thread)
    monkeypatch.setattr(background.asyncio, "wait_for", immediate_wait)
    settings = SimpleNamespace(redis_host="redis", redis_port=6379, scheduler_interval_seconds=1)

    finish(background.run_scheduler(settings))

    assert make_queue.call_args.args[0] == "ALLURE_IMPORTS_QUEUE"
    assert job.remove.await_count == int(should_remove)
    name, payload, options = queue.add.await_args.args
    assert name == "parse"
    assert payload == {"importId": str(import_id)}
    assert options["jobId"] == str(import_id)
    assert options["removeOnComplete"] == {"count": 100}
    assert options["removeOnFail"] == {"count": 100}
    queue.close.assert_awaited_once()


@pytest.mark.parametrize("status", ["completed", "failed"])
def test_processor_marks_failed_import_as_failed_queue_job(monkeypatch, status):
    import_id = uuid4()
    repository = MagicMock()
    repository.get_import.return_value = {"status": status}
    worker = MagicMock()
    worker.close = AsyncMock()
    captured = {}

    def make_worker(name, callback, options):
        captured["queue_name"] = name
        captured["callback"] = callback
        return worker

    stop = MagicMock()
    stop.wait = AsyncMock()
    monkeypatch.setattr(background, "Repository", lambda settings: repository)
    monkeypatch.setattr(background, "Worker", make_worker)
    monkeypatch.setattr(background, "_shutdown_event", lambda: stop)
    monkeypatch.setattr(background.asyncio, "to_thread", immediate_thread)
    settings = SimpleNamespace(redis_host="redis", redis_port=6379)
    finish(background.run_processor(settings))

    assert captured["queue_name"] == "ALLURE_IMPORTS_QUEUE"
    callback = captured["callback"]
    job = SimpleNamespace(data={"importId": str(import_id)})
    if status == "failed":
        with pytest.raises(RuntimeError, match="Ошибка обработки"):
            callback(job, "token").send(None)
    else:
        finish(callback(job, "token"))
    repository.process_import.assert_called_once_with(import_id)
    worker.close.assert_awaited_once()
