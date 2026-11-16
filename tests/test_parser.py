import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from metrics_srv.parser import InvalidArchiveError, parse_archive


def write_archive(path: Path, entries: dict[str, str | bytes]) -> Path:
    with ZipFile(path, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return path


def test_parse_results_preserves_test_and_nested_steps(tmp_path: Path) -> None:
    result = {
        "name": "Order can be created",
        "start": 1700000000000,
        "stop": 1700000000500,
        "steps": [
            {
                "name": "Submit order",
                "start": 1700000000010,
                "stop": 1700000000400,
                "steps": [{"name": "Check response", "start": 1700000000020}],
            },
            {"name": "Check receipt"},
        ],
        "attachments": [{"source": "secret-attachment.txt"}],
    }
    archive = write_archive(
        tmp_path / "results.zip",
        {
            "allure-results/example-result.json": json.dumps(result),
            "allure-results/example-container.json": "invalid fixture data",
            "allure-results/secret-attachment.txt": "ignored attachment",
            "allure-report/data/test-cases/example.json": "invalid generated report",
        },
    )

    tests = parse_archive(archive, max_uncompressed_bytes=100_000, max_result_files=10)

    assert len(tests) == 1
    test = tests[0]
    assert (test.name, test.start, test.stop) == (
        "Order can be created",
        1700000000000,
        1700000000500,
    )
    assert [step.name for step in test.steps] == ["Submit order", "Check receipt"]
    assert (test.steps[0].start, test.steps[0].stop) == (1700000000010, 1700000000400)
    assert test.steps[0].steps[0].name == "Check response"
    assert test.steps[0].steps[0].start == 1700000000020
    assert test.steps[0].steps[0].stop is None
    assert test.steps[1].start is None
    assert test.steps[1].stop is None


def test_parse_multiple_results_without_steps_or_times(tmp_path: Path) -> None:
    archive = write_archive(
        tmp_path / "results.zip",
        {
            "one-result.json": json.dumps({"name": "One"}),
            "allure-results/two-result.json": json.dumps({"name": "Two", "steps": []}),
        },
    )

    tests = parse_archive(archive, max_uncompressed_bytes=100_000, max_result_files=10)

    assert {test.name for test in tests} == {"One", "Two"}
    assert all(test.start is None and test.stop is None and not test.steps for test in tests)


@pytest.mark.parametrize("status", ["passed", "failed", "broken", "skipped", "unknown"])
def test_parse_status_and_allure_identity(tmp_path: Path, status: str) -> None:
    result = {
        "name": "Parameterized test",
        "status": status,
        "uuid": "attempt-1",
        "historyId": "history-1",
        "testCaseId": "case-1",
        "fullName": "suite.test_parameterized",
        "labels": [
            {"name": "suite", "value": "suite-a"},
            {"name": "tag", "value": "smoke"},
            {"name": "tag", "value": "api"},
        ],
        "parameters": [{"name": "region", "value": "test-region"}],
        "steps": [
            {"name": "First", "status": "failed", "steps": [
                {"name": "Nested", "status": "broken"}
            ]}
        ],
    }
    archive = write_archive(tmp_path / "results.zip", {"one-result.json": json.dumps(result)})

    tests = parse_archive(archive, max_uncompressed_bytes=100_000, max_result_files=10)

    assert len(tests) == 1
    test = tests[0]
    assert test.status == status
    assert (test.uuid, test.history_id, test.test_case_id, test.full_name) == (
        "attempt-1", "history-1", "case-1", "suite.test_parameterized"
    )
    assert test.labels == result["labels"]
    assert test.parameters == result["parameters"]
    assert test.steps[0].status == "failed"
    assert test.steps[0].steps[0].status == "broken"


def test_parse_attempts_and_missing_optional_fields(tmp_path: Path) -> None:
    archive = write_archive(
        tmp_path / "results.zip",
        {
            "one-result.json": json.dumps(
                {"name": "Test", "uuid": "first", "historyId": "same", "status": "failed"}
            ),
            "two-result.json": json.dumps(
                {"name": "Test", "uuid": "second", "historyId": "same", "status": "passed"}
            ),
            "three-result.json": json.dumps(
                {"name": "No optional fields", "steps": [{"name": "Step"}]}
            ),
        },
    )

    tests = parse_archive(archive, max_uncompressed_bytes=100_000, max_result_files=10)

    assert len(tests) == 3
    assert [(item.uuid, item.history_id, item.status) for item in tests[:2]] == [
        ("first", "same", "failed"), ("second", "same", "passed")
    ]
    assert tests[2].status is None
    assert tests[2].uuid is None
    assert tests[2].history_id is None
    assert tests[2].labels == []
    assert tests[2].parameters == []
    assert tests[2].steps[0].status is None


def test_masked_and_hidden_parameters_do_not_expose_values(tmp_path: Path) -> None:
    result = {
        "name": "Parameters",
        "parameters": [
            {"name": "visible", "value": "visible-value"},
            {"name": "masked", "value": "private-1", "mode": "masked"},
            {"name": "hidden", "value": "private-2", "mode": "hidden"},
        ],
    }
    archive = write_archive(tmp_path / "results.zip", {"one-result.json": json.dumps(result)})

    tests = parse_archive(archive, max_uncompressed_bytes=100_000, max_result_files=10)

    assert tests[0].parameters == [
        {"name": "visible", "value": "visible-value"},
        {"name": "masked", "mode": "masked"},
        {"name": "hidden", "mode": "hidden"},
    ]


def test_invalid_parameter_mode_is_rejected(tmp_path: Path) -> None:
    result = {"name": "Test", "parameters": [{"name": "value", "mode": ["masked"]}]}
    archive = write_archive(tmp_path / "results.zip", {"one-result.json": json.dumps(result)})

    with pytest.raises(InvalidArchiveError):
        parse_archive(archive, max_uncompressed_bytes=100_000, max_result_files=10)


@pytest.mark.parametrize(
    "entries",
    [
        {},
        {"allure-results/example-container.json": "{}"},
        {"allure-results/example-result.json": "{invalid json"},
        {"allure-results/example-result.json": "{}"},
    ],
    ids=["empty-archive", "only-fixtures", "invalid-json", "missing-test-name"],
)
def test_invalid_results_are_rejected(tmp_path: Path, entries: dict[str, str]) -> None:
    archive = write_archive(tmp_path / "invalid.zip", entries)

    with pytest.raises(InvalidArchiveError):
        parse_archive(archive, max_uncompressed_bytes=100_000, max_result_files=10)


def test_corrupt_archive_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "corrupt.zip"
    archive.write_bytes(b"this is not a zip archive")

    with pytest.raises(InvalidArchiveError):
        parse_archive(archive, max_uncompressed_bytes=100_000, max_result_files=10)


def test_limits_number_of_results(tmp_path: Path) -> None:
    archive = write_archive(
        tmp_path / "results.zip",
        {
            "one-result.json": json.dumps({"name": "One"}),
            "two-result.json": json.dumps({"name": "Two"}),
        },
    )

    with pytest.raises(InvalidArchiveError):
        parse_archive(archive, max_uncompressed_bytes=100_000, max_result_files=1)


def test_limits_uncompressed_result_size(tmp_path: Path) -> None:
    archive = write_archive(
        tmp_path / "results.zip", {"one-result.json": json.dumps({"name": "Long name"})}
    )

    with pytest.raises(InvalidArchiveError):
        parse_archive(archive, max_uncompressed_bytes=10, max_result_files=10)
