"""Ответ с состоянием импорта."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class ImportResponseDto(BaseModel):
    """Состояние принятого импорта."""

    import_id: UUID = Field(alias="importId", description="Идентификатор импорта")
    status: Literal["pending", "processing", "completed", "failed"] = Field(
        description="Состояние обработки архива"
    )
    error_message: str | None = Field(
        default=None, alias="errorMessage", description="Описание ошибки обработки"
    )
    archive_deleted: bool = Field(
        default=False, alias="archiveDeleted", description="Признак удаления архива"
    )
