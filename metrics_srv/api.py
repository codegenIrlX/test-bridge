"""Фабрика FastAPI и обработчики ошибок платформы."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from metrics_srv.config import Settings, load_settings
from metrics_srv.db.session import create_database_engine, create_session_factory
from metrics_srv.domains.imports.analytics import MetricsReader
from metrics_srv.domains.imports.api import (
    ERROR_RESPONSES,
    UploadGuard,
    create_router,
    error_response,
    service_authorization,
)
from metrics_srv.domains.imports.dto.read.error_response import ErrorResponseDto
from metrics_srv.domains.imports.repository import ImportRepository
from metrics_srv.domains.imports.services import ImportService

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    service: ImportService | None = None,
    *,
    metrics_reader: MetricsReader | None = None,
) -> FastAPI:
    """Собрать приложение и закрыть принадлежащий ему движок при остановке."""
    settings = settings or load_settings()
    engine = None
    if service is None:
        engine = create_database_engine(settings)
        sessions = create_session_factory(engine)
        service = ImportService(settings, sessions, ImportRepository())
        metrics_reader = metrics_reader or MetricsReader(settings, sessions)

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
    if metrics_reader is not None:
        _register_metrics(app, settings, metrics_reader)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {
            401: "unauthorized", 404: "not_found", 409: "conflict",
            413: "archive_too_large", 503: "metrics_unavailable",
        }.get(exc.status_code, "http_error")
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


def _register_metrics(app: FastAPI, settings: Settings, reader: MetricsReader) -> None:
    @app.get(
        "/metrics", response_class=PlainTextResponse,
        summary="Срез аналитики Allure для Prometheus",
        dependencies=[Depends(service_authorization(settings))],
        responses={
            401: ERROR_RESPONSES[401],
            503: {"model": ErrorResponseDto, "description": "Источник метрик недоступен"},
        },
    )
    def read_metrics() -> PlainTextResponse:
        try:
            payload = reader.render()
        except SQLAlchemyError as exc:
            logger.warning("Не удалось собрать метрики: %s", type(exc).__name__)
            raise HTTPException(503, "Источник метрик недоступен") from exc
        return PlainTextResponse(payload, media_type="text/plain; version=0.0.4")
