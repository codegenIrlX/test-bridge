# Карта проекта TestBridge

> Создано на основе реального кода. Перед работой агент обязан сверяться с этой картой.

## Общая структура

```
testbridge-metrics-srv/
├── metrics_srv/              # Исходный код сервиса
│   ├── __main__.py           # Точки запуска: api, scheduler, processor, migrate
│   ├── api.py                # Фабрика FastAPI, обработчики ошибок
│   ├── config.py             # Загрузка YAML-конфигурации (config.yaml + secrets.yaml)
│   ├── db/
│   │   ├── base.py           # DeclarativeBase + naming_convention
│   │   ├── session.py        # Engine + session factory
│   │   └── mixins.py         # UUIDPrimaryKeyMixin
│   ├── domains/
│   │   └── imports/          # Единственный домен (приём Allure-архивов)
│   │       ├── api.py        # FastAPI router (/api/v1/imports)
│   │       ├── models.py     # SQLAlchemy: Import, Result, Step
│   │       ├── services.py   # ImportService (бизнес-логика)
│   │       ├── repository.py # ImportRepository (доступ к БД)
│   │       ├── processing.py # ImportProcessor (разбор Allure)
│   │       ├── parser.py     # Парсер allure-result.zip
│   │       ├── analytics.py  # MetricsReader (аналитика для Grafana)
│   │       ├── cleanup.py    # ArchiveCleanup (фоновая очистка)
│   │       ├── archive_storage.py  # Хранение архивов на диске
│   │       └── dto/
│   │           └── read/
│   │               ├── import_response.py   # ImportResponseDto
│   │               ├── error_response.py    # ErrorResponseDto
│   │               └── error_detail.py      # ErrorDetailDto
│   ├── migrations/           # Alembic
│   │   ├── env.py
│   │   └── versions/
│   │       ├── 0001_imports_orm.py
│   │       ├── 0002_failed_archive_retention.py
│   │       ├── 0003_run_analytics.py
│   │       └── 0004_grafana_read_model.py
│   └── workers/
│       ├── scheduler.py      # asyncio планировщик
│       ├── processor.py      # BullMQ worker
│       └── runtime.py        # Общая runtime-логика
├── tests/                    # Unit и интеграционные тесты
│   ├── test_api.py           # HTTP API (TestClient)
│   ├── test_parser.py        # Парсер Allure
│   ├── test_repository_unit.py  # Репозиторий
│   ├── test_database_postgres.py # БД (PostgreSQL)
│   ├── test_config.py        # Конфигурация
│   ├── test_board_queue.py   # Очередь BullMQ
│   └── test_filelocks.py     # Файловые блокировки
├── deploy/                   # Эксплуатационные файлы
│   ├── postgres-entrypoint.sh
│   ├── otel-collector.yaml
│   ├── grafana/              # Dashboards и provisioning
│   ├── init_postgres.py
│   ├── init_board.py
│   ├── bootstrap_secret.py
│   └── analytics_bootstrap.py
├── compose.yaml              # Docker Compose: postgres, redis, api, scheduler, processor, grafana, victoriametrics
├── config.yaml               # Публичная конфигурация
├── secrets.yaml              # Приватные значения (в .gitignore)
├── pyproject.toml            # Зависимости, pytest, ruff
├── Dockerfile
└── AGENTS.md                 # Правила проекта
```

## Компоненты по ролям

### API и транспорт

| Путь | Роль | Особенности |
|------|------|-------------|
| `metrics_srv/api.py` | Фабрика FastAPI | lifespan, error handlers, DI |
| `metrics_srv/domains/imports/api.py` | Router | `/api/v1/imports`, bearer auth, UploadGuard, multipart |
| `metrics_srv/config.py` | Settings dataclass | YAML merge, frozen, slots |

### База данных

| Путь | Роль |
|------|------|
| `metrics_srv/db/base.py` | DeclarativeBase + naming_convention |
| `metrics_srv/db/session.py` | Engine, session factory |
| `metrics_srv/db/mixins.py` | UUIDPrimaryKeyMixin |
| `metrics_srv/domains/imports/models.py` | Import, Result, Step (SQLAlchemy) |

