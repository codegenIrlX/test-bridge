import json
import os
import re
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


@pytest.fixture
def analytics_database():
    dsn = os.environ.get("TESTBRIDGE_TEST_DATABASE_DSN")
    if not dsn:
        pytest.skip("TESTBRIDGE_TEST_DATABASE_DSN не задан для интеграционного теста")
    suffix = uuid4().hex
    data_schema = f"dashboard_data_{suffix}"
    analytics_schema = f"dashboard_views_{suffix}"
    base_sql = Path("metrics_srv/schema.sql").read_text(encoding="utf-8")
    view_sql = Path("deploy/grafana_analytics.sql").read_text(encoding="utf-8")
    assert "testbridge_analytics" in view_sql
    view_sql = view_sql.replace("testbridge_analytics", analytics_schema)
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {} ").format(sql.Identifier(data_schema)))
    try:
        def connect():
            connection = psycopg.connect(dsn, row_factory=dict_row)
            connection.execute(sql.SQL("SET search_path TO {} ").format(sql.Identifier(data_schema)))
            return connection

        with connect() as connection:
            connection.execute(base_sql)
            connection.execute(view_sql)
            connection.execute(view_sql)
        yield connect, analytics_schema
    finally:
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                sql.Identifier(analytics_schema)
            ))
            connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                sql.Identifier(data_schema)
            ))


def add_result(connection, import_id, position, history_id, status, start, stop, name="same"):
    connection.execute(
        "INSERT INTO test_results (id, import_id, position, name, history_id, status, "
        "started_at_ms, stopped_at_ms, labels) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (uuid4(), import_id, position, name, history_id, status, start, stop,
         Jsonb([{"name": "suite", "value": "suite-a"}])),
    )


def test_project_isolation_retries_and_unknown_history(analytics_database):
    connect, views = analytics_database
    first, second, incomplete = uuid4(), uuid4(), uuid4()
    with connect() as connection:
        for identifier, project, status in [
            (first, "project-a", "completed"), (second, "project-b", "completed"),
            (incomplete, "project-a", "failed"),
        ]:
            connection.execute("INSERT INTO imports (id, project, status) VALUES (%s,%s,%s)",
                               (identifier, project, status))
        add_result(connection, first, 0, "same-history", "failed", 0, 1000)
        add_result(connection, first, 1, "same-history", "passed", 0, 4000)
        add_result(connection, first, 2, "other-history", "skipped", 0, 2000)
        add_result(connection, first, 3, None, "passed", 0, 3000)
        add_result(connection, first, 4, None, "passed", None, None)
        add_result(connection, second, 0, "same-history", "broken", 0, 6000)
        add_result(connection, incomplete, 0, "should-not-appear", "passed", 0, 1000)
        rows = connection.execute(sql.SQL(
            "SELECT project, history_id, status, is_final, test_key, suite "
            "FROM {}.tests ORDER BY project, status, test_key"
        ).format(sql.Identifier(views))).fetchall()
        runs = connection.execute(sql.SQL(
            "SELECT project, run_status, final_count, attempt_count FROM {}.runs ORDER BY project"
        ).format(sql.Identifier(views))).fetchall()

    assert len(rows) == 6
    project_a = [row for row in rows if row["project"] == "project-a"]
    assert len(project_a) == 5
    retry = [row for row in project_a if row["history_id"] == "same-history"]
    assert {(row["status"], row["is_final"]) for row in retry} == {
        ("failed", False), ("passed", True)
    }
    unidentified = [row for row in project_a if row["history_id"] is None]
    assert len({row["test_key"] for row in unidentified}) == 2
    assert all(row["suite"] == "suite-a" for row in rows)
    assert runs == [
        {"project": "project-a", "run_status": "skipped", "final_count": 4, "attempt_count": 5},
        {"project": "project-b", "run_status": "broken", "final_count": 1, "attempt_count": 1},
    ]


