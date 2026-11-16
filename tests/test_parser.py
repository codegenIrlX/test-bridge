import json
from pathlib import Path
from struct import unpack_from
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from metrics_srv.domains.imports.parser import InvalidArchiveError, parse_archive


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


@pytest.mark.parametrize(
    "payload",
    [
        json.dumps({"name": "Example", "start": 2**63}),
        json.dumps({"name": "Example", "steps": [{"name": "Step", "stop": 2**63}]}),
        json.dumps({"name": "Example\x00"}),
        json.dumps({"name": "Example", "steps": [{"name": "\ud800"}]}),
        '{"name": "Example", "start": ' + "9" * 10_000 + "}",
    ],
    ids=["test-overflow", "step-overflow", "nul", "lone-surrogate", "huge-json-integer"],
)
def test_invalid_result_values_are_rejected(tmp_path: Path, payload: str) -> None:
    archive = write_archive(tmp_path / "invalid.zip", {"one-result.json": payload})

    with pytest.raises(InvalidArchiveError):
        parse_archive(archive, max_uncompressed_bytes=100_000, max_result_files=10)


def test_accepts_bigint_boundary_and_unicode_name(tmp_path: Path) -> None:
    archive = write_archive(
        tmp_path / "results.zip",
        {"one-result.json": json.dumps({"name": "Проверка ✅", "start": 0, "stop": 2**63 - 1})},
    )

    test, = parse_archive(archive, max_uncompressed_bytes=100_000, max_result_files=10)

    assert (test.name, test.start, test.stop) == ("Проверка ✅", 0, 2**63 - 1)


def test_corrupt_deflate_stream_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.zip"
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("one-result.json", '{"name": "Example"}')
        header_offset = archive.getinfo("one-result.json").header_offset

    data = bytearray(path.read_bytes())
    name_size, extra_size = unpack_from("<HH", data, header_offset + 26)
    payload_offset = header_offset + 30 + name_size + extra_size
    # Первые три бита 111 задают последний блок с недопустимым типом сжатия.
    data[payload_offset] |= 0b111
    path.write_bytes(data)

    with pytest.raises(InvalidArchiveError):
        parse_archive(path, max_uncompressed_bytes=100_000, max_result_files=10)


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
