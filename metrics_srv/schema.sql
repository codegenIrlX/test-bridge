CREATE TABLE IF NOT EXISTS imports (
    id UUID PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'processing', 'completed', 'failed')),
    error_message TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    archive_deleted BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON COLUMN imports.id IS 'Идентификатор импорта и имя архива';
COMMENT ON COLUMN imports.status IS 'Состояние обработки архива';
COMMENT ON COLUMN imports.error_message IS 'Безопасное описание ошибки обработки';
COMMENT ON COLUMN imports.attempt_count IS 'Число начатых попыток обработки';
COMMENT ON COLUMN imports.archive_deleted IS 'Признак удаления обработанного архива';
COMMENT ON COLUMN imports.created_at IS 'Время регистрации принятого архива';

CREATE TABLE IF NOT EXISTS test_results (
    id UUID PRIMARY KEY,
    import_id UUID NOT NULL REFERENCES imports(id),
    position INTEGER NOT NULL,
    name TEXT NOT NULL,
    started_at_ms BIGINT,
    stopped_at_ms BIGINT,
    UNIQUE (import_id, position)
);
COMMENT ON COLUMN test_results.id IS 'Идентификатор результата теста';
COMMENT ON COLUMN test_results.import_id IS 'Импорт, содержащий результат';
COMMENT ON COLUMN test_results.position IS 'Порядок результата в архиве';
COMMENT ON COLUMN test_results.name IS 'Название теста из Allure';
COMMENT ON COLUMN test_results.started_at_ms IS 'Начало теста в миллисекундах Unix';
COMMENT ON COLUMN test_results.stopped_at_ms IS 'Окончание теста в миллисекундах Unix';

CREATE TABLE IF NOT EXISTS test_steps (
    id UUID PRIMARY KEY,
    test_id UUID NOT NULL REFERENCES test_results(id),
    parent_id UUID REFERENCES test_steps(id),
    position INTEGER NOT NULL,
    name TEXT NOT NULL,
    started_at_ms BIGINT,
    stopped_at_ms BIGINT
);
COMMENT ON COLUMN test_steps.id IS 'Идентификатор шага';
COMMENT ON COLUMN test_steps.test_id IS 'Тест, которому принадлежит шаг';
COMMENT ON COLUMN test_steps.parent_id IS 'Родительский шаг, если шаг вложен';
COMMENT ON COLUMN test_steps.position IS 'Порядок шага среди соседних шагов';
COMMENT ON COLUMN test_steps.name IS 'Название шага из Allure';
COMMENT ON COLUMN test_steps.started_at_ms IS 'Начало шага в миллисекундах Unix';
COMMENT ON COLUMN test_steps.stopped_at_ms IS 'Окончание шага в миллисекундах Unix';

-- Идемпотентное расширение схемы для существующих установок.
ALTER TABLE imports ADD COLUMN IF NOT EXISTS project TEXT;
ALTER TABLE imports ADD COLUMN IF NOT EXISTS job TEXT;
ALTER TABLE imports ADD COLUMN IF NOT EXISTS build_number TEXT;
ALTER TABLE imports ADD COLUMN IF NOT EXISTS build_url TEXT;
ALTER TABLE imports ADD COLUMN IF NOT EXISTS revision TEXT;
ALTER TABLE imports ADD COLUMN IF NOT EXISTS idempotency_key TEXT;
ALTER TABLE imports ADD COLUMN IF NOT EXISTS archive_sha256 TEXT;
ALTER TABLE imports ADD COLUMN IF NOT EXISTS processing_started_at TIMESTAMPTZ;
ALTER TABLE imports ADD COLUMN IF NOT EXISTS processing_finished_at TIMESTAMPTZ;
CREATE UNIQUE INDEX IF NOT EXISTS imports_idempotency_key_unique ON imports(idempotency_key);
COMMENT ON COLUMN imports.project IS 'Проект, переданный отправителем';
COMMENT ON COLUMN imports.job IS 'Задание CI, переданное отправителем';
COMMENT ON COLUMN imports.build_number IS 'Номер сборки CI';
COMMENT ON COLUMN imports.build_url IS 'Ссылка на сборку CI';
COMMENT ON COLUMN imports.revision IS 'Ревизия исходного кода сборки';
COMMENT ON COLUMN imports.idempotency_key IS 'Ключ для безопасного повтора отправки';
COMMENT ON COLUMN imports.archive_sha256 IS 'SHA-256 принятого архива';
COMMENT ON COLUMN imports.processing_started_at IS 'Время начала последней попытки разбора';
COMMENT ON COLUMN imports.processing_finished_at IS 'Время завершения обработки импорта';

ALTER TABLE test_results ADD COLUMN IF NOT EXISTS status TEXT;
ALTER TABLE test_results ADD COLUMN IF NOT EXISTS allure_uuid TEXT;
ALTER TABLE test_results ADD COLUMN IF NOT EXISTS history_id TEXT;
ALTER TABLE test_results ADD COLUMN IF NOT EXISTS test_case_id TEXT;
ALTER TABLE test_results ADD COLUMN IF NOT EXISTS full_name TEXT;
ALTER TABLE test_results ADD COLUMN IF NOT EXISTS labels JSONB;
ALTER TABLE test_results ADD COLUMN IF NOT EXISTS parameters JSONB;
COMMENT ON COLUMN test_results.status IS 'Статус теста из Allure';
COMMENT ON COLUMN test_results.allure_uuid IS 'Идентификатор результата из Allure';
COMMENT ON COLUMN test_results.history_id IS 'Идентификатор истории теста из Allure';
COMMENT ON COLUMN test_results.test_case_id IS 'Идентификатор тестового случая из Allure';
COMMENT ON COLUMN test_results.full_name IS 'Полное имя теста из Allure';
COMMENT ON COLUMN test_results.labels IS 'Выбранные метки теста из Allure';
COMMENT ON COLUMN test_results.parameters IS 'Параметры теста из Allure';
ALTER TABLE test_steps ADD COLUMN IF NOT EXISTS status TEXT;
COMMENT ON COLUMN test_steps.status IS 'Статус шага из Allure';
