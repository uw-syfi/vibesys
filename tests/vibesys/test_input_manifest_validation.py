"""Validation errors raised by input manifest models and bundle loading."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError

from vibesys.evaluators.input_manifest import (
    BenchmarkResult,
    EvaluatorInput,
    InputCommand,
    InputManifest,
    WorkspaceSource,
    load_input_bundle,
)

if TYPE_CHECKING:
    from pathlib import Path

_COMMIT = "0123456789abcdef"


def _source(**overrides: str) -> dict[str, str]:
    fields = {"name": "src", "repo": "https://example.com/r.git", "commit": _COMMIT, "dest": "d"}
    fields.update(overrides)
    return fields


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"command": ()}, "at least one argv element"),
        ({"command": ("python", "")}, "command elements must be non-empty"),
        ({"entrypoint": ""}, "entrypoint must be a non-empty name"),
        ({"entrypoint": "two words"}, "entrypoint must be a non-empty name"),
        ({"entrypoint": "check", "args": ("ok", "")}, "args elements must be non-empty"),
        ({}, "exactly one of command or entrypoint"),
        ({"command": ("a",), "entrypoint": "b"}, "exactly one of command or entrypoint"),
        ({"command": ("a",), "args": ("x",)}, "args may only be used with an evaluator"),
    ],
)
def test_input_command_rejects_invalid_declarations(fields: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        InputCommand(**fields)


def test_input_command_display_quotes_argv_and_requires_resolution() -> None:
    assert InputCommand(command=("python", "a b")).display() == "python 'a b'"
    assert InputCommand(command=("x",)).display(resolved_command=("y", "$z")) == "y '$z'"
    with pytest.raises(ValueError, match="must be resolved before display"):
        InputCommand(entrypoint="check").display()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"name": ""}, "name must be non-empty"),
        ({"name": "a b"}, "name must not contain whitespace"),
        ({"repo": "  "}, "repo must be non-empty"),
        ({"repo": "ftp://example.com/r"}, "unsupported repo URL scheme: ftp"),
        ({"commit": ""}, "commit must be non-empty"),
        ({"commit": "abc"}, "7-64 character hexadecimal"),
        ({"commit": "zzzzzzzz"}, "7-64 character hexadecimal"),
        ({"dest": " "}, "dest must be a non-empty path"),
        ({"dest": "/abs"}, "dest must be relative to the workspace"),
        ({"dest": "a/../b"}, "empty, current, or parent path components"),
    ],
)
def test_workspace_source_rejects_invalid_fields(overrides: dict[str, str], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        WorkspaceSource(**_source(**overrides))


def test_workspace_source_normalizes_commit_case() -> None:
    assert WorkspaceSource(**_source(commit="ABCDEF1")).commit == "abcdef1"


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"source": " "}, "source must be a non-empty path"),
        ({"source": "/abs"}, "source must be relative to the input bundle"),
        ({"source": "eval", "name": "pkg"}, "cannot be combined with name or version"),
        ({"source": "eval", "version": "1"}, "cannot be combined with name or version"),
        ({"name": "pkg"}, "requires both name and version"),
        ({}, "requires both name and version"),
    ],
)
def test_evaluator_input_rejects_invalid_combinations(fields: dict[str, str], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        EvaluatorInput(**fields)


def test_evaluator_input_accepts_source_and_none() -> None:
    assert EvaluatorInput(source="eval").package_requirement is None


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"json_argument": "out", "metric": "m"}, "one option-style argv element"),
        ({"json_argument": "--a b", "metric": "m"}, "one option-style argv element"),
        ({"json_argument": "--json", "metric": ""}, "metric must be a non-empty JSON field"),
        ({"json_argument": "--json", "metric": "a b"}, "metric must be a non-empty JSON field"),
    ],
)
def test_benchmark_result_rejects_invalid_fields(fields: dict[str, str], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        BenchmarkResult(**fields)


def _manifest(overrides: dict[str, Any]) -> dict[str, Any]:
    document: dict[str, Any] = {
        "version": 1,
        "agent": {"domain": "generic"},
        "accuracy": {"command": ["python", "acc.py"]},
        "benchmark": {"command": ["python", "bench.py"]},
    }
    document.update(overrides)
    return document


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {"accuracy": {"entrypoint": "check"}},
            "evaluator entrypoints require a packaged",
        ),
        (
            {"evaluator": {"name": "pkg", "version": "1.0"}},
            "requires evaluator entrypoint commands",
        ),
        (
            {"workspace": {"sources": [_source(), _source(dest="other")]}},
            "duplicate workspace source name: src",
        ),
        (
            {"workspace": {"sources": [_source(), _source(name="two")]}},
            "duplicate workspace source destination: d",
        ),
    ],
)
def test_manifest_rejects_inconsistent_cross_references(
    overrides: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        InputManifest.model_validate(_manifest(overrides))


_VALID_TOML = """version = 1
[agent]
domain = "generic"
[accuracy]
command = ["python", "acc.py"]
[benchmark]
command = ["{benchmark}"]
"""


def _bundle(tmp_path: Path, benchmark: str = "python") -> Path:
    (tmp_path / "vibesys.input.toml").write_text(_VALID_TOML.format(benchmark=benchmark))
    (tmp_path / "OBJECTIVE.md").write_text("objective\n")
    return tmp_path


def test_load_input_bundle_rejects_missing_or_non_directory_root(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="--input path does not exist"):
        load_input_bundle(tmp_path / "missing")
    file_path = tmp_path / "file"
    file_path.write_text("x")
    with pytest.raises(ValueError, match="--input path is not a directory"):
        load_input_bundle(file_path)


def test_load_input_bundle_reports_missing_files_and_invalid_manifest(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="Input manifest not found"):
        load_input_bundle(tmp_path)
    (tmp_path / "vibesys.input.toml").write_text("version = 2\n")
    with pytest.raises(FileNotFoundError, match=r"OBJECTIVE\.md not found"):
        load_input_bundle(tmp_path)
    (tmp_path / "OBJECTIVE.md").write_text("objective\n")
    with pytest.raises(ValueError, match="Invalid input manifest"):
        load_input_bundle(tmp_path)


def test_load_input_bundle_keeps_bare_executable_and_checks_relative_ones(
    tmp_path: Path,
) -> None:
    bundle = load_input_bundle(_bundle(tmp_path))
    assert bundle.resolved_benchmark_command == ("python",)
    assert bundle.resolved_accuracy_command == ("python", "acc.py")

    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "run.sh").write_text("#!/bin/sh\n")
    _bundle(tmp_path, "bin/run.sh")
    assert load_input_bundle(tmp_path).resolved_benchmark_command == ("bin/run.sh",)


@pytest.mark.parametrize(
    ("benchmark", "error", "message"),
    [
        ("/usr/bin/env", ValueError, "benchmark.command executable must be relative"),
        ("../outside/run.sh", ValueError, "benchmark.command executable escapes the project"),
        ("bin/missing.sh", FileNotFoundError, "benchmark.command executable does not exist"),
        ("bin/.", ValueError, "benchmark.command executable is not a file"),
    ],
)
def test_load_input_bundle_rejects_bad_executables(
    tmp_path: Path, benchmark: str, error: type[Exception], message: str
) -> None:
    (tmp_path / "bin").mkdir()
    _bundle(tmp_path, benchmark)
    with pytest.raises(error, match=message):
        load_input_bundle(tmp_path)


def test_load_input_bundle_rejects_reference_that_is_a_file(tmp_path: Path) -> None:
    _bundle(tmp_path)
    (tmp_path / "reference").write_text("not a dir")
    with pytest.raises(ValueError, match="reference path is not a directory"):
        load_input_bundle(tmp_path)


@pytest.mark.parametrize(
    ("entrypoint", "setup", "error", "message"),
    [
        ("missing.py", None, FileNotFoundError, "environment.modal.entrypoint does not exist"),
        ("adir", "dir", ValueError, "environment.modal.entrypoint is not a file"),
    ],
)
def test_load_input_bundle_validates_modal_entrypoint(
    tmp_path: Path, entrypoint: str, setup: str | None, error: type[Exception], message: str
) -> None:
    _bundle(tmp_path)
    if setup == "dir":
        (tmp_path / entrypoint).mkdir()
    manifest = tmp_path / "vibesys.input.toml"
    manifest.write_text(
        manifest.read_text() + f'[environment.modal]\nentrypoint = "{entrypoint}"\n'
    )
    with pytest.raises(error, match=message):
        load_input_bundle(tmp_path)
