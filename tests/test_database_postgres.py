import json
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from threading import Barrier, Event
from uuid import UUID, uuid4
from zipfile import ZipFile

import pytest
from alembic import command
from alembic.runtime.migration import MigrationContext
from fastapi.testclient import TestClient
from httpx import Response
from psycopg import sql
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import NullPool

from metrics_srv.api import create_app
from metrics_srv.config import Settings
from metrics_srv.db.session import create_session_factory
from metrics_srv.domains.imports import processing
from metrics_srv.domains.imports.analytics import MetricsReader
from metrics_srv.domains.imports.cleanup import ArchiveCleanup
from metrics_srv.domains.imports.parser import ParsedTest
from metrics_srv.domains.imports.processing import ImportProcessor
from metrics_srv.domains.imports.repository import ImportRepository
from metrics_srv.domains.imports.services import ImportService
from metrics_srv.migrations import build_alembic_config


@pytest.fixture
def postgres_engine() -> Iterator[Engine]:
    connection_url = os.getenv("TESTBRIDGE_TEST_POSTGRES_URL")
    if not connection_url:
        pytest.skip("Укажите TESTBRIDGE_TEST_POSTGRES_URL для проверки на PostgreSQL 17")

    url = make_url(connection_url)
    if url.drivername == "postgresql":
        url = url.set(drivername="postgresql+psycopg")
    if url.drivername != "postgresql+psycopg":
        pytest.fail("Тестовый URL должен указывать на PostgreSQL с psycopg")

    schema = f"testbridge_test_{uuid4().hex}"
    identifier = sql.Identifier(schema)
    control = create_engine(url, poolclass=NullPool, hide_parameters=True)
    engine = create_engine(
        url, poolclass=NullPool, hide_parameters=True,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    try:
        with control.begin() as connection:
            version = connection.scalar(text("SHOW server_version_num"))
            assert int(version) // 10_000 == 17, "Для тестов требуется PostgreSQL 17"
            connection.exec_driver_sql(sql.SQL("CREATE SCHEMA {}").format(identifier).as_string())
        try:
            with engine.connect() as connection:
                assert connection.scalar(text("SELECT current_schema()")) == schema
            yield engine
        finally:
            with control.begin() as connection:
                connection.exec_driver_sql(
                    sql.SQL("DROP SCHEMA {} CASCADE").format(identifier).as_string()
                )
    finally:
        engine.dispose()
        control.dispose()


@pytest.fixture
def migrated_engine(postgres_engine: Engine) -> Engine:
    with postgres_engine.begin() as connection:
        command.upgrade(build_alembic_config(connection), "head")
    return postgres_engine


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_host="unused",
        database_port=5432,
        database_name="unused",
        database_user="unused",
        database_password="fake-password",
        redis_host="unused",
        redis_port=6379,
        storage_path=tmp_path,
        max_archive_bytes=8192,
        max_uncompressed_bytes=4096,
        max_result_files=10,
        scheduler_interval_seconds=1,
        api_token="fake-test-token",
    )


@pytest.fixture
def service(settings: Settings, migrated_engine: Engine) -> ImportService:
    return ImportService(settings, create_session_factory(migrated_engine), ImportRepository())


@pytest.fixture
def processor(settings: Settings, migrated_engine: Engine) -> ImportProcessor:
    return ImportProcessor(settings, migrated_engine, ImportRepository())


@pytest.fixture
def cleanup(settings: Settings, migrated_engine: Engine) -> ArchiveCleanup:
    return ArchiveCleanup(settings, create_session_factory(migrated_engine), ImportRepository())


def write_result_archive(path: Path) -> None:
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "allure-results/first-result.json",
            json.dumps({
                "name": "First result",
                "steps": [
                    {"name": "Parent", "steps": [{"name": "Child", "start": 10}]},
                    {"name": "Sibling"},
                ],
            }),
        )
        archive.writestr(
            "allure-results/second-result.json",
            json.dumps({"name": "Second result"}),
        )


def make_archive(results: list[dict[str, object]]) -> bytes:
    payload = BytesIO()
    with ZipFile(payload, "w") as archive:
        for position, result in enumerate(results):
            archive.writestr(f"allure-results/{position}-result.json", json.dumps(result))
    return payload.getvalue()


