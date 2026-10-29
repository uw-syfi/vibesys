"""Tests for immutable evaluator tool installation."""

from __future__ import annotations

import json
import os
import shlex
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from vs_sandbox.api import BeforeReadyContext, CommandResult, SandboxLifecycle
from vs_sandbox.api.evaluator_tools import (
    CargoGitToolSpec,
    EvaluatorToolError,
    EvaluatorToolLifecycleHooks,
    ToolResult,
    cargo_install_argv,
    evaluator_tools_install_command,
    prepare_evaluator_tools,
    tool_install_root,
    tool_result,
    tool_spec_digest,
    tool_timed_out,
    tool_token,
)
from vs_sandbox.api.testing import FakeCommandRunner
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from typing import Never
from vs_sim.api import DISCARD


def _spec() -> CargoGitToolSpec:
    return CargoGitToolSpec(
        kind="cargo-git",
        git="https://example.com/tools",
        rev="1" * 40,
        package="example-package",
        bins=("runner", "tracegen"),
    )


def test_cargo_install_argv_uses_locked_revision_and_positional_package(tmp_path: Path) -> None:
    arguments = cargo_install_argv(_spec(), tmp_path / "install")

    assert arguments == (
        "cargo",
        "install",
        "--git",
        "https://example.com/tools",
        "--rev",
        "1" * 40,
        "--locked",
        "--root",
        str(tmp_path / "install"),
        "--bin",
        "runner",
        "--bin",
        "tracegen",
        "example-package",
    )
    assert "--package" not in arguments


