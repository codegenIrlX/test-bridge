import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

from metrics_srv import metrics


@pytest.fixture
def postgres_schema():
    dsn = os.environ.get("TESTBRIDGE_TEST_DATABASE_DSN")
    if not dsn:
        pytest.skip("TESTBRIDGE_TEST_DATABASE_DSN не задан для интеграционного теста")
    schema = f"testbridge_test_{uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {} ").format(sql.Identifier(schema)))
    try:
        def connect():
            connection = psycopg.connect(dsn, row_factory=dict_row)
            connection.execute(sql.SQL("SET search_path TO {} ").format(sql.Identifier(schema)))
            return connection

        yield connect
    finally:
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_postgres_attempts_final_selection_and_valid_durations(
    postgres_schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    schema_sql = Path("metrics_srv/schema.sql").read_text(encoding="utf-8")
    import_id = uuid4()
    empty_import_id = uuid4()
    with postgres_schema() as connection:
        connection.execute(schema_sql)
        connection.execute("INSERT INTO imports (id, status) VALUES (%s, 'completed')", (import_id,))
        connection.execute(
            "INSERT INTO imports (id, status) VALUES (%s, 'completed')", (empty_import_id,)
        )
        for position, history, start, stop, status in [
            (0, "retry", 10, 200, "passed"),
            (1, "retry", 100, 150, "failed"),
            (2, "", 10, 5, "broken"),
            (3, "", None, None, None),
        ]:
            connection.execute(
                "INSERT INTO test_results (id, import_id, position, name, history_id, "
                "started_at_ms, stopped_at_ms, status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (uuid4(), import_id, position, "test", history, start, stop, status),
            )
        connection.execute(schema_sql)
    redis = MagicMock()
    redis.return_value.__enter__.return_value.pipeline.return_value.execute.return_value = [0] * 6
    monkeypatch.setattr(metrics, "Redis", redis)
    repository = SimpleNamespace(connect=postgres_schema)
    settings = SimpleNamespace(metrics_project_allowlist=(), metrics_suite_allowlist=(),
                               redis_host="redis", redis_port=6379)

    result = metrics.render_metrics(repository, settings)

    labels = 'project="unknown",suite="unknown"'
    assert f'testbridge_test_results{{kind="attempt",status="failed",{labels}}} 1' in result
    assert f'testbridge_test_results{{kind="final",status="passed",{labels}}} 1' in result
    assert f'testbridge_test_results{{kind="final",status="broken",{labels}}} 1' in result
    assert f'testbridge_test_results{{kind="final",status="unknown",{labels}}} 1' in result
    assert f'testbridge_test_duration_seconds_count{{kind="final",status="broken",{labels}}} 0' in result
    assert 'testbridge_runs{status="broken"} 1' in result
    assert 'testbridge_runs{status="unknown"} 1' in result
