from contextlib import nullcontext
from typing import Callable, ContextManager, Optional, Type, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


def assert_response_dto(
    response: httpx.Response,
    dto_type: Type[T],
    *,
    step_factory: Optional[Callable[[str], ContextManager[object]]] = None,
) -> T:
    """Проверяет JSON-ответ по DTO и возвращает модель."""
    step = step_factory("Проверить соответствие ответа DTO") if step_factory else nullcontext()
    with step:
        try:
            return dto_type.model_validate_json(response.content)
        except ValidationError as error:
            message = f"Ответ не соответствует DTO {dto_type.__name__}. Ошибок: {error.error_count()}."
        # Не добавляем исходную ошибку с данными ответа в отчёт.
        raise AssertionError(message) from None
