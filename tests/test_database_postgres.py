import json
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4
from zipfile import ZipFile

import pytest
from alembic import command
from sqlalchemy import Engine, create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DataError
from sqlalchemy.pool import NullPool

from metrics_srv import import_processing
from metrics_srv.config import Settings
from metrics_srv.import_processing import ImportProcessor
from metrics_srv.migrations import build_alembic_config
from metrics_srv.models import Import
from metrics_srv.repository import Repository


@pytest.fixture
def postgres_engine() -> Iterator[Engine]:
    connection_url = os.getenv("TESTBRIDGE_TEST_POSTGRES_URL")
    if not connection_url:
        pytest.skip("Укажите TESTBRIDGE_TEST_POSTGRES_URL для проверки на PostgreSQL 17")
    url = make_url(connection_url)
    if url.drivername != "postgresql+psycopg":
        pytest.fail("Тестовый URL должен использовать postgresql+psycopg")

    schema = f"testbridge_test_{uuid4().hex}"
    control = create_engine(url, poolclass=NullPool)
    engine = create_engine(
        url,
        poolclass=NullPool,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    try:
        with control.begin() as connection:
            version = connection.scalar(text("SHOW server_version_num"))
            assert int(version) // 10000 == 17, "Для тестов требуется PostgreSQL 17"
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        try:
            with engine.connect() as connection:
                assert connection.scalar(text("SELECT current_schema()")) == schema
            yield engine
        finally:
            with control.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    finally:
        engine.dispose()
        control.dispose()


@pytest.fixture
def repository(postgres_engine: Engine, tmp_path: Path) -> Repository:
    with postgres_engine.connect() as connection:
        command.upgrade(build_alembic_config(connection), "head")
    settings = Settings(
        database_host="unused",
        database_port=5432,
        database_name="unused",
        database_user="unused",
        database_password="unused",
        redis_host="unused",
        redis_port=6379,
        storage_path=tmp_path,
        max_archive_bytes=8192,
        max_uncompressed_bytes=4096,
        max_result_files=10,
        scheduler_interval_seconds=1,
        api_token="unused",
    )
    return Repository(settings, engine=postgres_engine)


def write_result_archive(path: Path, *, child_start: int = 10) -> None:
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "allure-results/first-result.json",
            json.dumps({
                "name": "First result",
                "steps": [
                    {"name": "Parent", "steps": [{"name": "Child", "start": child_start}]},
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
    with postgres_engine.connect() as connection:
        config = build_alembic_config(connection)
        command.upgrade(config, "head")
        command.upgrade(config, "head")
        command.check(config)
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
        assert {item["name"] for item in inspector.get_foreign_keys("test_steps")} == {
            "fk__test_steps__test_id__test_results",
            "fk__test_steps__parent_id__test_steps",
        }
        row = connection.execute(
            text(
                "INSERT INTO imports (id) VALUES (:id) "
                "RETURNING status, attempt_count, archive_deleted, created_at"
            ),
            {"id": import_id},
        ).one()
        assert row[:3] == ("pending", 0, False)
        assert isinstance(row.created_at, datetime)
        connection.commit()

        command.downgrade(config, "base")
        assert tables.isdisjoint(inspect(connection).get_table_names())
        connection.commit()
        command.upgrade(config, "head")
        assert tables <= set(inspect(connection).get_table_names())
        assert connection.scalar(text("SELECT count(*) FROM imports")) == 0


def test_processors_contend_for_one_import(
    repository: Repository, postgres_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    import_id = uuid4()
    repository.create_import(import_id)
    write_result_archive(repository.settings.storage_path / f"{import_id}.zip")
    processor = ImportProcessor(repository.settings, repository)
    parsing = Event()
    finish = Event()
    original_parse = import_processing.parse_archive

    def paused_parse(path: Path, max_bytes: int, max_files: int):
        parsing.set()
        assert finish.wait(10), "Тест не разрешил завершить разбор ZIP"
        return original_parse(path, max_bytes, max_files)

    monkeypatch.setattr(import_processing, "parse_archive", paused_parse)
    with ThreadPoolExecutor(max_workers=2) as executor:
        winner = executor.submit(processor.process_import, import_id)
        try:
            assert parsing.wait(10), "Первый обработчик не начал разбор ZIP"
            assert repository.get_import(import_id)["status"] == "processing"
            contender = executor.submit(processor.process_import, import_id)
            with pytest.raises(RuntimeError, match="Импорт уже обрабатывается"):
                contender.result(timeout=5)
        finally:
            finish.set()
        winner.result(timeout=10)

    processor.process_import(import_id)
    assert repository.get_import(import_id)["status"] == "completed"
    with postgres_engine.connect() as connection:
        assert connection.scalar(select(Import.attempt_count)) == 1
        assert connection.execute(
            text("SELECT position, name FROM test_results ORDER BY position")
        ).all() == [(0, "First result"), (1, "Second result")]


def test_failed_batch_rolls_back_then_retry_records_nested_steps(
    repository: Repository, postgres_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    import_id = uuid4()
    archive = repository.settings.storage_path / f"{import_id}.zip"
    repository.create_import(import_id)
    write_result_archive(archive, child_start=2**63)
    processor = ImportProcessor(repository.settings, repository)

    with pytest.raises(DataError):
        processor.process_import(import_id)

    assert archive.is_file()
    assert repository.get_import(import_id)["status"] == "processing"
    with postgres_engine.connect() as connection:
        assert connection.scalar(select(Import.attempt_count)) == 1
        assert connection.scalar(text("SELECT count(*) FROM test_results")) == 0
        assert connection.scalar(text("SELECT count(*) FROM test_steps")) == 0

    write_result_archive(archive)
    processor.process_import(import_id)
    assert repository.get_import(import_id)["status"] == "completed"
    with postgres_engine.connect() as connection:
        assert connection.scalar(select(Import.attempt_count)) == 2
        steps = connection.execute(
            text(
                "SELECT s.name, s.position, p.name AS parent, r.name AS result "
                "FROM test_steps s LEFT JOIN test_steps p ON p.id = s.parent_id "
                "JOIN test_results r ON r.id = s.test_id ORDER BY s.name"
            )
        ).all()
        assert steps == [
            ("Child", 0, "Parent", "First result"),
            ("Parent", 0, None, "First result"),
            ("Sibling", 1, None, "First result"),
        ]

    def reject_archive_removal(path: Path, *, missing_ok: bool = False) -> None:
        assert path == archive
        raise PermissionError("Файл ещё открыт")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", reject_archive_removal)
        repository.cleanup_completed()
    assert archive.is_file()
    assert repository.get_import(import_id)["archive_deleted"] is False
    repository.cleanup_completed()
    assert not archive.exists()
    assert repository.get_import(import_id)["archive_deleted"] is True


def test_invalid_archive_remains_available_for_manual_retry(repository: Repository) -> None:
    import_id = uuid4()
    archive = repository.settings.storage_path / f"{import_id}.zip"
    repository.create_import(import_id)
    archive.write_bytes(b"not a zip")
    processor = ImportProcessor(repository.settings, repository)

    processor.process_import(import_id)

    assert archive.is_file()
    record = repository.get_import(import_id)
    assert record["status"] == "failed"
    assert record["error_message"]
    assert repository.retry_import(import_id) == "pending"
    assert repository.get_import(import_id)["error_message"] is None
    write_result_archive(archive)
    processor.process_import(import_id)
    assert repository.get_import(import_id)["status"] == "completed"


def test_exhausted_attempts_mark_import_failed_without_deleting_archive(
    repository: Repository, postgres_engine: Engine
) -> None:
    import_id: UUID = uuid4()
    archive = repository.settings.storage_path / f"{import_id}.zip"
    repository.create_import(import_id)
    write_result_archive(archive)
    with postgres_engine.begin() as connection:
        connection.execute(text("UPDATE imports SET status = 'processing', attempt_count = 3"))

    ImportProcessor(repository.settings, repository).process_import(import_id)

    assert repository.get_import(import_id)["status"] == "failed"
    assert archive.is_file()
    with postgres_engine.connect() as connection:
        assert connection.scalar(select(Import.attempt_count)) == 3
        assert connection.scalar(text("SELECT count(*) FROM test_results")) == 0
