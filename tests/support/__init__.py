"""Shared factories for test fixtures."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from os import PathLike
from typing import Literal, Protocol, cast, overload
from unittest.mock import patch

from vibesys.search.hypothesis import OrchestratorPlan


def make_orchestrator_plan(*, criteria: str, **fields: object) -> OrchestratorPlan:
    """Build a validated plan fixture with the required criterion."""
    return OrchestratorPlan.model_validate({**fields, "pass_criteria": criteria})


_Command = Sequence[str | PathLike[str]]


class _StartableSandbox(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...


def capture_docker_start_argv(sandbox: _StartableSandbox) -> list[str]:
    """Return the Docker argv issued by a sandbox's public start operation."""
    commands: list[list[str]] = []

    def fake_run(
        argv: Sequence[str | PathLike[str]], **_options: object
    ) -> subprocess.CompletedProcess[str]:
        command = [str(argument) for argument in argv]
        commands.append(command)
        container_id = "test-container-id" if command[1:2] == ["run"] else ""
        return subprocess.CompletedProcess(command, 0, stdout=container_id, stderr="")

    with patch("vs_sandbox.docker_sandbox.subprocess.run", side_effect=fake_run):
        try:
            sandbox.start()
        finally:
            sandbox.stop()
    return next(command for command in commands if command[1:2] == ["run"])


@overload
def run_test_command(
    argv: _Command,
    *,
    text: Literal[True],
    check: bool = False,
    cwd: str | PathLike[str] | None = None,
    capture_output: bool = False,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
    stdin: int | None = None,
) -> subprocess.CompletedProcess[str]: ...
@overload
def run_test_command(
    argv: _Command,
    *,
    text: Literal[False] = False,
    check: bool = False,
    cwd: str | PathLike[str] | None = None,
    capture_output: bool = False,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
    stdin: int | None = None,
) -> subprocess.CompletedProcess[bytes]: ...
def run_test_command(  # noqa: PLR0913  # lint-waiver: LW-006007; mirrors explicit subprocess options used by test callers.
    argv: _Command,
    *,
    text: bool = False,
    check: bool = False,
    cwd: str | PathLike[str] | None = None,
    capture_output: bool = False,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
    stdin: int | None = None,
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    """Run a test-owned command with explicit status handling and options."""
    result = subprocess.run(  # noqa: S603  # lint-waiver: LW-006006; test-owned argv and cwd values exercise external command behavior.
        argv,
        check=check,
        cwd=cwd,
        capture_output=capture_output,
        text=text,
        env=env,
        timeout=timeout,
        stdin=stdin,
    )
    return cast("subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]", result)
