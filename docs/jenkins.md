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
                        curl --fail --silent --show-error \
                          -H "Authorization: Bearer $METRICS_TOKEN" \
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
