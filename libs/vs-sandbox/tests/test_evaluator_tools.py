"""Tests for immutable evaluator tool installation."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

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

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import Never


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
    assert json.loads(arguments[-2])["tools"] == {"example": _spec().model_dump(mode="json")}
    assert arguments[-1] == str(install_parent)


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
        )
    )
    lifecycle = SandboxLifecycle(
        [EvaluatorToolLifecycleHooks({"example": _spec()}, tmp_path / "tools")]
    )

    with pytest.raises(
        EvaluatorToolError,
        match=rf"sandbox installation failed \(exit {exit_code}\): permission denied",
    ) as error:
        lifecycle.hooks[0].before_ready(BeforeReadyContext(sandbox=sandbox))

    assert "root sandbox failure" in str(error.value)
    assert len(str(error.value)) < 2100


def test_target_install_command_quotes_manifest_and_root(tmp_path: Path) -> None:
    install_parent = tmp_path / "tools with 'quotes'; $(not-a-command)"

    command = evaluator_tools_install_command({"example": _spec()}, install_parent)
    arguments = shlex.split(command)

    assert arguments[0:2] == ["python3", "-c"]
    assert json.loads(arguments[-2]) == {
        "schema_version": 1,
        "tools": {"example": _spec().model_dump(mode="json")},
    }
    assert arguments[-1] == str(install_parent)


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