def test_multiple_suites_are_available_without_duplicating_test(analytics_database):
    connect, views = analytics_database
    import_id = uuid4()
    with connect() as connection:
        connection.execute("INSERT INTO imports (id, project, status) VALUES (%s,%s,%s)",
                           (import_id, "project-a", "completed"))
        connection.execute(
            "INSERT INTO test_results (id, import_id, position, name, labels) "
            "VALUES (%s,%s,%s,%s,%s)",
            (uuid4(), import_id, 0, "test", Jsonb([
                {"name": "suite", "value": "one"},
                {"name": "suite", "value": "two"},
                {"name": "suite", "value": "one"},
            ])),
        )
        rows = connection.execute(sql.SQL(
            "SELECT suites FROM {}.tests WHERE %s = ANY(suites)"
        ).format(sql.Identifier(views)), ("two",)).fetchall()
    assert len(rows) == 1
    assert set(rows[0]["suites"]) == {"one", "two"}


def test_p95_excludes_nonfinal_missing_and_invalid_durations(analytics_database):
    connect, views = analytics_database
    import_id = uuid4()
    with connect() as connection:
        connection.execute("INSERT INTO imports (id, project, status) VALUES (%s,%s,%s)",
                           (import_id, "project-a", "completed"))
        add_result(connection, import_id, 0, "retry", "failed", 0, 1000)
        add_result(connection, import_id, 1, "retry", "passed", 0, 4000)
        add_result(connection, import_id, 2, "other", "passed", 0, 2000)
        add_result(connection, import_id, 3, None, "passed", 0, 3000)
        add_result(connection, import_id, 4, None, "passed", 10, 5)
        add_result(connection, import_id, 5, None, "passed", None, None)
        result = connection.execute(sql.SQL(
            "SELECT count(duration_seconds) AS measurements, "
            "percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_seconds) AS p95 "
            "FROM {}.tests WHERE project = %s AND is_final"
        ).format(sql.Identifier(views)), ("project-a",)).fetchone()
    assert result["measurements"] == 3
    assert result["p95"] == pytest.approx(3.9)


def dashboard_query(panel_id, analytics_schema, project, *, since=None):
    dashboard_path = Path(os.environ.get(
        "TESTBRIDGE_TEST_DASHBOARD_PATH", "deploy/grafana/dashboards/testbridge.json"
    ))
    dashboard = json.loads(dashboard_path.read_text(encoding="utf-8"))
    panel = next(panel for panel in dashboard["panels"] if panel["id"] == panel_id)
    query = panel["targets"][0]["rawSql"].replace("testbridge_analytics", analytics_schema)
    query = re.sub(
        r"\$__timeFilter\(([^)]+)\)",
        lambda match: f"{match.group(1)} >= '{since}'::timestamptz" if since else "TRUE",
        query,
    )
    query = re.sub(r"\$__timeGroupAlias\(imported_at, '\$__interval'\)",
                   "date_trunc('minute', imported_at) AS time", query)
    # Grafana sqlstring doubles single quotes inside each selected value.
    project_value = "n:" if project is None else "v:" + project
    query = query.replace("${project:sqlstring}", "'" + project_value.replace("'", "''") + "'")
    return query.replace("${suite:sqlstring}", "'v:suite-a'")


def test_dashboard_top_and_p95_use_selected_project_and_test_names(analytics_database):
    connect, views = analytics_database
    project = "project-a'); SELECT 1; --"
    imports = [uuid4(), uuid4(), uuid4()]
    with connect() as connection:
        for index, identifier in enumerate(imports):
            connection.execute(
                "INSERT INTO imports (id, project, status, created_at) "
                "VALUES (%s,%s,'completed','2026-01-01T00:00:00Z')",
                (identifier, project if index < 2 else "project-b"),
            )
        add_result(connection, imports[0], 0, "history-a", "passed", 0, 2000, name="login")
        add_result(connection, imports[1], 0, "history-a", "passed", 0, 4000, name="login")
        add_result(connection, imports[0], 1, "history-b", "passed", 0, 3000, name="login")
        add_result(connection, imports[0], 2, "invalid", "passed", 10, 5, name="invalid")
        add_result(connection, imports[2], 0, "history-a", "passed", 0, 9000, name="login")
        graph = connection.execute(dashboard_query(4, views, project)).fetchall()
        distribution = connection.execute(dashboard_query(6, views, project)).fetchall()
        remaining = connection.execute("SELECT count(*) AS n FROM imports").fetchone()

    assert len(graph) == 2
    assert {float(row["value"]) for row in graph} == {3.0, 4.0}
    assert all("login" in row["metric"] and project in row["metric"] for row in graph)
    assert len({row["metric"] for row in graph}) == 1
    assert all("history:" not in row["metric"] for row in graph)
    assert len(distribution) == 1
    assert distribution[0]["P95"] == pytest.approx(3.9)
    assert remaining["n"] == 3