def test_migrations_can_be_reapplied(postgres_engine: Engine) -> None:
    tables = {"imports", "test_results", "test_steps"}
    views = {"dashboard_imports", "dashboard_attempts", "dashboard_final_results"}
    import_id = uuid4()
    with postgres_engine.begin() as connection:
        command.upgrade(build_alembic_config(connection), "head")
        connection.execute(text("INSERT INTO imports (id) VALUES (:id)"), {"id": import_id})

    with postgres_engine.begin() as connection:
        config = build_alembic_config(connection)
        command.upgrade(config, "head")
        command.check(config)
        assert MigrationContext.configure(connection).get_current_heads() == (
            "0004_grafana_read_model",
        )

        inspector = inspect(connection)
        assert set(inspector.get_table_names()) == tables | {"alembic_version"}
        assert views <= set(inspector.get_view_names())
        for view in views:
            assert all(column["comment"].strip() for column in inspector.get_columns(view))
        for table in tables:
            assert inspector.get_table_comment(table)["text"].strip()
            assert all(column["comment"].strip() for column in inspector.get_columns(table))
            assert inspector.get_pk_constraint(table)["name"] == f"pk__{table}"
        assert {item["name"] for item in inspector.get_unique_constraints("test_results")} == {
            "uq__test_results__import_id_position",
        }
        assert {item["name"] for item in inspector.get_check_constraints("imports")} == {
            "ck__imports__status",
        }
        assert {item["name"] for item in inspector.get_foreign_keys("test_results")} == {
            "fk__test_results__import_id__imports",
        }
        assert {item["name"] for item in inspector.get_foreign_keys("test_steps")} == {
            "fk__test_steps__test_id__test_results",
            "fk__test_steps__parent_id__test_steps",
        }
        idempotency_index, = [
            index for index in inspector.get_indexes("imports")
            if index["column_names"] == ["idempotency_key"]
        ]
        assert idempotency_index["unique"] is True
        assert idempotency_index["name"] == "ix__imports__idempotency_key"
        assert "IS NOT NULL" in idempotency_index["dialect_options"]["postgresql_where"]
        row = connection.execute(
            text(
                "SELECT status, attempt_count, archive_deleted, archive_cleanup_after, "
                "idempotency_key, archive_sha256, failed_at FROM imports WHERE id = :id"
            ),
            {"id": import_id},
        ).one()
        assert tuple(row) == ("pending", 0, False, None, None, None, None)

    with postgres_engine.begin() as connection:
        command.downgrade(build_alembic_config(connection), "base")
        inspector = inspect(connection)
        assert tables.isdisjoint(inspector.get_table_names())
        assert views.isdisjoint(inspector.get_view_names())

    with postgres_engine.begin() as connection:
        command.upgrade(build_alembic_config(connection), "head")
        assert tables <= set(inspect(connection).get_table_names())
        assert connection.scalar(text("SELECT count(*) FROM imports")) == 0


def test_retention_migration_backfills_failed_imports_and_preserves_history(
    postgres_engine: Engine,
) -> None:
    statuses = ("pending", "processing", "completed", "failed")
    with postgres_engine.begin() as connection:
        command.upgrade(build_alembic_config(connection), "0001_imports_orm")
        connection.execute(text(
            "INSERT INTO imports (id, status, error_message, created_at) "
            "VALUES (:id, :status, :error_message, now() - interval '30 days')"
        ), [
            {
                "id": uuid4(),
                "status": status,
                "error_message": "old error" if status == "failed" else None,
            }
            for status in statuses
        ])
        before = connection.scalar(text("SELECT clock_timestamp()"))

    with postgres_engine.begin() as connection:
        config = build_alembic_config(connection)
        command.upgrade(config, "head")
        after = connection.scalar(text("SELECT clock_timestamp()"))
        rows = connection.execute(
            text("SELECT status, failed_at, error_message, created_at FROM imports"),
        ).all()

    assert {row.status for row in rows} == set(statuses)
    for row in rows:
        if row.status == "failed":
            assert row.failed_at is not None
            assert row.failed_at.utcoffset() is not None
            assert row.created_at < before <= row.failed_at <= after
            assert row.error_message == "old error"
        else:
            assert row.failed_at is None

    with postgres_engine.begin() as connection:
        command.downgrade(build_alembic_config(connection), "0001_imports_orm")
        columns = inspect(connection).get_columns("imports")
        assert "failed_at" not in {column["name"] for column in columns}
        assert set(connection.scalars(text("SELECT status FROM imports"))) == set(statuses)


