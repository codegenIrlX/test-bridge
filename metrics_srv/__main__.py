"""Точки запуска API, Scheduler, Processor и миграций."""

import argparse
import asyncio
import logging

from metrics_srv.config import load_settings
from metrics_srv.db.session import create_database_engine, create_session_factory
from metrics_srv.domains.imports.repository import ImportRepository
from metrics_srv.domains.imports.services import ImportService


def main() -> None:
    """Запустить выбранную роль сервиса."""
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=("api", "scheduler", "processor", "migrate"))
    role = parser.parse_args().role
    logging.basicConfig(level=logging.INFO)
    settings = load_settings()
    if role == "migrate":
        from metrics_srv.migrations import upgrade_database

        upgrade_database(settings)
        return

    engine = create_database_engine(settings)
    try:
        sessions = create_session_factory(engine)
        repository = ImportRepository()
        service = ImportService(settings, sessions, repository)
        if role == "api":
            import uvicorn

            from metrics_srv.api import create_app
            from metrics_srv.domains.imports.analytics import MetricsReader

            app = create_app(settings, service, metrics_reader=MetricsReader(settings, sessions))
            uvicorn.run(app, host="0.0.0.0", port=8000)
        elif role == "scheduler":
            from metrics_srv.domains.imports.cleanup import ArchiveCleanup
            from metrics_srv.workers.scheduler import run_scheduler

            asyncio.run(run_scheduler(settings, service, ArchiveCleanup(settings, sessions, repository)))
        else:
            from metrics_srv.domains.imports.processing import ImportProcessor
            from metrics_srv.workers.processor import run_processor

            asyncio.run(run_processor(settings, service, ImportProcessor(settings, engine, repository)))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
