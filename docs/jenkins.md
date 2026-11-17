# Отправка результатов из Jenkins

Эталонная функция `publishAllureToTestBridge()` находится в Jenkinsfile проекта `qa-lab-test-framework`. Вызовите её в `post.always` стадии с агентом, перед архивацией отчётов.

```groovy
post {
    always {
        script {
            publishAllureToTestBridge()
        }
    }
}
```

Задайте `TESTBRIDGE_URL = 'http://host.docker.internal:8000'` для агента Docker Desktop. Сохраните токен API в Jenkins Credentials как Secret Text `testbridge-metrics-srv`. Агенту нужны `curl`, `zip`, `sha256sum` и Python. В команде архивации добавьте `${env.REPORT_DIR}/allure-results.zip` и `${env.REPORT_DIR}/upload-response.json`.

Вызов выполняется и при падении тестов. Если Allure не создал ни одного `*-result.json`, отправка пропускается. Функция упаковывает весь каталог `allure-results`, включая вложения, и передаёт проект, имя джобы, номер и URL сборки и SHA проверенных исходников. Временная ошибка приводит к повторной отправке того же ZIP с тем же ключом. Ключ формируется из URL контроллера, имени джобы и номера сборки.

При `202` журнал показывает `TestBridge importId`. Чтобы проверить обработку, запросите `GET /api/v1/imports/{importId}` с тем же токеном и проверьте `completed` и `archiveDeleted: true`. Повтор с тем же ключом и байтами возвращает прежний `importId`. При других байтах API возвращает `409`. Ошибка передачи отмечается на этапе и сохраняет результат тестов.
