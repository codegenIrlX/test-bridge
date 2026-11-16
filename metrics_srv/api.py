"""HTTP API приёма архивов и просмотра состояния обработки."""

from __future__ import annotations

import hmac
import logging
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi import Path as ApiPath
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from metrics_srv.config import Settings, load_settings
from metrics_srv.domains.imports.dto.read.error_response import ErrorResponseDto
from metrics_srv.domains.imports.dto.read.import_response import ImportResponseDto
from metrics_srv.filelocks import lock_upload
from metrics_srv.repository import Repository

logger = logging.getLogger(__name__)
bearer = HTTPBearer(auto_error=False)
ERROR_RESPONSES = {
    400: {"model": ErrorResponseDto, "description": "Некорректное тело запроса"},
    401: {"model": ErrorResponseDto, "description": "Требуется сервисный токен"},
    404: {"model": ErrorResponseDto, "description": "Импорт не найден"},
    409: {"model": ErrorResponseDto, "description": "Импорт нельзя запустить повторно"},
    413: {"model": ErrorResponseDto, "description": "Архив превышает допустимый размер"},
    422: {"model": ErrorResponseDto, "description": "Некорректный запрос"},
    500: {"model": ErrorResponseDto, "description": "Внутренняя ошибка сервиса"},
}


def _error(status: int, code: str, message: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"error": {"code": code, "message": message, "details": []}},
        headers=headers,
    )


class UploadGuard:
    """Проверять токен и размер тела до разбора multipart."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] != "/api/v1/imports":
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        authorization = headers.get(b"authorization", b"")
        scheme, _, token = authorization.partition(b" ")
        if scheme.lower() != b"bearer" or not hmac.compare_digest(token, self.settings.api_token.encode()):
            response = _error(401, "unauthorized", "Недействительный сервисный токен", {"WWW-Authenticate": "Bearer"})
            await response(scope, receive, send)
            return
        limit = self.settings.max_archive_bytes + 1024 * 1024
        length = headers.get(b"content-length")
        if length is not None:
            try:
                if int(length) < 0:
                    raise ValueError
                if int(length) > limit:
                    response = _error(413, "archive_too_large", "Архив превышает допустимый размер")
                    await response(scope, receive, send)
                    return
            except ValueError:
                response = _error(400, "http_error", "Некорректный размер запроса")
                await response(scope, receive, send)
                return
        size = 0

        async def limited_receive() -> Message:
            nonlocal size
            message = await receive()
            if message["type"] == "http.request":
                size += len(message.get("body", b""))
                if size > limit:
                    raise StarletteHTTPException(413, "Архив превышает допустимый размер")
            return message

        await self.app(scope, limited_receive, send)


def create_app(settings: Settings | None = None, repository: Repository | None = None) -> FastAPI:
    """Создать API и зарегистрировать его обработчики."""
    settings = settings or load_settings()
    repository = repository or Repository(settings)
    app = FastAPI(title="TestBridge Metrics Service", version="0.1.0")
    app.add_middleware(UploadGuard, settings=settings)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {401: "unauthorized", 404: "not_found", 409: "conflict", 413: "archive_too_large"}.get(
            exc.status_code, "http_error"
        )
        message = exc.detail if isinstance(exc.detail, str) else "Ошибка запроса"
        return _error(exc.status_code, code, message, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _error(422, "validation_error", "Некорректный запрос")

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.error("Необработанная ошибка API: %s", type(exc).__name__)
        return _error(500, "internal_error", "Внутренняя ошибка сервиса")

    def authorize(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> None:
        if credentials is None or not hmac.compare_digest(
            credentials.credentials.encode(), settings.api_token.encode()
        ):
            raise HTTPException(401, "Недействительный сервисный токен", headers={"WWW-Authenticate": "Bearer"})

    @app.post(
        "/api/v1/imports", response_model=ImportResponseDto, status_code=202,
        summary="Принять ZIP с результатами Allure",
        responses={k: ERROR_RESPONSES[k] for k in (400, 401, 413, 422, 500)},
        dependencies=[Depends(authorize)],
    )
    def upload_import(file: Annotated[UploadFile, File(description="ZIP с результатами Allure")]) -> ImportResponseDto:
        settings.storage_path.mkdir(parents=True, exist_ok=True)
        import_id = uuid4()
        target = settings.storage_path / f"{import_id}.zip"
        temporary: Path | None = None
        try:
            with NamedTemporaryFile(dir=settings.storage_path, prefix="upload-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                lock_upload(stream)
                size = 0
                while chunk := file.file.read(1024 * 1024):
                    size += len(chunk)
                    if size > settings.max_archive_bytes:
                        raise HTTPException(413, "Архив превышает допустимый размер")
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
                os.utime(temporary, None)
            temporary.replace(target)
            repository.create_import(import_id)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            file.file.close()
        return ImportResponseDto(importId=import_id, status="pending")

    @app.get(
        "/api/v1/imports/{import_id}", response_model=ImportResponseDto,
        summary="Получить состояние импорта",
        responses={k: ERROR_RESPONSES[k] for k in (401, 404, 422, 500)},
        dependencies=[Depends(authorize)],
    )
    def read_import(import_id: Annotated[UUID, ApiPath(description="Идентификатор импорта")]) -> ImportResponseDto:
        row = repository.get_import(import_id)
        if row is None:
            raise HTTPException(404, "Импорт не найден")
        return ImportResponseDto(
            importId=row["id"], status=row["status"], errorMessage=row["error_message"],
            archiveDeleted=row["archive_deleted"],
        )

    @app.post(
        "/api/v1/imports/{import_id}/retry", response_model=ImportResponseDto, status_code=202,
        summary="Повторить неуспешный импорт",
        responses={k: ERROR_RESPONSES[k] for k in (401, 404, 409, 422, 500)},
        dependencies=[Depends(authorize)],
    )
    def retry_import(import_id: Annotated[UUID, ApiPath(description="Идентификатор импорта")]) -> ImportResponseDto:
        result = repository.retry_import(import_id)
        if result == "not_found":
            raise HTTPException(404, "Импорт не найден")
        if result == "conflict":
            raise HTTPException(409, "Импорт нельзя запустить повторно")
        return ImportResponseDto(importId=import_id, status="pending")

    return app