def test_prepare_tools_publishes_complete_install_and_reuses_it(tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []

    def install(arguments: Sequence[str]) -> ToolResult:
        normalized = tuple(arguments)
        calls.append(normalized)
        root = Path(normalized[normalized.index("--root") + 1])
        for binary in ("runner", "tracegen"):
            path = root / "bin" / binary
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("binary", encoding="utf-8")
            path.chmod(0o755)
        return tool_result(normalized)

    install_parent = tmp_path / "tools"
    first = prepare_evaluator_tools({"example": _spec()}, install_parent, command_runner=install)
    second = prepare_evaluator_tools({"example": _spec()}, install_parent, command_runner=install)

    root = tool_install_root(install_parent, "example", _spec())
    expected = root / "bin" / "runner"
    assert first[tool_token("example", "runner")] == str(expected)
    assert second == first
    assert len(calls) == 1
    assert expected.is_file()
    receipt = json.loads((root / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["spec"] == _spec().model_dump(mode="json")
    assert set(receipt["binaries"]) == {"runner", "tracegen"}
    assert root.name == tool_spec_digest(_spec())
    assert not list(root.parent.glob(f".{root.name}-*"))


def test_lifecycle_hooks_snapshot_tools_and_execute_target_command(tmp_path: Path) -> None:
    tools = {"example": _spec()}
    install_parent = tmp_path / "tools"
    hooks = EvaluatorToolLifecycleHooks(tools, install_parent)
    tools.clear()
    sandbox = FakeCommandRunner()

    lifecycle = SandboxLifecycle([hooks])
    lifecycle.before_ready(sandbox)
    lifecycle.before_ready(sandbox)

    assert len(sandbox.calls) == 2
    command = sandbox.calls[0].command
    assert sandbox.calls[0].timeout == 660
    assert sandbox.calls[1].command == command
    arguments = shlex.split(command)
    assert arguments[0:2] == ["python3", "-c"]
    assert json.loads(arguments[-3])["tools"] == {"example": _spec().model_dump(mode="json")}
    assert arguments[-2] == str(install_parent)
    assert arguments[-1] == ""


@pytest.mark.parametrize("exit_code", [None, 17])
def test_lifecycle_hooks_reject_target_install_failure(
    tmp_path: Path,
    exit_code: int | None,
) -> None:
    sandbox = FakeCommandRunner(
        default_result=CommandResult(
            exit_code=exit_code,
            output=(
                "permission denied\n"
                + ("unhelpful install progress\n" * 1000)
                + "root sandbox failure"
            ),
            truncated=True,
            diagnostic_output="permission denied\nroot sandbox failure",
        )
    )
    lifecycle = SandboxLifecycle(
        [EvaluatorToolLifecycleHooks({"example": _spec()}, tmp_path / "tools")]
    )

    with pytest.raises(
        EvaluatorToolError,
        match=rf"sandbox installation failed \(exit {exit_code}\): permission denied",
    ) as error:
        lifecycle.hooks[0].before_ready(BeforeReadyContext(sandbox=sandbox, output=DISCARD))

    assert "root sandbox failure" in str(error.value)
    assert len(str(error.value)) < 2100


def test_target_install_command_quotes_manifest_and_root(tmp_path: Path) -> None:
    install_parent = tmp_path / "tools with 'quotes'; $(not-a-command)"

    command = evaluator_tools_install_command({"example": _spec()}, install_parent)
    arguments = shlex.split(command)

    assert arguments[0:2] == ["python3", "-c"]
    assert json.loads(arguments[-3]) == {
        "schema_version": 1,
        "tools": {"example": _spec().model_dump(mode="json")},
    }
    assert arguments[-2] == str(install_parent)


def test_target_install_command_rejects_unsafe_tool_name(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="invalid evaluator tool name"):
        evaluator_tools_install_command({"../escape": _spec()}, tmp_path / "tools")


def test_prepare_tools_rejects_binary_changed_after_receipt(tmp_path: Path) -> None:
    def install(arguments: Sequence[str]) -> ToolResult:
        normalized = tuple(arguments)
        root = Path(normalized[normalized.index("--root") + 1])
        for binary in ("runner", "tracegen"):
            path = root / "bin" / binary
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("binary", encoding="utf-8")
            path.chmod(0o755)
        return tool_result(normalized)

    install_parent = tmp_path / "tools"
    prepare_evaluator_tools({"example": _spec()}, install_parent, command_runner=install)
    root = tool_install_root(install_parent, "example", _spec())
    (root / "bin" / "runner").write_text("tampered", encoding="utf-8")

    with pytest.raises(EvaluatorToolError, match="failed receipt verification"):
        prepare_evaluator_tools({"example": _spec()}, install_parent, command_runner=install)


def test_prepare_tools_accepts_verified_concurrent_winner(tmp_path: Path) -> None:
    install_parent = tmp_path / "tools"

    def write_binaries(arguments: Sequence[str]) -> ToolResult:
        normalized = tuple(arguments)
        root = Path(normalized[normalized.index("--root") + 1])
        for binary in ("runner", "tracegen"):
            path = root / "bin" / binary
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("binary", encoding="utf-8")
            path.chmod(0o755)
        return tool_result(normalized)

    def publish_winner(arguments: Sequence[str]) -> ToolResult:
        result = write_binaries(arguments)
        prepare_evaluator_tools(
            {"example": _spec()},
            install_parent,
            command_runner=write_binaries,
        )
        return result

    replacements = prepare_evaluator_tools(
        {"example": _spec()},
        install_parent,
        command_runner=publish_winner,
    )

    root = tool_install_root(install_parent, "example", _spec())
    assert replacements[tool_token("example", "runner")] == str(root / "bin" / "runner")
    assert json.loads((root / "receipt.json").read_text(encoding="utf-8"))["spec"] == (
        _spec().model_dump(mode="json")
    )


def test_prepare_tools_translates_missing_cargo(tmp_path: Path) -> None:
    def missing(_arguments: Sequence[str]) -> Never:
        raise FileNotFoundError("cargo")

    with pytest.raises(EvaluatorToolError, match="cargo was not found"):
        prepare_evaluator_tools({"example": _spec()}, tmp_path, command_runner=missing)


def test_prepare_tools_reports_cargo_failure_and_cleans_staging(tmp_path: Path) -> None:
    def fail(arguments: Sequence[str]) -> ToolResult:
        return tool_result(
            arguments,
            7,
            stderr="unhelpful compiler progress\n" * 1000 + "dependency resolution failed",
        )

    with pytest.raises(EvaluatorToolError, match="dependency resolution failed"):
        prepare_evaluator_tools({"example": _spec()}, tmp_path, command_runner=fail)

    cache = tmp_path / "example"
    assert not list(cache.glob(".*-*"))


def test_prepare_tools_reports_timeout_and_cleans_staging(tmp_path: Path) -> None:
    def timeout(arguments: Sequence[str]) -> Never:
        raise tool_timed_out(arguments, 600)

    with pytest.raises(EvaluatorToolError, match="cargo install timed out"):
        prepare_evaluator_tools({"example": _spec()}, tmp_path, command_runner=timeout)

    cache = tmp_path / "example"
    assert not list(cache.glob(".*-*"))


def _installer(search_path: str = "") -> dict[str, Any]:
    """Load the target-side installer program, as the target's ``python3 -c`` would, without running it."""
    program = shlex.split(
        evaluator_tools_install_command({"example": _spec()}, "x", search_path=search_path)
    )[2]
    namespace: dict[str, Any] = {"__name__": "installer"}
    exec(compile(program, "installer", "exec"), namespace)  # noqa: S102  # lint-waiver: LW-930060 [S102]; the installer ships as program text for a target's python3, and loading it in process is the only way to test its logic without starting a process.
    return namespace


@contextmanager
def _installer_run(root: Path, search_path: str) -> Iterator[dict[str, Any]]:
    """Set the arguments the installer reads (its input), run no process, restore them after."""
    previous = sys.argv
    manifest = json.dumps(
        {"schema_version": 1, "tools": {"example": _spec().model_dump(mode="json")}}
    )
    sys.argv = ["installer", manifest, str(root), search_path]
    try:
        yield _installer(search_path)
    finally:
        sys.argv = previous


@contextmanager
def _process_path(value: str) -> Iterator[None]:
    """Set this process's ``PATH`` (an input of the installer), and restore it after."""
    previous = os.environ["PATH"]
    os.environ["PATH"] = value
    try:
        yield
    finally:
        os.environ["PATH"] = previous


_DIRECTORY = st.from_regex(r"[a-z]{1,6}", fullmatch=True)


@given(
    login=st.lists(_DIRECTORY, max_size=4, unique=True),
    own=st.lists(_DIRECTORY, max_size=4, unique=True),
    with_cargo=st.sets(_DIRECTORY, max_size=3),
)
@example(login=["cargobin"], own=["usr"], with_cargo={"cargobin"})
@settings(max_examples=examples(pr=40, nightly=300), deadline=None)
def test_cargo_is_found_in_the_login_path_or_the_process_path_and_never_elsewhere(
    tmp_path_factory: pytest.TempPathFactory,
    login: list[str],
    own: list[str],
    with_cargo: set[str],
) -> None:
    """A login-shell directory is searched first; a directory in neither is never searched."""
    root = tmp_path_factory.mktemp("path")
    for name in {*login, *own, "elsewhere"}:
        (root / name).mkdir(exist_ok=True)
    for name in with_cargo | {"elsewhere"}:
        (root / name).mkdir(exist_ok=True)
        binary = root / name / "cargo"
        binary.write_text("", encoding="utf-8")
        binary.chmod(0o755)
    namespace = _installer()

    with _process_path(os.pathsep.join(str(root / name) for name in own)):
        found = namespace["find_cargo"](
            namespace["search_directories"](os.pathsep.join(str(root / name) for name in login))
        )

    expected = next((name for name in [*login, *own] if name in with_cargo), None)
    assert found == (None if expected is None else str(root / expected / "cargo"))


def test_a_missing_cargo_names_the_tool_the_path_searched_and_the_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "login").mkdir()
    (tmp_path / "own").mkdir()
    monkeypatch.setenv("PATH", str(tmp_path / "own"))

    with _installer_run(tmp_path / "tools", str(tmp_path / "login")) as namespace:
        install_error = namespace["InstallError"]
        with pytest.raises(install_error) as error:
            namespace["main"]()

    message = str(error.value)
    assert "'example'" in message
    assert f"{tmp_path / 'login'}{os.pathsep}{tmp_path / 'own'}" in message
    assert "login shell" in message
    assert ".cargo/env" in message
    assert list((tmp_path / "tools").glob("**/.*")) == []


def test_a_verified_cached_install_needs_no_cargo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cache hit is verified and used without looking for cargo at all."""
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))

    def install(arguments: Sequence[str]) -> ToolResult:
        root = Path(arguments[arguments.index("--root") + 1]) / "bin"
        root.mkdir(parents=True)
        for binary in _spec().bins:
            (root / binary).write_text(f"#!/bin/sh\n# {binary}\n", encoding="utf-8")
            (root / binary).chmod(0o755)
        return tool_result(arguments)

    prepare_evaluator_tools({"example": _spec()}, tmp_path / "tools", command_runner=install)

    with _installer_run(tmp_path / "tools", "") as namespace:
        namespace["main"]()


def test_the_search_path_reaches_the_installer_as_its_third_argument(tmp_path: Path) -> None:
    sandbox = FakeCommandRunner()

    SandboxLifecycle(
        [
            EvaluatorToolLifecycleHooks(
                {"example": _spec()}, tmp_path / "tools", search_path="/login/bin:/x"
            )
        ]
    ).before_ready(sandbox)

    assert shlex.split(sandbox.calls[0].command)[-1] == "/login/bin:/x"