### Домен импортов

| Путь | Роль | Пример |
|------|------|--------|
| `services.py` | Бизнес-логика | `ImportService.create_import()`, `get_import()` |
| `repository.py` | Доступ к БД | `ImportRepository.create_import()`, `lock_import()` |
| `processing.py` | Обработка архивов | `ImportProcessor.process_import()` |
| `parser.py` | Разбор Allure | `parse_allure_results()` |
| `analytics.py` | Чтение метрик | `MetricsReader` для Grafana |
| `cleanup.py` | Фоновая очистка | `ArchiveCleanup.remove_old_archives()` |

### DTO

| Путь | Роль | Пример |
|------|------|--------|
| `dto/read/import_response.py` | Ответ GET /imports/{id} | `ImportResponseDto` |
| `dto/read/error_response.py` | Единый формат ошибок | `ErrorResponseDto` |

### Workers

| Путь | Роль |
|------|------|
| `workers/scheduler.py` | Периодический запуск cleanup |
| `workers/processor.py` | BullMQ worker для импортов |
| `workers/runtime.py` | `QUEUE_NAME = "ALLURE_IMPORTS_QUEUE"`, shutdown_event |

### Тесты

| Путь | Роль | Зависимости |
|------|------|-------------|
| `tests/test_api.py` | HTTP API + интеграция | TestClient, FakeRepository |
| `tests/test_parser.py` | Парсер Allure | Временные ZIP-файлы |
| `tests/test_repository_unit.py` | Репозиторий | SQLite in-memory |
| `tests/test_database_postgres.py` | PostgreSQL | Требует запущенный postgres |
| `tests/test_config.py` | Конфигурация | Временные YAML |
| `tests/test_board_queue.py` | BullMQ | Требует Redis |
| `tests/test_filelocks.py` | Файловые блокировки | Временные файлы |

## Команды проверки

```bash
# Lint
ruff check .

# Тесты (unit, без внешних зависимостей)
pytest tests/test_config.py tests/test_parser.py tests/test_repository_unit.py -v

# Тесты (с PostgreSQL — требует запущенный compose)
pytest tests/test_database_postgres.py -v

# Тесты (с Redis — требует запущенный compose)
pytest tests/test_board_queue.py -v

# Все тесты
pytest -v

# Запуск платформы
docker compose up -d --build
```

## Зависимости

| Библиотека | Версия | Назначение |
|------------|--------|------------|
| fastapi | 0.115+ | HTTP API |
| uvicorn | 0.30+ | ASGI сервер |
| sqlalchemy | 2.0.23+ | ORM |
| alembic | 1.13+ | Миграции |
| psycopg | 3.2+ | PostgreSQL драйвер |
| bullmq | 2+ | Очередь через Redis |
| pydantic | 2+ | Валидация DTO |
| pyyaml | 6+ | Конфигурация |
| pytest | 8+ | Тесты (dev) |
| httpx | 0.27+ | HTTP клиент для тестов (dev) |
| ruff | 0.9+ | Lint (dev) |

## Инфраструктура (Compose)

| Сервис | Порт | Роль |
|--------|------|------|
| postgres | 5433 | PostgreSQL 17 |
| redis | 6379 | Redis 7 (BullMQ) |
| api | 8000 | FastAPI/uvicorn |
| scheduler | — | asyncio планировщик |
| processor | — | BullMQ worker |
| migrate | — | Одноразовый Alembic |
| grafana | 3000 | Dashboards |
| victoriametrics | 8428 | Метрики |
| bull-board | 3001 | UI очереди |

## Архитектурные принципы

- **Единый образ**: все роли запускаются как `python -m metrics_srv <role>`
- **Domain-driven**: код в `metrics_srv/domains/<домен>/`
- **UUID PK**: все сущности импорта, результата, шага — UUID
- **Naming convention**: в схеме БД через `MetaData`
- **Self-contained migrations**: каждая ревизия Alembic независима
- **Error format**: единый `{error: {code, message, details}}`
