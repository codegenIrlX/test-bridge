"""Ответ API при ошибке."""

from pydantic import BaseModel, Field

from metrics_srv.domains.imports.dto.read.error_detail import ErrorDetailDto


class ErrorResponseDto(BaseModel):
    """Ответ с описанием ошибки запроса."""

    error: ErrorDetailDto = Field(description="Сведения об ошибке")
