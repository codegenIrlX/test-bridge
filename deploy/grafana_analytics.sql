CREATE SCHEMA IF NOT EXISTS testbridge_analytics;
REVOKE ALL ON SCHEMA testbridge_analytics FROM PUBLIC;

CREATE OR REPLACE VIEW testbridge_analytics.tests AS
WITH ranked AS (
    SELECT t.*, i.project, i.job, i.build_number, i.build_url, i.revision,
           i.created_at AS imported_at,
           row_number() OVER (
               PARTITION BY t.import_id, COALESCE(NULLIF(t.history_id, ''), t.id::text)
               ORDER BY t.stopped_at_ms DESC NULLS LAST, t.started_at_ms DESC NULLS LAST,
                        t.position DESC
           ) AS retry_rank
    FROM test_results t JOIN imports i ON i.id = t.import_id
    WHERE i.status = 'completed'
)
SELECT t.id, t.import_id, t.project, t.job, t.build_number, t.build_url, t.revision,
       t.imported_at, t.name, t.history_id, t.full_name,
       CASE WHEN t.status IN ('passed', 'failed', 'broken', 'skipped')
            THEN t.status ELSE 'unknown' END AS status,
       t.started_at_ms, t.stopped_at_ms,
       CASE WHEN t.stopped_at_ms >= t.started_at_ms
            THEN (t.stopped_at_ms::numeric - t.started_at_ms::numeric) / 1000 END AS duration_seconds,
       (SELECT item->>'value'
        FROM jsonb_array_elements(
            CASE WHEN jsonb_typeof(t.labels) = 'array' THEN t.labels ELSE '[]'::jsonb END
        ) AS item WHERE item->>'name' = 'suite' LIMIT 1) AS suite,
       (t.retry_rank = 1) AS is_final,
       CASE WHEN NULLIF(t.history_id, '') IS NOT NULL THEN 'history:' || t.history_id
            ELSE 'result:' || t.id::text END AS test_key,
       ARRAY(SELECT DISTINCT item->>'value'
             FROM jsonb_array_elements(
                 CASE WHEN jsonb_typeof(t.labels) = 'array' THEN t.labels ELSE '[]'::jsonb END
             ) AS item
             WHERE item->>'name' = 'suite' AND jsonb_typeof(item->'value') = 'string'
             ORDER BY item->>'value') AS suites
FROM ranked t;
COMMENT ON COLUMN testbridge_analytics.tests.id IS 'Идентификатор результата теста';
COMMENT ON COLUMN testbridge_analytics.tests.import_id IS 'Идентификатор принятого импорта';
COMMENT ON COLUMN testbridge_analytics.tests.project IS 'Имя проекта из CI';
COMMENT ON COLUMN testbridge_analytics.tests.job IS 'Имя задания CI';
COMMENT ON COLUMN testbridge_analytics.tests.build_number IS 'Номер сборки CI';
COMMENT ON COLUMN testbridge_analytics.tests.build_url IS 'Ссылка на сборку CI';
COMMENT ON COLUMN testbridge_analytics.tests.revision IS 'Ревизия исходного кода';
COMMENT ON COLUMN testbridge_analytics.tests.imported_at IS 'Время приёма архива';
COMMENT ON COLUMN testbridge_analytics.tests.name IS 'Название теста Allure';
COMMENT ON COLUMN testbridge_analytics.tests.history_id IS 'Идентификатор истории теста Allure';
COMMENT ON COLUMN testbridge_analytics.tests.full_name IS 'Полное имя теста Allure';
COMMENT ON COLUMN testbridge_analytics.tests.status IS 'Нормализованный статус результата теста';
COMMENT ON COLUMN testbridge_analytics.tests.started_at_ms IS 'Начало теста в миллисекундах Unix';
COMMENT ON COLUMN testbridge_analytics.tests.stopped_at_ms IS 'Окончание теста в миллисекундах Unix';
COMMENT ON COLUMN testbridge_analytics.tests.duration_seconds IS 'Длительность теста в секундах';
COMMENT ON COLUMN testbridge_analytics.tests.suite IS 'Первое имя набора Allure';
COMMENT ON COLUMN testbridge_analytics.tests.is_final IS 'Признак итоговой попытки теста в импорте';
COMMENT ON COLUMN testbridge_analytics.tests.test_key IS 'Идентификатор теста для группировки в проекте';
COMMENT ON COLUMN testbridge_analytics.tests.suites IS 'Уникальные имена всех наборов Allure';

CREATE OR REPLACE VIEW testbridge_analytics.runs AS
SELECT i.id AS import_id, i.project, i.job, i.build_number, i.build_url, i.revision,
       i.created_at AS imported_at, i.status AS import_status,
       CASE WHEN bool_or(t.status = 'broken') FILTER (WHERE t.is_final) THEN 'broken'
            WHEN bool_or(t.status = 'failed') FILTER (WHERE t.is_final) THEN 'failed'
            WHEN count(t.id) FILTER (WHERE t.is_final) = 0
                 OR bool_or(t.status = 'unknown') FILTER (WHERE t.is_final) THEN 'unknown'
            WHEN bool_or(t.status = 'skipped') FILTER (WHERE t.is_final) THEN 'skipped'
            ELSE 'passed' END AS run_status,
       count(t.id) FILTER (WHERE t.is_final) AS final_count,
       count(t.id) AS attempt_count,
       (max(t.stopped_at_ms) FILTER (WHERE t.is_final AND t.duration_seconds IS NOT NULL)::numeric
        - min(t.started_at_ms) FILTER (WHERE t.is_final AND t.duration_seconds IS NOT NULL)::numeric)
        / 1000 AS duration_seconds
FROM imports i LEFT JOIN testbridge_analytics.tests t ON t.import_id = i.id
WHERE i.status = 'completed'
GROUP BY i.id;
COMMENT ON COLUMN testbridge_analytics.runs.import_id IS 'Идентификатор принятого импорта';
COMMENT ON COLUMN testbridge_analytics.runs.project IS 'Имя проекта из CI';
COMMENT ON COLUMN testbridge_analytics.runs.job IS 'Имя задания CI';
COMMENT ON COLUMN testbridge_analytics.runs.build_number IS 'Номер сборки CI';
COMMENT ON COLUMN testbridge_analytics.runs.build_url IS 'Ссылка на сборку CI';
COMMENT ON COLUMN testbridge_analytics.runs.revision IS 'Ревизия исходного кода';
COMMENT ON COLUMN testbridge_analytics.runs.imported_at IS 'Время приёма архива';
COMMENT ON COLUMN testbridge_analytics.runs.import_status IS 'Состояние обработки архива';
COMMENT ON COLUMN testbridge_analytics.runs.run_status IS 'Итоговый статус тестов прогона';
COMMENT ON COLUMN testbridge_analytics.runs.final_count IS 'Число итоговых тестов';
COMMENT ON COLUMN testbridge_analytics.runs.attempt_count IS 'Число всех попыток тестов';
COMMENT ON COLUMN testbridge_analytics.runs.duration_seconds IS 'Интервал выполнения итоговых тестов в секундах';
