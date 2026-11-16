from contextlib import nullcontext
from http import HTTPStatus
from typing import Callable, ContextManager, Optional

import httpx


def assert_status_code(
    response: httpx.Response,
    expected_status: HTTPStatus,
    *,
    step_factory: Optional[Callable[[str], ContextManager[object]]] = None,
) -> None:
    """Проверяет точное соответствие HTTP-статуса."""
    step = step_factory("Проверить HTTP-статус") if step_factory else nullcontext()
    with step:
        if response.status_code != expected_status.value:
            raise AssertionError(
                f"Ожидался HTTP-статус {expected_status.value}, получен {response.status_code}"
            )
