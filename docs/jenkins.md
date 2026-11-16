# Отправка результатов из Jenkins

Подготовьте ZIP с исходным каталогом `allure-results` и сохраните его в рабочем каталоге как `allure-results.zip`. Адрес платформы задайте переменной Jenkins `METRICS_SRV_URL`, а сервисный токен сохраните в Jenkins Credentials с идентификатором `metrics-srv-token`. Задайте стабильный и уникальный для контроллера идентификатор `TESTBRIDGE_JENKINS_ID`.

Этот фрагмент Declarative Pipeline выполняется после сборки, в том числе при падении тестов. Ошибка отправки отмечается в журнале и не меняет результат сборки. `202 Accepted` означает, что архив принят, но не подтверждает его обработку.

```groovy
post {
    always {
        script {
            catchError(buildResult: 'SUCCESS', stageResult: 'UNSTABLE') {
                withCredentials([string(
                    credentialsId: 'metrics-srv-token',
                    variable: 'METRICS_TOKEN'
                )]) {
                    sh '''
                        set +x
                        set -eu
                        delivery_key=$(printf '%s\\n' "$TESTBRIDGE_JENKINS_ID" "$JOB_NAME" "$BUILD_NUMBER" | sha256sum)
                        delivery_key=${delivery_key%% *}
                        curl --fail --silent --show-error \
                          -H "Authorization: Bearer $METRICS_TOKEN" \
                          -H "Idempotency-Key: $delivery_key" \
                          -F "file=@allure-results.zip" \
                          "$METRICS_SRV_URL/api/v1/imports"
                    '''
                }
            }
        }
    }
}
```

Пример использует Unix-агент с `curl` и `sha256sum` и отправляет один ZIP на сборку. Если ответ потерялся, повторите запрос с тем же ключом и исходным ZIP. TestBridge вернёт `202`, прежний `importId` и текущее состояние импорта. При том же ключе и других байтах ZIP вернётся `409`. Заголовок необязателен. Без него каждый запрос регистрируется отдельно. Чтобы проверить результат разбора, используйте `GET /api/v1/imports/{importId}`.
