# Проверка прогона Jenkins на рабочем TestBridge

Проследить последний прогон пользователя от Jenkins до рабочего `testbridge`. Проверка выполняется на чтение в существующих Jenkins, PostgreSQL и VictoriaMetrics.

- [x] **Tech-lead · Jenkins.** Сборка `qa-lab-python-example #37` от 17 ноября 2026, 15:23 МСК, завершилась с `FAILURE` и кодом pytest `1`. В JUnit восемь успешных тестов и одна ошибка на подготовке `test_connect_etth_service`. В стеке есть `tests/helpers/etth.py:132`, где выбор порта завершается ошибкой при отсутствии свободного согласованного ETTH-порта. Отправка Allure получила HTTP `202`, а журнал и артефакт ответа содержат один `importId` `acd37f5f-7304-40c1-9416-3f3ea8141617`.
- [x] **Tech-lead · TestBridge.** Импорт завершён с первой попытки, API вернул `completed` и `archiveDeleted: true`. SHA-256 ZIP совпал с записью в БД, на томе файл отсутствует. С исходным Allure совпали статусы, времена, `historyId` и вложенность шагов. Сохранены 9 результатов и 51 шаг. VictoriaMetrics и Grafana для `qa-lab-test-framework` показали 1 прогон, 8 `passed`, 1 `failed`, среднее время 2,35 с. Источник метрик вернул `up=1`.
