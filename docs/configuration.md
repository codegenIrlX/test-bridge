# Конфигурация и запуск

Платформа читает настройки из `config.yaml`, а токен API и пароли PostgreSQL, Bull Board и Grafana — из `secrets.yaml`. Перед запуском создайте `secrets.yaml` по образцу `secrets.example.yaml` и задайте собственные значения. Не добавляйте этот файл в Git и не передавайте его в контекст сборки образа.

В обоих файлах раздел `default` задаёт общие значения, а раздел выбранного окружения переопределяет их. Вложенные объекты объединяются по ключам. Окружение выбирается переменной `METRICS_SRV_ENV`; без неё используется `development`. Оба файла должны содержать раздел выбранного окружения, даже если он пуст. При отсутствии или неверном значении обязательных параметров процесс завершает запуск с ошибкой.

`config.yaml` содержит адреса и порты PostgreSQL и Redis, путь к архивам, пределы размера и количества файлов и интервал работы Scheduler. В Docker файл конфигурации монтируется только для чтения. Сервис подготовки копирует `secrets.yaml` в tmpfs с владельцем приложения и правами `0400`. Collector и Grafana получают только свои учётные данные через отдельные файлы. Локально приложение читает `secrets.yaml` из рабочего каталога, внутри контейнера используется `/run/secrets/secrets.yaml`.

## Запуск TestBridge

Единый `compose.yaml` запускает проект `testbridge`, включая OpenTelemetry Collector, VictoriaMetrics и Grafana. API доступен на `8000`. PostgreSQL, Redis и Bull Board доступны с хоста на `127.0.0.1:5433`, `127.0.0.1:6379` и `127.0.0.1:3001`.

```powershell
docker --context desktop-linux compose -p testbridge config --quiet
docker --context desktop-linux compose -p testbridge up -d --build
docker --context desktop-linux compose -p testbridge ps -a
```

Проверьте, что `migrate` завершился с кодом `0`, а API, Scheduler и Processor запущены. Если Compose сообщает об ошибке миграции, проверьте `docker --context desktop-linux compose -p testbridge logs migrate`. `migrate` ждёт готовности PostgreSQL (`service_healthy`) и выполняет `upgrade head`. Запуск трёх процессов зависит от успешного завершения мигратора (`service_completed_successfully`).

Данные PostgreSQL, очередь Redis и входящие архивы хранятся в именованных томах. Для запуска требуются Linux-контейнеры. Временные файлы для инициализации PostgreSQL создаются в отдельном томе tmpfs. Служебный контейнер удерживает тома tmpfs смонтированными между однократной подготовкой и запуском потребителей.

При запуске через Compose задайте `storage.path: /data/imports`. Compose монтирует общий том по этому пути в API, Scheduler и Processor и передаёт ожидаемый путь через `METRICS_SRV_REQUIRED_STORAGE_PATH`. При несовпадении конфигурация отклоняется до приёма архивов. При запуске без этой переменной путь задаётся в YAML.

Вне Compose выполните `python -m metrics_srv migrate` перед запуском приложения. Команда находит ревизии в установленном пакете независимо от рабочего каталога. `config.yaml` читается из рабочего каталога. При отсутствии `/run/secrets/secrets.yaml` там же читается `secrets.yaml`.

Для разработки и интеграционных проверок используйте рабочий `testbridge` и существующий Jenkins. При проверке миграций сверяйте ревизию и схему рабочей базы. Откат `0001_imports_orm` удаляет таблицы импорта, результатов и шагов вместе с их данными.

## Эксплуатация

Если удалить обработанный ZIP не удалось, Scheduler повторит попытку спустя как минимум пять минут и продолжит очистку других архивов.

Через семь дней с перехода импорта в `failed` Scheduler начинает очистку ZIP и сохраняет запись импорта. Ручной повтор возможен, пока файл доступен. После удаления запрос на повтор получает `409`.

Scheduler раз в минуту пишет в журнал `oldest_unfinished_age_seconds` и `archive_free_bytes`. Первое значение — возраст старейшего `pending` или `processing` импорта, второе — свободное место на томе архивов. При пустой очереди возраст отсутствует. Ошибки чтения источников и число архивов с отложенным повтором удаления записываются отдельными предупреждениями.

После изменения `secrets.yaml` обновите копии секретов командой `docker compose run --rm --no-deps secret-bootstrap`. Перезапустите процессы, использующие изменённые настройки. Для Collector и Grafana выполните шаги из раздела ниже. После перезапуска Docker daemon или хоста выполните `docker compose up -d`, чтобы сервисы подготовки снова записали файлы в tmpfs.

Пароль из YAML используется при первоначальном создании базы. Изменение YAML не меняет пароль существующего пользователя PostgreSQL: ротация должна отдельно обновить пароль в БД и секрет приложения. Именованные тома не заменяют резервное копирование.

### Копия и восстановление импортов

