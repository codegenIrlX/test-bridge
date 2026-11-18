# Согласованные решения по стилю

> Подтверждённые правила проекта TestBridge. Перед реализацией агент должен сверяться с этим файлом.

## Python и инфраструктура

- **Python**: 3.12+
- **FastAPI**: 0.115+
- **SQLAlchemy**: 2.x (declarative, `Mapped`, `mapped_column`)
- **Alembic**: 1.13+
- **Pydantic**: v2 (`str | None`, `model_dump()`, `Field(...)`)
- **psycopg**: 3.x (binary)
- **BullMQ**: через Redis, имя очереди `ALLURE_IMPORTS_QUEUE`
- **PyYAML**: конфигурация

## Форматирование и lint

- **ruff**: `ruff check .`, строка 100 символов, `target-version = "py312"`
- Одиночные кавычки в Python по конвенции проекта

## Именование

- **JSON API**: `camelCase` (через `alias=` в Pydantic)
- **Python/БД**: `snake_case`
- **Очереди**: `UPPER_SNAKE_CASE` + `_QUEUE`

## DTO

- Размещать в домене-владельце, по одному классу в файле
- Каждый DTO — краткий docstring на русском
- Каждое поле: `Field(description=...)` с ограничениями валидации
- Пример:
  ```python
  class ImportResponseDto(BaseModel):
      """Состояние принятого импорта."""
      import_id: UUID = Field(alias="importId", description="Идентификатор импорта")
  ```

## Ошибки API

- Все HTTP API возвращают:
  ```json
  {"error": {"code": "...", "message": "...", "details": []}}
  ```
- Централизовать преобразование на транспортной границе

## SQLAlchemy и БД

- Общий `DeclarativeBase` с `MetaData.naming_convention`
- Имена: `pk__<table>`, `fk__<table>__<columns>__<ref>`, `uq__<table>__<columns>`, `ck__<table>__<purpose>`, `ix__<table>__<columns>`
- UUID-mixin для первичных ключей
- Комментарии к таблицам и колонкам — в схеме
- Миграции самодостаточные, проверять автогенерацию вручную

## Docstring и комментарии

- Краткие docstring по фактическому назначению
- Комментарии — причины решений и неочевидные вычисления
- Использовать `human-writing` skill

## Async

- Выбирать sync/async по фактическому стеку
- Учитывать блокирующие вызовы, отмену и жизненный цикл ресурсов

## Структура кода

- `metrics_srv/domains/<домен>/` — доменные модули
- `api.py` — FastAPI router
- `dto/` — Pydantic модели (read/write по поддиректориям)
- `models.py` — SQLAlchemy модели
- `services.py` — бизнес-логика
- `repository.py` — доступ к данным
- `tests/` — pytest, `testpaths = ["tests"]`