def test_analytics_migration_preserves_existing_import_results_and_steps(
    postgres_engine: Engine,
) -> None:
    import_id, test_id, step_id = uuid4(), uuid4(), uuid4()
    with postgres_engine.begin() as connection:
        command.upgrade(build_alembic_config(connection), "0002_failed_archive_retention")
        connection.execute(text(
            "INSERT INTO imports (id, status, archive_deleted, idempotency_key, archive_sha256) "
            "VALUES (:id, 'completed', true, 'jenkins:legacy', :hash)"
        ), {"id": import_id, "hash": "a" * 64})
        connection.execute(text(
            "INSERT INTO test_results (id, import_id, position, name) "
            "VALUES (:id, :import_id, 0, 'old result')"
        ), {"id": test_id, "import_id": import_id})
        connection.execute(text(
            "INSERT INTO test_steps (id, test_id, position, name) "
            "VALUES (:id, :test_id, 0, 'old step')"
        ), {"id": step_id, "test_id": test_id})

    with postgres_engine.begin() as connection:
        config = build_alembic_config(connection)
        command.upgrade(config, "head")
        assert connection.execute(text(
            "SELECT id, status, archive_deleted, idempotency_key, archive_sha256, "
            "project, job, build_number, build_url, revision FROM imports"
        )).one() == (
            import_id, "completed", True, "jenkins:legacy", "a" * 64, None, None, None, None, None,
        )
        assert connection.execute(text(
            "SELECT id, import_id, name, status, history_id FROM test_results"
        )).one() == (test_id, import_id, "old result", None, None)
        assert connection.execute(text(
            "SELECT id, test_id, name, status FROM test_steps"
        )).one() == (step_id, test_id, "old step", None)

    with postgres_engine.begin() as connection:
        command.downgrade(build_alembic_config(connection), "0002_failed_archive_retention")
        assert connection.scalar(text("SELECT id FROM imports")) == import_id
        assert connection.scalar(text("SELECT id FROM test_results")) == test_id
        assert connection.scalar(text("SELECT id FROM test_steps")) == step_id
        columns = inspect(connection).get_columns("imports")
        assert "project" not in {column["name"] for column in columns}


def test_invalid_test_data_fails_first_attempt_and_preserves_zip(
    service: ImportService,
    processor: ImportProcessor,
    settings: Settings,
    migrated_engine: Engine,
) -> None:
    import_id = service.upload(BytesIO(b""), None)["id"]
    archive = settings.storage_path / f"{import_id}.zip"
    with ZipFile(archive, "w") as zip_file:
        zip_file.writestr("first-result.json", json.dumps({"name": "Valid result"}))
        zip_file.writestr("second-result.json", json.dumps({"name": "Invalid", "stop": 2**63}))
    with migrated_engine.connect() as connection:
        before = connection.scalar(text("SELECT clock_timestamp()"))

    processor.process_import(import_id)

    with migrated_engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT status, attempt_count, error_message, failed_at FROM imports WHERE id = :id"
            ),
            {"id": import_id},
        ).mappings().one()
        assert row["status"] == "failed" and row["attempt_count"] == 1
        assert row["error_message"] == "Не удалось прочитать результаты Allure"
        assert row["failed_at"] is not None
        assert row["failed_at"].utcoffset() is not None
        assert before <= row["failed_at"] <= connection.scalar(text("SELECT clock_timestamp()"))
        assert connection.scalar(text("SELECT count(*) FROM test_results")) == 0
    assert archive.is_file()

    assert service.retry_import(import_id) == "pending"
    pending = service.get_import(import_id)
    assert pending is not None and pending["error_message"] is None
    with migrated_engine.connect() as connection:
        assert connection.scalar(text("SELECT failed_at IS NULL FROM imports"))
    write_result_archive(archive)
    processor.process_import(import_id)
    completed = service.get_import(import_id)
    assert completed is not None and completed["status"] == "completed"
    with migrated_engine.connect() as connection:
        assert connection.scalar(text("SELECT attempt_count FROM imports")) == 1


