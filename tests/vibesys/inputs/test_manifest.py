"""Validation errors raised by input manifest models and bundle loading."""

from __future__ import annotations

import tempfile
import tomllib
from pathlib import Path
from typing import Any, Literal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError
from tests.support.task_project import task_path, write_task

from vibesys.inputs import (
    BenchmarkResult,
    EvaluatorInput,
    InputCommand,
    InputManifest,
    InputManifestParseError,
    WorkspaceSource,
    load_task,
    render_input_manifest,
)
from vs_project.api import AmbiguousTaskError, InvalidTaskDefinitionError
from vs_sim.api.testing import examples

_COMMIT = "0123456789abcdef"
_BENCHMARK_CONTRACTS = st.one_of(
    st.none(),
    st.builds(
        BenchmarkResult,
        json_argument=st.text(
            alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_",
            min_size=1,
            max_size=20,
        ).map(lambda suffix: f"--{suffix}"),
        metric=st.text(
            alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_",
            min_size=1,
            max_size=20,
        ),
    ),
    st.just(2),
)


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
        ({"source": "/abs"}, "source must be relative to the task directory"),
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


def test_benchmark_result_protocol_round_trips_at_the_benchmark_table() -> None:
    manifest = InputManifest.model_validate(
        {
            "version": 1,
            "agent": {"domain": "generic"},
            "accuracy": {"command": ["python", "acc.py"]},
            "benchmark": {
                "command": ["python", "bench.py"],
                "timeout_seconds": 30,
                "result_protocol": 2,
            },
        }
    )

    rendered = render_input_manifest(manifest)

    assert (
        rendered
        == """\
version = 1

[agent]
domain = "generic"

[accuracy]
command = ["python", "acc.py"]

[benchmark]
command = ["python", "bench.py"]
timeout_seconds = 30
result_protocol = 2
"""
    )
    assert InputManifest.model_validate(tomllib.loads(rendered)) == manifest


@settings(max_examples=examples(pr=8, nightly=100))
@given(contract=_BENCHMARK_CONTRACTS)
def test_benchmark_contract_alternatives_round_trip_through_manifest_toml(
    contract: BenchmarkResult | Literal[2] | None,
) -> None:
    benchmark: dict[str, object] = {"command": ["python", "bench.py"]}
    if isinstance(contract, BenchmarkResult):
        benchmark["result"] = contract
    elif contract is not None:
        benchmark["result_protocol"] = contract
    manifest = InputManifest.model_validate(
        {
            "version": 1,
            "agent": {"domain": "generic"},
            "accuracy": {"command": ["python", "acc.py"]},
            "benchmark": benchmark,
        }
    )

    reparsed = InputManifest.model_validate(tomllib.loads(render_input_manifest(manifest)))

    assert reparsed == manifest


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


_PLAIN_PATH = st.lists(
    st.text(alphabet="abcXYZ019_-.", min_size=1, max_size=6).filter(
        lambda part: part not in {".", ".."}
    ),
    min_size=1,
    max_size=3,
).map("/".join)


@settings(max_examples=examples(pr=20, nightly=200))
@given(
    unread=st.lists(
        st.tuples(_PLAIN_PATH, st.booleans()).map(lambda item: item[0] + ("/" if item[1] else "")),
        min_size=1,
        max_size=3,
    ),
    sources=st.booleans(),
)
def test_unread_workspace_paths_round_trip_through_manifest_toml(
    *, unread: list[str], sources: bool
) -> None:
    workspace = {"unread": unread, "sources": [_source()] if sources else []}
    manifest = InputManifest.model_validate(_manifest({"workspace": workspace}))
    assert InputManifest.model_validate(tomllib.loads(render_input_manifest(manifest))) == manifest


@pytest.mark.parametrize(
    "path", ["", "/abs", "../up", "a/../b", "a//b", "./a", "notes/*", "a?", "[ab]", ":(top)a", "!a"]
)
def test_an_unread_path_that_is_not_plain_is_rejected_naming_it(path: str) -> None:
    with pytest.raises(ValidationError, match=r"unread path .* must be a plain relative"):
        InputManifest.model_validate(_manifest({"workspace": {"unread": [path]}}))


_VALID_TOML = """version = 1
[agent]
domain = "generic"
[accuracy]
command = ["python", "acc.py"]
[benchmark]
command = ["{benchmark}"]
"""


def _bundle(tmp_path: Path, benchmark: str = "python") -> Path:
    write_task(tmp_path, _VALID_TOML.format(benchmark=benchmark))
    return tmp_path


def test_load_task_rejects_missing_or_non_directory_root(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="project path does not exist"):
        load_task(tmp_path / "missing")
    file_path = tmp_path / "file"
    file_path.write_text("x")
    with pytest.raises(ValueError, match="project path is not a directory"):
        load_task(file_path)


def test_load_task_reports_missing_files_and_invalid_manifest(tmp_path: Path) -> None:
    task = task_path(tmp_path)
    task.mkdir(parents=True)
    with pytest.raises(InvalidTaskDefinitionError, match=r"OBJECTIVE\.md"):
        load_task(tmp_path)
    (task / "OBJECTIVE.md").write_text("objective\n")
    with pytest.raises(InvalidTaskDefinitionError, match=r"vibesys\.input\.toml"):
        load_task(tmp_path)
    (task / "vibesys.input.toml").write_text("version = 2\n")
    with pytest.raises(ValueError, match="Invalid input manifest"):
        load_task(tmp_path)
    (task / "vibesys.input.toml").write_text("version = [\n")
    with pytest.raises(InputManifestParseError, match="Invalid input manifest") as raised:
        load_task(tmp_path)
    assert raised.value.path == task / "vibesys.input.toml"


