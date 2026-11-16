# Именование очередей BullMQ

Цель: закрепить формат UPPER_SNAKE_CASE с суффиксом `_QUEUE` и заменить существующую очередь без потери импортов PostgreSQL.

- [x] Tech-lead: обновить AGENTS.md, metrics_srv/background.py и docs/bull-board.md; 108 строк включая пустые, Ruff пройден.
- [x] Tester: проверить имя очереди Scheduler и Processor и сценарии повторов; 8 тестов прошли в Linux-контейнере.
- [x] Tech-lead: остановить Scheduler и Processor, сверить PostgreSQL, запустить новую очередь, проверить импорт и удалить только старую очередь BullMQ; 6 импортов PostgreSQL сохранены, новая очередь видна в Bull Board.
- [x] Reviewer: проверить изменения на чтение; подтверждённых дефектов не найдено.
