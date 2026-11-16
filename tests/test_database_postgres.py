import json
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from threading import Barrier, Event
from uuid import uuid4
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


def test_initial_migration_can_be_reapplied(postgres_engine: Engine) -> None:
    tables = {"imports", "test_results", "test_steps"}
    import_id = uuid4()
    with postgres_engine.begin() as connection:
        command.upgrade(build_alembic_config(connection), "head")
        connection.execute(text("INSERT INTO imports (id) VALUES (:id)"), {"id": import_id})

    with postgres_engine.begin() as connection:
        config = build_alembic_config(connection)
        command.upgrade(config, "head")
        command.check(config)
        assert MigrationContext.configure(connection).get_current_heads() == ("0001_imports_orm",)

        inspector = inspect(connection)
        assert set(inspector.get_table_names()) == tables | {"alembic_version"}
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
                "idempotency_key, archive_sha256 FROM imports WHERE id = :id"
            ),
            {"id": import_id},
        ).one()
        assert tuple(row) == ("pending", 0, False, None, None, None)

    with postgres_engine.begin() as connection:
        command.downgrade(build_alembic_config(connection), "base")
        assert tables.isdisjoint(inspect(connection).get_table_names())

    with postgres_engine.begin() as connection:
        command.upgrade(build_alembic_config(connection), "head")
        assert tables <= set(inspect(connection).get_table_names())
        assert connection.scalar(text("SELECT count(*) FROM imports")) == 0


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

    processor.process_import(import_id)

    with migrated_engine.connect() as connection:
        row = connection.execute(
            text("SELECT status, attempt_count, error_message FROM imports WHERE id = :id"),
            {"id": import_id},
        ).mappings().one()
        assert dict(row) == {
            "status": "failed",
            "attempt_count": 1,
            "error_message": "Не удалось прочитать результаты Allure",
        }
        assert connection.scalar(text("SELECT count(*) FROM test_results")) == 0
    assert archive.is_file()

    assert service.retry_import(import_id) == "pending"
    pending = service.get_import(import_id)
    assert pending is not None and pending["error_message"] is None
    write_result_archive(archive)
    processor.process_import(import_id)
    completed = service.get_import(import_id)
    assert completed is not None and completed["status"] == "completed"
    with migrated_engine.connect() as connection:
        assert connection.scalar(text("SELECT attempt_count FROM imports")) == 1


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
        cleanup.cleanup_completed()
        assert newer_archive.is_file()
        cleanup.cleanup_completed()

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

    cleanup.cleanup_completed()

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
        executor.submit(cleanup.cleanup_completed).result(timeout=5)
        assert locked_archive.is_file()
        assert not unlocked_archive.exists()

    cleanup.cleanup_completed()

    assert not locked_archive.exists()
    with migrated_engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM imports WHERE archive_deleted")) == 2


def test_parallel_keyed_upload_and_replay_share_one_archive(
    service: ImportService, settings: Settings, migrated_engine: Engine,
) -> None:
    app = create_app(settings, service)
    headers = {"Authorization": f"Bearer {settings.api_token}", "Idempotency-Key": "jenkins:42"}
    barrier = Barrier(2)

    def upload(payload: bytes, *, parallel: bool = False) -> Response:
        with TestClient(app) as client:
            if parallel:
                barrier.wait(timeout=5)
            return client.post(
                "/api/v1/imports", headers=headers, files={"file": ("results.zip", payload)},
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(upload, b"one archive", parallel=True)
        second = executor.submit(upload, b"one archive", parallel=True)
        responses = [first.result(), second.result()]

    assert [response.status_code for response in responses] == [202, 202]
    import_id = responses[0].json()["importId"]
    assert responses[1].json()["importId"] == import_id
    with migrated_engine.begin() as connection:
        connection.execute(text("UPDATE imports SET status = 'completed'"))

    replay = upload(b"one archive")
    conflict = upload(b"different archive")
    assert replay.status_code == 202
    assert replay.json()["importId"] == import_id
    assert replay.json()["status"] == "completed"
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "conflict"
    with migrated_engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM imports")) == 1
    assert [path.name for path in settings.storage_path.iterdir()] == [f"{import_id}.zip"]
    assert (settings.storage_path / f"{import_id}.zip").read_bytes() == b"one archive"
