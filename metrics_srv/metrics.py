"""Срез агрегатов PostgreSQL и состояния очереди в формате Prometheus."""

from __future__ import annotations

from collections import defaultdict

from redis import Redis

from metrics_srv.background import QUEUE_NAME
from metrics_srv.config import Settings
from metrics_srv.repository import Repository

STATUSES = ("passed", "failed", "broken", "skipped", "unknown")
IMPORT_STATUSES = ("pending", "processing", "completed", "failed")
QUEUE_STATES = {"waiting": "wait", "active": "active", "delayed": "delayed",
                "failed": "failed", "completed": "completed", "paused": "paused"}


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _bounded(value: str | None, allowed: tuple[str, ...]) -> str:
    if not value:
        return "unknown"
    return value if value in allowed else "other"


def render_metrics(repository: Repository, settings: Settings) -> str:
    """Вернуть текущие агрегаты; ошибки источников передаются вызывающему коду."""
    with repository.connect() as connection:
        imports = connection.execute(
            "SELECT status, count(*) AS count FROM imports GROUP BY status"
        ).fetchall()
        processing = connection.execute(
            "SELECT count(*) AS count, COALESCE(sum(EXTRACT(EPOCH FROM "
            "(processing_finished_at - processing_started_at))), 0) AS seconds "
            "FROM imports WHERE processing_finished_at >= processing_started_at"
        ).fetchone()
        rows = connection.execute("""
            WITH ranked AS (
              SELECT t.*, i.project,
                (SELECT item->>'value' FROM jsonb_array_elements(
                    CASE WHEN jsonb_typeof(t.labels) = 'array' THEN t.labels ELSE '[]'::jsonb END
                ) item WHERE item->>'name' = 'suite' LIMIT 1) AS suite,
                row_number() OVER (
                  PARTITION BY t.import_id, COALESCE(NULLIF(t.history_id, ''), t.id::text)
                  ORDER BY t.stopped_at_ms DESC NULLS LAST,
                           t.started_at_ms DESC NULLS LAST, t.position DESC
                ) AS retry_rank
              FROM test_results t JOIN imports i ON i.id = t.import_id
              WHERE i.status = 'completed'
            )
            SELECT status, project, suite, retry_rank = 1 AS final,
              count(*) AS count,
              count(*) FILTER (WHERE stopped_at_ms >= started_at_ms) AS duration_count,
              COALESCE(sum((stopped_at_ms - started_at_ms)::numeric / 1000)
                FILTER (WHERE stopped_at_ms >= started_at_ms), 0) AS duration_seconds
            FROM ranked GROUP BY status, project, suite, retry_rank = 1
        """).fetchall()
        runs = connection.execute("""
            WITH ranked AS (
              SELECT t.import_id, t.status,
                row_number() OVER (
                  PARTITION BY t.import_id, COALESCE(NULLIF(t.history_id, ''), t.id::text)
                  ORDER BY t.stopped_at_ms DESC NULLS LAST,
                           t.started_at_ms DESC NULLS LAST, t.position DESC
                ) AS retry_rank
              FROM test_results t JOIN imports i ON i.id = t.import_id
              WHERE i.status = 'completed'
            ), outcomes AS (
              SELECT i.id, CASE
                WHEN bool_or(r.status = 'broken') THEN 'broken'
                WHEN bool_or(r.status = 'failed') THEN 'failed'
                WHEN bool_or(r.status IS NULL OR r.status NOT IN
                             ('passed', 'failed', 'broken', 'skipped')) THEN 'unknown'
                WHEN bool_or(r.status = 'skipped') THEN 'skipped'
                ELSE 'passed' END AS status
              FROM imports i LEFT JOIN ranked r ON r.import_id = i.id AND r.retry_rank = 1
              WHERE i.status = 'completed' GROUP BY i.id
            ) SELECT status, count(*) AS count FROM outcomes GROUP BY status
        """).fetchall()

    aggregate: dict[tuple[str, str, str, str], list[float]] = defaultdict(lambda: [0, 0, 0])
    for row in rows:
        status = row["status"] if row["status"] in STATUSES else "unknown"
        project = _bounded(row["project"], settings.metrics_project_allowlist)
        suite = _bounded(row["suite"], settings.metrics_suite_allowlist)
        for kind in ("attempt", "final") if row["final"] else ("attempt",):
            value = aggregate[(kind, status, project, suite)]
            value[0] += row["count"]
            value[1] += row["duration_count"]
            value[2] += float(row["duration_seconds"])

    output = ["# HELP testbridge_imports Импорты по текущему состоянию.",
              "# TYPE testbridge_imports gauge"]
    import_counts = {row["status"]: row["count"] for row in imports}
    for status in IMPORT_STATUSES:
        output.append(f'testbridge_imports{{status="{status}"}} {import_counts.get(status, 0)}')
    run_counts = {row["status"]: row["count"] for row in runs}
    output.extend(("# HELP testbridge_runs Прогоны по итоговым статусам тестов.",
                   "# TYPE testbridge_runs gauge"))
    for status in STATUSES:
        output.append(f'testbridge_runs{{status="{status}"}} {run_counts.get(status, 0)}')
    for name, description, index in (
        ("testbridge_test_results", "Результаты тестов и попыток.", 0),
        ("testbridge_test_duration_seconds_count", "Число тестов с корректным временем.", 1),
        ("testbridge_test_duration_seconds_sum", "Сумма длительностей тестов в секундах.", 2),
    ):
        output.extend((f"# HELP {name} {description}", f"# TYPE {name} gauge"))
        for (kind, status, project, suite), values in sorted(aggregate.items()):
            labels = f'kind="{kind}",status="{status}",project="{_label(project)}",suite="{_label(suite)}"'
            output.append(f"{name}{{{labels}}} {values[index]}")
    for suffix, value in (("count", processing["count"]), ("sum", processing["seconds"])):
        name = f"testbridge_import_processing_seconds_{suffix}"
        output.extend((f"# HELP {name} Длительность обработки импортов.",
                       f"# TYPE {name} gauge", f"{name} {value}"))

    with Redis(host=settings.redis_host, port=settings.redis_port,
               socket_connect_timeout=2, socket_timeout=2) as redis:
        pipeline = redis.pipeline(transaction=False)
        for key in QUEUE_STATES.values():
            full_key = f"bull:{QUEUE_NAME}:{key}"
            if key in ("wait", "active", "paused"):
                pipeline.llen(full_key)
            else:
                pipeline.zcard(full_key)
        counts = pipeline.execute()
    output.extend(("# HELP testbridge_queue_jobs Задания BullMQ по состоянию.",
                   "# TYPE testbridge_queue_jobs gauge"))
    for state, count in zip(QUEUE_STATES, counts, strict=True):
        output.append(f'testbridge_queue_jobs{{state="{state}"}} {count}')
    return "\n".join(output) + "\n"
