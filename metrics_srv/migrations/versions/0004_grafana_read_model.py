"""Добавляет представления результатов для Grafana."""

from alembic import op

revision = "0004_grafana_read_model"
down_revision = "0003_run_analytics"
branch_labels = None
depends_on = None

_IMPORT_COLUMNS = {
    "import_id": "Идентификатор импорта",
    "project": "Проект Jenkins из записи импорта",
    "created_at": "Время регистрации импорта",
    "status": "Состояние импорта",
}
_RESULT_COLUMNS = {
    **_IMPORT_COLUMNS,
    "result_id": "Идентификатор результата теста",
    "name": "Имя теста из Allure",
    "status": "Статус теста из Allure с подстановкой unknown",
    "history_id": "Идентификатор истории теста из Allure",
    "duration_seconds": "Длительность теста в секундах при корректных временах",
}


def upgrade() -> None:
    """Открывает для чтения импорты, попытки и итоги тестов."""
    op.execute("""
        CREATE VIEW dashboard_imports AS
        SELECT id AS import_id, project::text AS project, created_at, status
        FROM imports
        WHERE NULLIF(btrim(project), '') IS NOT NULL
    """)
    op.execute("""
        CREATE VIEW dashboard_attempts AS
        SELECT r.id AS result_id, i.import_id, i.project, i.created_at, r.name,
               CASE WHEN r.status IN ('passed', 'failed', 'broken', 'skipped', 'unknown')
                    THEN r.status ELSE 'unknown' END AS status,
               NULLIF(r.history_id, '') AS history_id,
               CASE WHEN r.stopped_at_ms >= r.started_at_ms
                    THEN ((r.stopped_at_ms::numeric - r.started_at_ms::numeric)
                          / 1000)::double precision END AS duration_seconds,
               r.started_at_ms, r.stopped_at_ms, r.position
        FROM dashboard_imports AS i
        JOIN test_results AS r ON r.import_id = i.import_id
        WHERE i.status = 'completed'
    """)
    op.execute("""
        CREATE VIEW dashboard_final_results AS
        SELECT result_id, import_id, project, created_at, name, status, history_id, duration_seconds
        FROM (
            SELECT a.*,
                   row_number() OVER (
                       PARTITION BY project, created_at, import_id, history_id,
                                    CASE WHEN history_id IS NULL THEN result_id END
                       ORDER BY stopped_at_ms DESC NULLS LAST,
                                started_at_ms DESC NULLS LAST, position DESC
                   ) AS attempt_rank
            FROM dashboard_attempts AS a
        ) AS ranked
        WHERE attempt_rank = 1
    """)
    for view, title, columns in (
        ("dashboard_imports", "Импорты с указанным проектом", _IMPORT_COLUMNS),
        ("dashboard_attempts", "Все попытки тестов из завершённых импортов", {
            **_RESULT_COLUMNS,
            "started_at_ms": "Время начала попытки в миллисекундах Unix",
            "stopped_at_ms": "Время завершения попытки в миллисекундах Unix",
            "position": "Порядок результата в архиве",
        }),
        ("dashboard_final_results", "Итоговые результаты тестов в каждом импорте", _RESULT_COLUMNS),
    ):
        op.execute(f"COMMENT ON VIEW {view} IS '{title}'")
        for column, description in columns.items():
            op.execute(f"COMMENT ON COLUMN {view}.{column} IS '{description}'")


def downgrade() -> None:
    """Удаляет представления Grafana."""
    op.execute("DROP VIEW dashboard_final_results")
    op.execute("DROP VIEW dashboard_attempts")
    op.execute("DROP VIEW dashboard_imports")