_TASK_NAMES = st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=8)


@settings(max_examples=examples(pr=8, nightly=100))
@given(task=_TASK_NAMES, has_tasks=st.booleans())
def test_load_task_rejects_a_root_manifest_naming_the_task_location(
    *, task: str, has_tasks: bool
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        if has_tasks:
            write_task(root, _VALID_TOML.format(benchmark="python"), task=task)
        (root / "vibesys.input.toml").write_text(_VALID_TOML.format(benchmark="python"))

        with pytest.raises(ValueError, match="root-level input manifest") as info:
            load_task(root, task if has_tasks else None)

    message = str(info.value)
    assert str((root / "vibesys.input.toml").resolve()) in message
    assert ".vibesys/tasks/<task>/vibesys.input.toml" in message


def test_load_task_requires_a_tasks_directory(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not exist") as info:
        load_task(tmp_path)

    assert str(tmp_path.resolve() / ".vibesys" / "tasks") in str(info.value)


def test_load_task_selects_the_only_task_or_the_named_one(tmp_path: Path) -> None:
    write_task(tmp_path, _VALID_TOML.format(benchmark="python"), task="alpha")
    assert load_task(tmp_path).task_name == "alpha"
    write_task(tmp_path, _VALID_TOML.format(benchmark="python"), task="beta")
    assert load_task(tmp_path, "beta").task_name == "beta"
    with pytest.raises(AmbiguousTaskError, match="alpha"):
        load_task(tmp_path)


def test_load_task_keeps_bare_executable_and_checks_relative_ones(
    tmp_path: Path,
) -> None:
    bundle = load_task(_bundle(tmp_path))
    assert bundle.resolved_benchmark_command == ("python",)
    assert bundle.resolved_accuracy_command == ("python", "acc.py")

    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "run.sh").write_text("#!/bin/sh\n")
    _bundle(tmp_path, "bin/run.sh")
    assert load_task(tmp_path).resolved_benchmark_command == ("bin/run.sh",)


@pytest.mark.parametrize(
    ("benchmark", "error", "message"),
    [
        ("/usr/bin/env", ValueError, "benchmark.command executable must be relative"),
        ("../outside/run.sh", ValueError, "benchmark.command executable escapes the project"),
        ("bin/missing.sh", FileNotFoundError, "benchmark.command executable does not exist"),
        ("bin/.", ValueError, "benchmark.command executable is not a file"),
    ],
)
def test_load_task_rejects_bad_executables(
    tmp_path: Path, benchmark: str, error: type[Exception], message: str
) -> None:
    (tmp_path / "bin").mkdir()
    _bundle(tmp_path, benchmark)
    with pytest.raises(error, match=message):
        load_task(tmp_path)


def test_load_task_rejects_reference_that_is_a_file(tmp_path: Path) -> None:
    task = write_task(tmp_path, _VALID_TOML.format(benchmark="python"))
    (task / "reference").write_text("not a dir")
    with pytest.raises(ValueError, match="reference path is not a directory"):
        load_task(tmp_path)


@pytest.mark.parametrize(
    ("entrypoint", "setup", "error", "message"),
    [
        ("missing.py", None, FileNotFoundError, "environment.modal.entrypoint does not exist"),
        ("adir", "dir", ValueError, "environment.modal.entrypoint is not a file"),
    ],
)
def test_load_task_validates_modal_entrypoint(
    tmp_path: Path, entrypoint: str, setup: str | None, error: type[Exception], message: str
) -> None:
    _bundle(tmp_path)
    if setup == "dir":
        (tmp_path / entrypoint).mkdir()
    manifest = task_path(tmp_path) / "vibesys.input.toml"
    manifest.write_text(
        manifest.read_text() + f'[environment.modal]\nentrypoint = "{entrypoint}"\n'
    )
    with pytest.raises(error, match=message):
        load_task(tmp_path)


@pytest.mark.parametrize(
    "profile",
    [
        {
            "command": [
                "python",
                "benchmark/profile.py",
                "--base-url",
                "http://localhost:VIBESYS_DYNAMIC_PORT",
            ],
            "timeout_seconds": 120,
        },
        {
            "entrypoint": "request-factory-adapter",
            "args": ["benchmark/profile.py"],
            "timeout_seconds": 90,
        },
    ],
)
def test_profile_workload_is_a_strict_trusted_command_roundtrip(
    tmp_path: Path, profile: dict[str, Any]
) -> None:
    manifest = InputManifest.model_validate(
        {
            "version": 1,
            "agent": {"domain": "generic"},
            "accuracy": {"command": ["python", "accuracy.py"]},
            "benchmark": {"entrypoint": "request-factory-adapter", "args": ["benchmark/run.py"]},
            "evaluator": {"name": "vibesys-evaluator-request-factory", "version": "0.1.0"},
            "profile": profile,
        }
    )
    assert InputManifest.model_validate(tomllib.loads(render_input_manifest(manifest))) == manifest
    write_task(tmp_path, render_input_manifest(manifest), objective="Optimize throughput.\n")
    bundle = load_task(tmp_path)
    assert bundle.profile_command is not None
    if "command" in profile:
        assert bundle.profile_command == tuple(profile["command"])
    else:
        assert bundle.profile_command[-len(profile["args"]) :] == tuple(profile["args"])
    with pytest.raises(ValidationError, match=r"profile\.untrusted_workload"):
        InputManifest.model_validate(
            {**manifest.model_dump(), "profile": {**profile, "untrusted_workload": "ignored"}}
        )
