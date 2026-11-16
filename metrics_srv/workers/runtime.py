"""Общие настройки фоновых процессов."""

import asyncio
import signal

QUEUE_NAME = "ALLURE_IMPORTS_QUEUE"


def shutdown_event() -> asyncio.Event:
    """Создать событие остановки по системному сигналу."""
    event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, event.set)
        except NotImplementedError:
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(event.set))
    return event