def test_attempt_limit_timestamps_failed_import(
    service: ImportService, processor: ImportProcessor, migrated_engine: Engine,
) -> None:
    import_id = service.upload(BytesIO(b"archive"), None)["id"]
    with migrated_engine.begin() as connection:
        connection.execute(text("UPDATE imports SET status = 'processing', attempt_count = 3"))
        before = connection.scalar(text("SELECT clock_timestamp()"))

    processor.process_import(import_id)

    with migrated_engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT status, attempt_count, failed_at, error_message FROM imports WHERE id = :id"
            ),
            {"id": import_id},
        ).one()
        assert row.status == "failed" and row.attempt_count == 3
        assert row.failed_at is not None
        assert row.failed_at.utcoffset() is not None
        assert before <= row.failed_at <= connection.scalar(text("SELECT clock_timestamp()"))
        assert row.error_message == "Исчерпаны попытки обработки импорта"


def test_processors_contend_for_one_import(
    service: ImportService,
    processor: ImportProcessor,
    settings: Settings,
    migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import_id = service.upload(BytesIO(b""), None)["id"]
    write_result_archive(settings.storage_path / f"{import_id}.zip")
    parsing = Event()
    finish = Event()
    original_parse = processing.parse_archive

    def paused_parse(path: Path, max_bytes: int, max_files: int) -> list[ParsedTest]:
        parsing.set()
        assert finish.wait(10), "Тест не разрешил завершить разбор ZIP"
        return original_parse(path, max_bytes, max_files)

    monkeypatch.setattr(processing, "parse_archive", paused_parse)
    with ThreadPoolExecutor(max_workers=2) as executor:
        winner = executor.submit(processor.process_import, import_id)
        try:
            assert parsing.wait(10), "Первый обработчик не начал разбор ZIP"
            record = service.get_import(import_id)
            assert record is not None and record["status"] == "processing"
            contender = executor.submit(processor.process_import, import_id)
            with pytest.raises(RuntimeError, match="Импорт уже обрабатывается"):
                contender.result(timeout=5)
        finally:
            finish.set()
        winner.result(timeout=10)

    processor.process_import(import_id)
    record = service.get_import(import_id)
    assert record is not None and record["status"] == "completed"
    with migrated_engine.connect() as connection:
        assert connection.scalar(text("SELECT attempt_count FROM imports")) == 1
        assert connection.execute(
            text("SELECT position, name FROM test_results ORDER BY position")
        ).all() == [(0, "First result"), (1, "Second result")]


def test_step_insert_failure_rolls_back_results_and_allows_queued_retry(
    service: ImportService,
    processor: ImportProcessor,
    settings: Settings,
    migrated_engine: Engine,
) -> None:
    import_id = service.upload(BytesIO(b""), None)["id"]
    archive = settings.storage_path / f"{import_id}.zip"
    write_result_archive(archive)
    with migrated_engine.begin() as connection:
        connection.execute(text(
            "ALTER TABLE test_steps ADD CONSTRAINT reject_child CHECK (name <> 'Child')"
        ))

    with pytest.raises(IntegrityError, match="reject_child"):
        processor.process_import(import_id)

    assert archive.is_file()
    record = service.get_import(import_id)
    assert record is not None and record["status"] == "processing"
    with migrated_engine.begin() as connection:
        assert connection.scalar(text("SELECT attempt_count FROM imports")) == 1
        assert connection.scalar(text("SELECT count(*) FROM test_results")) == 0
        assert connection.scalar(text("SELECT count(*) FROM test_steps")) == 0
        connection.execute(text("ALTER TABLE test_steps DROP CONSTRAINT reject_child"))

    processor.process_import(import_id)

    record = service.get_import(import_id)
    assert record is not None and record["status"] == "completed"
    with migrated_engine.connect() as connection:
        assert connection.scalar(text("SELECT attempt_count FROM imports")) == 2
        assert connection.execute(text(
            "SELECT s.name, s.position, p.name AS parent, r.name AS result "
            "FROM test_steps s LEFT JOIN test_steps p ON p.id = s.parent_id "
            "JOIN test_results r ON r.id = s.test_id ORDER BY s.name"
        )).all() == [
            ("Child", 0, "Parent", "First result"),
            ("Parent", 0, None, "First result"),
            ("Sibling", 1, None, "First result"),
        ]


def test_cleanup_reaches_later_zip_despite_one_hundred_failures(
    service: ImportService,
    cleanup: ArchiveCleanup,
    settings: Settings,
    migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed_ids = [uuid4() for _ in range(100)]
    newer_id = uuid4()
    all_ids = [*failed_ids, newer_id]
    storage = settings.storage_path
    failed_paths = {storage / f"{import_id}.zip" for import_id in failed_ids}
    newer_archive = storage / f"{newer_id}.zip"
    for archive in {*failed_paths, newer_archive}:
        archive.touch()
    with migrated_engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO imports (id, status, created_at) "
            "VALUES (:id, 'completed', CURRENT_TIMESTAMP + :position * interval '1 second')"
        ), [{"id": import_id, "position": index} for index, import_id in enumerate(all_ids)])

    original_unlink = Path.unlink
    failures: list[Path] = []

    def fail_on_old_archives(path: Path, *, missing_ok: bool = False) -> None:
        if path in failed_paths:
            failures.append(path)
            raise PermissionError("Archive temporarily locked")
        original_unlink(path, missing_ok=missing_ok)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail_on_old_archives)
        cleanup.cleanup_due_archives()
        assert newer_archive.is_file()
        cleanup.cleanup_due_archives()

    assert len(failures) == 100
    assert set(failures) == failed_paths
    assert not newer_archive.exists()
    assert all(path.is_file() for path in failed_paths)
    row = service.get_import(newer_id)
    assert row is not None and row["archive_deleted"] is True
    with migrated_engine.begin() as connection:
        assert connection.scalar(text(
            "SELECT count(*) FROM imports WHERE archive_cleanup_after > now()"
        )) == 100
        connection.execute(
            text("UPDATE imports SET archive_cleanup_after = now() WHERE id = :id"),
            {"id": failed_ids[0]},
        )

    cleanup.cleanup_due_archives()

    assert not (storage / f"{failed_ids[0]}.zip").exists()
    with migrated_engine.connect() as connection:
        row = connection.execute(
            text("SELECT archive_deleted, archive_cleanup_after FROM imports WHERE id = :id"),
            {"id": failed_ids[0]},
        ).one()
        assert tuple(row) == (True, None)


