"""Фабрика FastAPI и обработчики ошибок платформы."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from metrics_srv.config import Settings, load_settings
from metrics_srv.db.session import create_database_engine, create_session_factory
from metrics_srv.domains.imports.api import UploadGuard, create_router, error_response
from metrics_srv.domains.imports.repository import ImportRepository
from metrics_srv.domains.imports.services import ImportService

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, service: ImportService | None = None) -> FastAPI:
    """Собрать приложение и закрыть принадлежащий ему движок при остановке."""
    settings = settings or load_settings()
    engine = None
    if service is None:
        engine = create_database_engine(settings)
        service = ImportService(settings, create_session_factory(engine), ImportRepository())

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if engine is not None:
                engine.dispose()

    app = FastAPI(title="TestBridge Metrics Service", version="0.1.0", lifespan=lifespan)
    app.add_middleware(UploadGuard, settings=settings)
    app.include_router(create_router(settings, service))

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {401: "unauthorized", 404: "not_found", 409: "conflict", 413: "archive_too_large"}.get(
            exc.status_code, "http_error"
        )
        message = exc.detail if isinstance(exc.detail, str) else "Ошибка запроса"
        return error_response(exc.status_code, code, message, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(422, "validation_error", "Некорректный запрос")

    @app.exception_handler(Exception)
    async def unexpected_error(_request: Request, exc: Exception) -> JSONResponse:
        logger.error("Необработанная ошибка API: %s", type(exc).__name__)
        return error_response(500, "internal_error", "Внутренняя ошибка сервиса")

    return app
