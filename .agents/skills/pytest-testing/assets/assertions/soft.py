from contextlib import contextmanager, nullcontext
from types import TracebackType
from typing import Callable, ContextManager, Iterator, List, Optional, Type


class _SoftAssertionFailure(AssertionError):
    """Отмечает несовпадение внутри отчётного шага."""


def _format_failures(messages: List[str]) -> str:
    failures = "\n".join(f"{number}. {message}" for number, message in enumerate(messages, 1))
    return f"Не пройдено проверок: {len(messages)}\n{failures}"


class SoftAssertions:
    """Накапливает несовпадения и завершает блок общей ошибкой."""

    def __init__(self, step_factory: Optional[Callable[[str], ContextManager[object]]] = None) -> None:
        self._messages: List[str] = []
        self._active = False
        self._finished = False
        self._step_factory = step_factory

    def __enter__(self) -> "SoftAssertions":
        if self._active or self._finished:
            raise RuntimeError("Блок soft assertions нельзя использовать повторно")
        self._active = True
        return self

    def __exit__(self, exc_type: Optional[Type[BaseException]], exc_value: Optional[BaseException], traceback: Optional[TracebackType]) -> bool:
        messages = self._messages
        self._messages = []
        self._active = False
        self._finished = True
        if exc_type is None and messages:
            raise AssertionError(_format_failures(messages)) from None
        return False

    def check(self, condition: bool, message: str) -> "SoftAssertions":
        """Добавляет несовпадение с безопасным сообщением."""
        self._ensure_active()
        if not isinstance(condition, bool):
            raise TypeError("Условие проверки должно иметь тип bool")
        if not message.strip():
            raise ValueError("Сообщение проверки не должно быть пустым")
        if not condition:
            self._messages.append(message)
        return self

    @contextmanager
    def step(self, title: str) -> Iterator[None]:
        """Отмечает отчётный шаг при несовпадениях и продолжает проверки."""
        self._ensure_active()
        if not title.strip():
            raise ValueError("Название шага не должно быть пустым")
        first_message = len(self._messages)
        step = self._step_factory(title) if self._step_factory else nullcontext()
        try:
            with step:
                yield
                if self._step_factory and len(self._messages) > first_message:
                    raise _SoftAssertionFailure(_format_failures(self._messages[first_message:])) from None
        except _SoftAssertionFailure:
            # Исключение помечает шаг; несовпадения уже накоплены.
            pass

    def _ensure_active(self) -> None:
        if not self._active:
            raise RuntimeError("Проверки разрешены только внутри блока with SoftAssertions()")
