"""HTTP-маршруты, авторизация и ограничения загрузки импорта."""

from __future__ import annotations

import hmac
from collections.abc import Callable
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from fastapi import Path as ApiPath
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import AfterValidator
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from metrics_srv.config import Settings
from metrics_srv.domains.imports.archive_storage import ArchiveTooLargeError
from metrics_srv.domains.imports.dto.read.error_response import ErrorResponseDto
from metrics_srv.domains.imports.dto.read.import_response import ImportResponseDto
from metrics_srv.domains.imports.repository import ImportMetadata
from metrics_srv.domains.imports.services import IdempotencyConflictError, ImportService

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


def error_response(status: int, code: str, message: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"error": {"code": code, "message": message, "details": []}},
        headers=headers,
    )


def _validate_pg_text(value: str) -> str:
    if "\x00" in value:
        raise ValueError("Поле содержит символ, который PostgreSQL не принимает")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("Поле содержит символ, который PostgreSQL не принимает") from exc
    return value


FormText = Annotated[str, AfterValidator(_validate_pg_text)]


def _blank_as_none(value: str | None) -> str | None:
    return value if value is not None and value.strip() else None


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
            response = error_response(401, "unauthorized", "Недействительный сервисный токен", {"WWW-Authenticate": "Bearer"})
            await response(scope, receive, send)
            return
        limit = self.settings.max_archive_bytes + 1024 * 1024
        length = headers.get(b"content-length")
        if length is not None:
            try:
                if int(length) < 0:
                    raise ValueError
                if int(length) > limit:
                    response = error_response(413, "archive_too_large", "Архив превышает допустимый размер")
                    await response(scope, receive, send)
                    return
            except ValueError:
                response = error_response(400, "http_error", "Некорректный размер запроса")
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


def service_authorization(
    settings: Settings,
) -> Callable[[HTTPAuthorizationCredentials | None], None]:
    """Создаёт зависимость FastAPI для проверки сервисного токена."""

    def authorize(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> None:
        if credentials is None or not hmac.compare_digest(
            credentials.credentials.encode(), settings.api_token.encode()
        ):
            raise HTTPException(401, "Недействительный сервисный токен", headers={"WWW-Authenticate": "Bearer"})

    return authorize


def create_router(settings: Settings, service: ImportService) -> APIRouter:
    """Подключить маршруты импорта и проверку сервисного токена."""
    router = APIRouter()
    authorize = service_authorization(settings)

    @router.post(
        "/api/v1/imports", response_model=ImportResponseDto, status_code=202,
        summary="Принять ZIP с результатами Allure",
        responses={
            **{k: ERROR_RESPONSES[k] for k in (400, 401, 413, 422, 500)},
            409: {"model": ErrorResponseDto, "description": "Этот ключ уже принят для другого ZIP"},
        },
        dependencies=[Depends(authorize)],
    )
    def upload_import(
        file: Annotated[UploadFile, File(description="ZIP с результатами Allure")],
        idempotency_key: Annotated[str | None, Header(
            alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$",
            description="Стабильный ключ для повторной отправки того же ZIP",
        )] = None,
        project: Annotated[FormText | None, Form(
            max_length=255, description="Проект Jenkins"
        )] = None,
        job: Annotated[FormText | None, Form(
            max_length=255, description="Полное имя задания Jenkins"
        )] = None,
        build_number: Annotated[FormText | None, Form(
            alias="buildNumber", max_length=255, description="Номер сборки Jenkins"
        )] = None,
        build_url: Annotated[FormText | None, Form(
            alias="buildUrl", max_length=2048, description="Ссылка на сборку Jenkins"
        )] = None,
        revision: Annotated[FormText | None, Form(
            max_length=255, description="Ревизия исходного кода сборки"
        )] = None,
    ) -> ImportResponseDto:
        metadata: ImportMetadata = {
            "project": _blank_as_none(project),
            "job": _blank_as_none(job),
            "build_number": _blank_as_none(build_number),
            "build_url": _blank_as_none(build_url),
            "revision": _blank_as_none(revision),
        }
        try:
            row = service.upload(file.file, idempotency_key, metadata)
        except ArchiveTooLargeError as exc:
            raise HTTPException(413, "Архив превышает допустимый размер") from exc
        except IdempotencyConflictError as exc:
            raise HTTPException(409, "Этот ключ уже принят для другого ZIP") from exc
        finally:
            file.file.close()
        return ImportResponseDto(
            importId=row["id"], status=row["status"], errorMessage=row["error_message"],
            archiveDeleted=row["archive_deleted"],
        )

    @router.get(
        "/api/v1/imports/{import_id}", response_model=ImportResponseDto,
        summary="Получить состояние импорта",
        responses={k: ERROR_RESPONSES[k] for k in (401, 404, 422, 500)},
        dependencies=[Depends(authorize)],
    )
    def read_import(import_id: Annotated[UUID, ApiPath(description="Идентификатор импорта")]) -> ImportResponseDto:
        row = service.get_import(import_id)
        if row is None:
            raise HTTPException(404, "Импорт не найден")
        return ImportResponseDto(
            importId=row["id"], status=row["status"], errorMessage=row["error_message"],
            archiveDeleted=row["archive_deleted"],
        )

    @router.post(
        "/api/v1/imports/{import_id}/retry", response_model=ImportResponseDto, status_code=202,
        summary="Повторить неуспешный импорт",
        responses={k: ERROR_RESPONSES[k] for k in (401, 404, 409, 422, 500)},
        dependencies=[Depends(authorize)],
    )
    def retry_import(import_id: Annotated[UUID, ApiPath(description="Идентификатор импорта")]) -> ImportResponseDto:
        result = service.retry_import(import_id)
        if result == "not_found":
            raise HTTPException(404, "Импорт не найден")
        if result == "conflict":
            raise HTTPException(409, "Импорт нельзя запустить повторно")
        return ImportResponseDto(importId=import_id, status="pending")

    return router
