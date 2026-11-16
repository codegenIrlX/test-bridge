# Отправка результатов из Jenkins

Подготовьте ZIP с исходным каталогом `allure-results` и сохраните его в рабочем каталоге как `allure-results.zip`. Адрес платформы задайте переменной Jenkins `METRICS_SRV_URL`, а сервисный токен сохраните в Jenkins Credentials с идентификатором `metrics-srv-token`.

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
                        test -n "${JENKINS_URL:-}"
                        key=$(printf '%s\\n%s\\n%s' "$JENKINS_URL" "$JOB_NAME" "$BUILD_NUMBER" | sha256sum | cut -d ' ' -f 1)
                        curl --fail --silent --show-error \
                          -H "Authorization: Bearer $METRICS_TOKEN" \
                          -H "Idempotency-Key: $key" \
                          --form-string "project=${JOB_NAME}" \
                          --form-string "job=${JOB_NAME}" \
                          --form-string "buildNumber=${BUILD_NUMBER}" \
                          --form-string "buildUrl=${BUILD_URL:-}" \
                          --form-string "revision=${GIT_COMMIT:-}" \
                          -F "file=@allure-results.zip" \
                          "$METRICS_SRV_URL/api/v1/imports"
                    '''
                }
            }
        }
    }
}
```

Пример использует Unix-агент с `curl` и предполагает, что ZIP уже создан. Чтобы проверить результат разбора отдельно, используйте `importId` из ответа и метод `GET /api/v1/imports/{importId}`.

Ключ формируется из стабильного `JENKINS_URL`, полного `JOB_NAME` и номера сборки. Повтор с тем же ключом и теми же данными возвращает прежний импорт; повтор с другим содержимым или метаданными отклоняется с кодом `409`. Для повтора используйте тот же ZIP: при его повторном создании байты могут измениться. Для новой сборки нужен новый ключ. `project` содержит полное имя Jenkins job; для отдельного ряда в текущих метриках добавьте его в `metrics.project_allowlist` в `config.yaml`.