1. На исходном экземпляре остановите API, Scheduler и Processor, чтобы зафиксировать состояние приёма и очистки. PostgreSQL оставьте доступным.
2. Сохраните `pg_dump -Fc` базы и согласованную копию каталога архивов. Для незавершённых импортов необходимы ZIP с именами `<importId>.zip`. ZIP неуспешных импортов нужны, если после восстановления требуется ручной повтор.
3. При восстановлении держите приём остановленным. Восстановите дамп в подготовленную PostgreSQL и разложите исходные ZIP в том архивов. Затем запустите мигратор, API и воркеры. Если очередь была потеряна, Scheduler вновь поставит незавершённые импорты в Redis по данным PostgreSQL.
4. Проверьте состояние нескольких импортов и число результатов в PostgreSQL. Архив завершённого импорта удаляется после фиксации результатов.

### Перенос рабочего стенда из `testbridge-orm`

Ревизии `0002_failed_archive_retention` и `0003_run_analytics` обновляют существующую PostgreSQL от `0001_imports_orm`. Для прежних импортов поля Jenkins, статусы и `historyId` остаются пустыми. Из этих импортов Grafana учитывает завершённые прогоны и доступные времена тестов. Статусы для них отображаются как `unknown`.

Рабочий проект использует тома `testbridge`. Тома `testbridge-orm` содержат состояние на момент переноса. Для восстановления после начала приёма используйте актуальную копию рабочего проекта. [Результаты переноса](plan/testbridge-rollout/implementation.md)

### Аналитика и Grafana

В папке **TestBridge** в Grafana находятся дашборды «Результаты Allure», «Качество прогонов», «Длительность тестов» и «Нестабильные тесты». Переменная **Проект** загружает из PostgreSQL все уникальные непустые `imports.project`. Данные на панелях зависят от выбранного проекта и периода. Состояние сбора относится ко всей платформе.

`metrics.allowed_projects` ограничивает набор меток проекта при выгрузке агрегатов в VictoriaMetrics. Остальные проекты собираются под меткой `other`, а прогоны без проекта — под `unknown`.

Пароль администратора Grafana задаёт `grafana.password` в `secrets.yaml`. Если значение переопределено в выбранном `METRICS_SRV_ENV`, используется оно.

Для источника **TestBridge PostgreSQL** задайте `grafana.database_password`. После миграций сервис `analytics-bootstrap` настраивает роль `testbridge_grafana_reader` с чтением представлений `dashboard_imports`, `dashboard_attempts` и `dashboard_final_results`. Grafana получает пароль этой роли из временного файла.

Collector читает `/metrics` API с сервисным токеном и передаёт значения в VictoriaMetrics. Из сети Compose VictoriaMetrics доступна по адресу `http://victoriametrics:8428`, Redis — по адресу `redis:6379`.

| Сервис | Подключение с Windows |
|---|---|
| Grafana | [TestBridge Imports](http://127.0.0.1:3000/d/testbridge-imports), пользователь `admin`, пароль `grafana.password`. Порт меняется через `TESTBRIDGE_GRAFANA_PORT`. |
| VictoriaMetrics | [VMUI](http://127.0.0.1:8428/vmui/) или Prometheus-совместимый HTTP API на `http://127.0.0.1:8428`. |
| Redis | `127.0.0.1:6379`, база `0`, без пароля. Используйте клиент Redis, например Redis Insight. [Bull Board](http://127.0.0.1:3001) показывает очередь `ALLURE_IMPORTS_QUEUE`. |

```powershell
curl.exe -G http://127.0.0.1:8428/api/v1/query --data-urlencode "query=testbridge_runs"
redis-cli -h 127.0.0.1 -p 6379 PING
docker --context desktop-linux compose -p testbridge exec redis redis-cli --scan --pattern 'bull:ALLURE_IMPORTS_QUEUE:*'
```

В VMUI или Grafana Explore запустите `up{job="testbridge"}` для проверки сбора, `testbridge_runs` для числа завершённых импортов и `testbridge_test_results{kind="final"}` для итоговых статусов тестов. Дашборды рассчитывают результаты за выбранный период по времени регистрации импорта в PostgreSQL. Дневные графики используют UTC.

После изменения токена API, пароля администратора или пароля источника PostgreSQL обновите файлы.

```powershell
docker compose run --rm --no-deps secret-bootstrap
docker compose run --rm --no-deps analytics-bootstrap
```

После ротации токена перезапустите `api scheduler processor otel-collector` и проверьте `up{job="testbridge"}`. При изменении пароля примените его к существующей учётной записи, которая хранится в SQLite Grafana.

```powershell
docker compose exec -T grafana sh -ec 'grafana cli --homepath /usr/share/grafana --config /etc/grafana/grafana.ini admin reset-admin-password --password-from-stdin < /run/grafana-auth/password'
```

После смены `grafana.database_password` выполните `docker compose restart grafana`, чтобы Grafana заново прочитала пароль источника.
