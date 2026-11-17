# Первый запуск TestBridge на новых томах

Цель — оставить TestBridge запущенным в Docker-контексте `desktop-linux` как отдельный Compose-проект `testbridge-orm`. API доступен с Windows и из контейнера агента Jenkins. Старые тома TestBridge сохраняются.

- [x] **Tech-lead · Конфигурация.** Проверены окружение `development`, итоговый Compose и свободные порты. PostgreSQL, Redis и архивы используют новые тома `testbridge-orm_postgres-data`, `testbridge-orm_redis-data` и `testbridge-orm_import-data`.
- [x] **Tech-lead · Старт.** Команда `docker --context desktop-linux compose -p testbridge-orm -f compose.yaml up -d --build` завершилась успешно. Мигратор вышел с кодом 0, API, Scheduler и Processor запущены.
- [x] **Tech-lead · Доступность.** В PostgreSQL применена ревизия `0001_imports_orm`, Redis ответил `PONG`. OpenAPI вернул HTTP 200 с Windows и из агента Jenkins по `http://host.docker.internal:8000/openapi.json`. Bull Board вернул HTTP 200 после входа.
- [x] **Tech-lead · Импорт.** Синтетический ZIP получил `202` и дошёл до `completed`. В PostgreSQL сохранены один результат и два шага. API вернул `archiveDeleted: true`, отсутствие файла подтверждено в общем томе. Проверочный импорт `40850ac1-2d3c-4922-ab08-f3b11e87a8b6` сохранён. Проект оставлен запущенным.
