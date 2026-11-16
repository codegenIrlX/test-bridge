"""Сведения об ошибке API."""

from pydantic import BaseModel, Field


class ErrorDetailDto(BaseModel):
    """Код и безопасное описание ошибки."""

    code: str = Field(description="Машинный код ошибки")
    message: str = Field(description="Описание ошибки")
    details: list[str] = Field(default_factory=list, description="Дополнительные сведения")