def test_cleanup_reaches_unlocked_archive_while_another_row_is_locked(
    service: ImportService, cleanup: ArchiveCleanup, settings: Settings, migrated_engine: Engine,
) -> None:
    locked_id = service.upload(BytesIO(b"first"), None)["id"]
    unlocked_id = service.upload(BytesIO(b"second"), None)["id"]
    locked_archive = settings.storage_path / f"{locked_id}.zip"
    unlocked_archive = settings.storage_path / f"{unlocked_id}.zip"
    with migrated_engine.begin() as connection:
        connection.execute(text("UPDATE imports SET status = 'completed'"))

    with ThreadPoolExecutor(max_workers=1) as executor, migrated_engine.begin() as connection:
        connection.execute(
            text("SELECT id FROM imports WHERE id = :id FOR UPDATE"),
            {"id": locked_id},
        )
        executor.submit(cleanup.cleanup_due_archives).result(timeout=5)
        assert locked_archive.is_file()
        assert not unlocked_archive.exists()

    cleanup.cleanup_due_archives()

    assert not locked_archive.exists()
    with migrated_engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM imports WHERE archive_deleted")) == 2


def test_failed_archive_cleanup_uses_failure_date_and_preserves_history(
    service: ImportService, cleanup: ArchiveCleanup, settings: Settings, migrated_engine: Engine,
) -> None:
    recent_id = service.upload(BytesIO(b"recent"), None)["id"]
    expired_id = service.upload(BytesIO(b"expired"), None)["id"]
    with migrated_engine.begin() as connection:
        connection.execute(text(
            "UPDATE imports SET status = 'failed', created_at = now() - interval '30 days', "
            "failed_at = now() - :age, error_message = 'bad test data' WHERE id = :id"
        ), [
            {"id": recent_id, "age": timedelta(days=7) - timedelta(minutes=10)},
            {"id": expired_id, "age": timedelta(days=7)},
        ])
        expired_at = connection.scalar(
            text("SELECT failed_at FROM imports WHERE id = :id"), {"id": expired_id},
        )

    cleanup.cleanup_due_archives()

    assert (settings.storage_path / f"{recent_id}.zip").is_file()
    assert not (settings.storage_path / f"{expired_id}.zip").exists()
    with migrated_engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT status, error_message, archive_deleted, failed_at "
                "FROM imports WHERE id = :id"
            ),
            {"id": expired_id},
        ).one()
    assert tuple(row) == ("failed", "bad test data", True, expired_at)

    with TestClient(create_app(settings, service)) as client:
        response = client.post(
            f"/api/v1/imports/{expired_id}/retry",
            headers={"Authorization": f"Bearer {settings.api_token}"},
        )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"
    assert service.retry_import(recent_id) == "pending"


