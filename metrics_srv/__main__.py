"""Точки запуска API, Scheduler, Processor и миграций."""

import argparse
import asyncio
import logging

import uvicorn

from metrics_srv.config import load_settings


def main() -> None:
    """Запустить выбранную роль сервиса."""
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=("api", "scheduler", "processor", "migrate"))
    role = parser.parse_args().role
    logging.basicConfig(level=logging.INFO)
    settings = load_settings()
    if role == "api":
        from metrics_srv.api import create_app

        uvicorn.run(create_app(settings), host="0.0.0.0", port=8000)
    elif role == "migrate":
        from metrics_srv.migrations import upgrade_database

        upgrade_database(settings)
    else:
        from metrics_srv.background import run_processor, run_scheduler

        asyncio.run(run_scheduler(settings) if role == "scheduler" else run_processor(settings))


if __name__ == "__main__":
    main()