def test_slowest_tests_group_by_name_per_project_and_import(analytics_database):
    connect, views = analytics_database
    first, second, other, failed, previous = (uuid4() for _ in range(5))
    with connect() as connection:
        for identifier, project, status in (
            (first, "project-a", "completed"), (second, "project-a", "completed"),
            (other, "project-b", "completed"), (failed, "project-a", "failed"),
        ):
            connection.execute(
                "INSERT INTO imports (id, project, status, created_at) "
                "VALUES (%s, %s, %s, '2026-01-01T00:00:00Z')",
                (identifier, project, status),
            )
        connection.execute(
            "INSERT INTO imports (id, project, status, created_at) "
            "VALUES (%s, 'project-a', 'completed', '2025-01-01T00:00:00Z')", (previous,),
        )
        add_result(connection, first, 0, "retry", "failed", 0, 4000, name="login")
        add_result(connection, first, 1, "retry", "passed", 10000, 15000, name="login")
        add_result(connection, first, 2, "different", "passed", 0, 20000, name="login")
        add_result(connection, second, 0, "retry", "passed", 0, 30000, name="login")
        add_result(connection, other, 0, "retry", "passed", 0, 99000, name="login")
        add_result(connection, failed, 0, "retry", "passed", 0, 99000, name="login")
        add_result(connection, previous, 0, "retry", "passed", 0, 99000, name="login")
        add_result(connection, first, 3, "missing", "passed", None, None, name="missing")
        add_result(connection, first, 4, "invalid", "passed", 10, 5, name="invalid")
        add_result(connection, first, 16, "other-suite", "passed", 0, 120000, name="other-suite")
        connection.execute(
            "UPDATE test_results SET labels = %s WHERE import_id = %s AND name = %s",
            (Jsonb([{"name": "suite", "value": "suite-b"}]), first, "other-suite"),
        )
        for index in range(1, 12):
            add_result(connection, first, index + 4, f"h-{index}", "passed",
                       0, index * 1000, name=f"test-{index}")

        rows = connection.execute(dashboard_query(4, views, "project-a", since="2026-01-01"))
        rows = rows.fetchall()
        other_rows = connection.execute(dashboard_query(4, views, "project-b", since="2026-01-01"))
        other_rows = other_rows.fetchall()

    metrics = {row["metric"] for row in rows}
    login_rows = [row for row in rows if row["metric"].endswith("login")]
    assert len(metrics) == 10
    assert len(login_rows) == 2
    assert {float(row["value"]) for row in login_rows} == {20.0, 30.0}
    assert all("history:" not in metric and "project-b" not in metric for metric in metrics)
    assert all(not metric.endswith(("test-1", "test-2", "invalid", "missing", "other-suite"))
               for metric in metrics)
    assert len(other_rows) == 1 and float(other_rows[0]["value"]) == 99.0


def test_unknown_project_is_distinct_from_named_project(analytics_database):
    connect, views = analytics_database
    unknown, named = uuid4(), uuid4()
    with connect() as connection:
        connection.execute("INSERT INTO imports (id, project, status) VALUES (%s,NULL,'completed')",
                           (unknown,))
        connection.execute("INSERT INTO imports (id, project, status) VALUES (%s,%s,'completed')",
                           (named, "Неизвестный проект"))
        add_result(connection, unknown, 0, "unknown-history", "passed", 0, 1000, name="missing")
        add_result(connection, named, 0, "named-history", "passed", 0, 2000, name="named")
        unknown_rows = connection.execute(dashboard_query(4, views, None)).fetchall()
        named_rows = connection.execute(dashboard_query(4, views, "Неизвестный проект")).fetchall()
    assert len(unknown_rows) == 1 and "missing" in unknown_rows[0]["metric"]
    assert len(named_rows) == 1 and "named" in named_rows[0]["metric"]