def test_failed_archive_cleanup_retries_after_unlink_error(
    service: ImportService,
    cleanup: ArchiveCleanup,
    settings: Settings,
    migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import_id = service.upload(BytesIO(b"failed archive"), None)["id"]
    archive = settings.storage_path / f"{import_id}.zip"
    with migrated_engine.begin() as connection:
        connection.execute(text(
            "UPDATE imports SET status = 'failed', failed_at = now() - interval '8 days'"
        ))
    original_unlink = Path.unlink
    attempts: list[Path] = []

    def reject_archive(path: Path, *, missing_ok: bool = False) -> None:
        if path == archive:
            attempts.append(path)
            raise PermissionError("Archive temporarily locked")
        original_unlink(path, missing_ok=missing_ok)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", reject_archive)
        cleanup.cleanup_due_archives()
        cleanup.cleanup_due_archives()

    assert attempts == [archive] and archive.is_file()
    with migrated_engine.begin() as connection:
        assert connection.scalar(text(
            "SELECT NOT archive_deleted AND archive_cleanup_after > now() FROM imports"
        ))
        connection.execute(text("UPDATE imports SET archive_cleanup_after = now()"))

    cleanup.cleanup_due_archives()

    assert not archive.exists()
    with migrated_engine.connect() as connection:
        row = connection.execute(
            text("SELECT status, archive_deleted, archive_cleanup_after FROM imports"),
        ).one()
    assert tuple(row) == ("failed", True, None)


def test_parallel_keyed_upload_and_replay_share_one_archive(
    service: ImportService, processor: ImportProcessor, settings: Settings, migrated_engine: Engine,
) -> None:
    app = create_app(settings, service)
    headers = {"Authorization": f"Bearer {settings.api_token}", "Idempotency-Key": "jenkins:42"}
    barrier = Barrier(2)
    metadata = {
        "project": "qa-lab",
        "job": "team/qa-lab-test-framework",
        "buildNumber": "42",
        "buildUrl": "https://jenkins.example.invalid/job/qa-lab/42/",
        "revision": "cafe123",
    }
    archive = make_archive([
        {
            "name": "First result", "status": "failed", "historyId": "stable-case",
            "steps": [{
                "name": "Outer",
                "status": "broken",
                "steps": [{"name": "Inner", "status": "passed"}],
            }],
        },
        {"name": "Second result", "status": "invalid", "historyId": "  "},
    ])

    def upload(
        payload: bytes, *, fields: dict[str, str] | None = None, parallel: bool = False,
    ) -> Response:
        with TestClient(app) as client:
            if parallel:
                barrier.wait(timeout=5)
            return client.post(
                "/api/v1/imports",
                headers=headers,
                data=metadata if fields is None else fields,
                files={"file": ("results.zip", payload)},
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(upload, archive, parallel=True)
        second = executor.submit(upload, archive, parallel=True)
        responses = [first.result(), second.result()]

    assert [response.status_code for response in responses] == [202, 202]
    import_id = responses[0].json()["importId"]
    assert responses[1].json()["importId"] == import_id
    processor.process_import(UUID(import_id))

    replay = upload(archive, fields={
        "project": "new-project",
        "job": "new-job",
        "buildNumber": "99",
        "buildUrl": "https://jenkins.example.invalid/job/new-job/99/",
        "revision": "deed999",
    })
    conflict = upload(b"different archive")
    assert replay.status_code == 202
    assert replay.json()["importId"] == import_id
    assert replay.json()["status"] == "completed"
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "conflict"
    with migrated_engine.connect() as connection:
        assert connection.execute(text(
            "SELECT project, job, build_number, build_url, revision FROM imports"
        )).one() == (
            "qa-lab",
            "team/qa-lab-test-framework",
            "42",
            "https://jenkins.example.invalid/job/qa-lab/42/",
            "cafe123",
        )
        assert connection.execute(text(
            "SELECT status, history_id FROM test_results ORDER BY position"
        )).all() == [("failed", "stable-case"), (None, None)]
        assert connection.execute(text(
            "SELECT child.name, child.status, parent.name "
            "FROM test_steps child LEFT JOIN test_steps parent ON parent.id = child.parent_id "
            "ORDER BY child.name"
        )).all() == [("Inner", "passed", "Outer"), ("Outer", "broken", None)]
    assert [path.name for path in settings.storage_path.iterdir()] == [f"{import_id}.zip"]
    assert (settings.storage_path / f"{import_id}.zip").read_bytes() == archive


def test_metrics_group_attempts_and_final_results_per_run(
    service: ImportService, processor: ImportProcessor, settings: Settings, migrated_engine: Engine,
) -> None:
    metrics_settings = replace(settings, metrics_projects=("alpha",))
    sessions = create_session_factory(migrated_engine)
    reader = MetricsReader(metrics_settings, sessions)
    first_run = make_archive([
        {"name": "Earlier", "historyId": "case-1", "status": "failed", "start": 0, "stop": 2000},
        {"name": "Same end and start", "historyId": "case-1", "status": "broken",
         "start": 1000, "stop": 2300},
        {"name": "Final attempt", "historyId": "case-1", "status": "passed",
         "start": 1000, "stop": 2300},
        {"name": "Same end but earlier start", "historyId": "case-1", "status": "skipped",
         "start": 500, "stop": 2300},
        {"name": "Earlier end despite later start", "historyId": "case-1", "status": "broken",
         "start": 2200, "stop": 2299},
        {"name": "Missing end", "historyId": "case-1", "status": "broken", "start": 9999},
        {"name": "Negative duration", "status": "failed", "start": 50, "stop": 40},
        {"name": "Unrecognized status", "status": "invalid", "start": 0, "stop": 500},
    ])
    second_run = make_archive([
        {"name": "Next run same case", "historyId": "case-1", "status": "failed",
         "start": 0, "stop": 1000}
    ])
    simple_run = make_archive([{"name": "Legacy result"}])
    authorization = {"Authorization": f"Bearer {settings.api_token}"}

    with TestClient(create_app(metrics_settings, service, metrics_reader=reader)) as client:
        def upload(payload: bytes, key: str, project: str | None) -> UUID:
            response = client.post(
                "/api/v1/imports",
                headers={**authorization, "Idempotency-Key": key},
                data={"project": project} if project is not None else None,
                files={"file": ("results.zip", payload)},
            )
            assert response.status_code == 202
            return UUID(response.json()["importId"])

        first_id = upload(first_run, "jenkins:alpha:1", "alpha")
        second_id = upload(second_run, "jenkins:alpha:2", "alpha")
        unnamed_id = upload(simple_run, "jenkins:unnamed:1", None)
        unapproved_id = upload(simple_run, "jenkins:unapproved:1", "unapproved-project")
        upload(simple_run, "jenkins:pending:1", "alpha")
        for import_id in (first_id, second_id, unnamed_id, unapproved_id):
            processor.process_import(import_id)

        response = client.get("/metrics", headers=authorization)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        assert upload(first_run, "jenkins:alpha:1", "alpha") == first_id
        assert client.get("/metrics", headers=authorization).text == response.text

    samples = {}
    for line in response.text.splitlines():
        if line and not line.startswith("#"):
            name, value = line.rsplit(" ", 1)
            samples[name] = float(value)

    alpha_samples = {
        name: value for name, value in samples.items() if 'project="alpha"' in name and value != 0
    }
    assert alpha_samples == pytest.approx({
        'testbridge_runs{project="alpha"}': 2,
        'testbridge_test_results{project="alpha",kind="attempt",status="passed"}': 1,
        'testbridge_test_results{project="alpha",kind="attempt",status="failed"}': 3,
        'testbridge_test_results{project="alpha",kind="attempt",status="broken"}': 3,
        'testbridge_test_results{project="alpha",kind="attempt",status="skipped"}': 1,
        'testbridge_test_results{project="alpha",kind="attempt",status="unknown"}': 1,
        'testbridge_test_results{project="alpha",kind="final",status="passed"}': 1,
        'testbridge_test_results{project="alpha",kind="final",status="failed"}': 2,
        'testbridge_test_results{project="alpha",kind="final",status="unknown"}': 1,
        'testbridge_test_duration_seconds_sum{project="alpha",kind="attempt"}': 7.999,
        'testbridge_test_duration_seconds_sum{project="alpha",kind="final"}': 2.8,
        'testbridge_test_duration_seconds_count{project="alpha",kind="attempt"}': 7,
        'testbridge_test_duration_seconds_count{project="alpha",kind="final"}': 3,
    })
    assert samples['testbridge_runs{project="unknown"}'] == 1
    assert samples['testbridge_runs{project="other"}'] == 1
    assert "unapproved-project" not in response.text
