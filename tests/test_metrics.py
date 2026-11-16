from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from metrics_srv import metrics


def make_repository(rows: list[dict]) -> MagicMock:
    repository = MagicMock()
    connection = repository.connect.return_value.__enter__.return_value
    imports = MagicMock()
    imports.fetchall.return_value = [{"status": "completed", "count": 2}]
    processing = MagicMock()
    processing.fetchone.return_value = {"count": 2, "seconds": 3.5}
    results = MagicMock()
    results.fetchall.return_value = rows
    runs = MagicMock()
    runs.fetchall.return_value = [{"status": "passed", "count": 1}]
    connection.execute.side_effect = [imports, processing, results, runs]
    return repository


def settings() -> SimpleNamespace:
    return SimpleNamespace(
        metrics_project_allowlist=("project-a",),
        metrics_suite_allowlist=("suite-a",),
        redis_host="redis",
        redis_port=6379,
    )


def test_render_metrics_bounds_labels_and_counts_attempts_and_finals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {"status": "passed", "project": "project-a", "suite": "suite-a", "final": True,
         "count": 1, "duration_count": 1, "duration_seconds": 2.5},
        {"status": "unrecognized", "project": "private-project", "suite": None, "final": False,
         "count": 2, "duration_count": 0, "duration_seconds": 0},
    ]
    redis = MagicMock()
    redis.return_value.__enter__.return_value.pipeline.return_value.execute.return_value = [
        4, 2, 1, 0, 3, 0
    ]
    monkeypatch.setattr(metrics, "Redis", redis)

    result = metrics.render_metrics(make_repository(rows), settings())

    assert 'testbridge_imports{status="completed"} 2' in result
    assert 'testbridge_imports{status="failed"} 0' in result
    assert 'testbridge_runs{status="passed"} 1' in result
    assert (
        'testbridge_test_results{kind="final",status="passed",'
        'project="project-a",suite="suite-a"} 1'
    ) in result
    assert (
        'testbridge_test_results{kind="attempt",status="unknown",'
        'project="other",suite="unknown"} 2'
    ) in result
    assert (
        'testbridge_test_duration_seconds_sum{kind="final",status="passed",'
        'project="project-a",suite="suite-a"} 2.5'
    ) in result
    assert 'testbridge_queue_jobs{state="waiting"} 4' in result
    assert "private-project" not in result


def test_unavailable_queue_is_not_reported_as_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = MagicMock()
    redis.return_value.__enter__.side_effect = ConnectionError("queue unavailable")
    monkeypatch.setattr(metrics, "Redis", redis)

    with pytest.raises(ConnectionError, match="queue unavailable"):
        metrics.render_metrics(make_repository([]), settings())


def test_unavailable_database_is_not_reported_as_empty() -> None:
    repository = MagicMock()
    repository.connect.side_effect = ConnectionError("database unavailable")

    with pytest.raises(ConnectionError, match="database unavailable"):
        metrics.render_metrics(repository, settings())
