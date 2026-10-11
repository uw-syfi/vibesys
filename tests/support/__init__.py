"""Shared factories for test fixtures."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from os import PathLike
from typing import TYPE_CHECKING, Literal, cast, overload

if TYPE_CHECKING:
    from vibesys.hypothesis import OrchestratorPlan


# lint-waiver: LW-041041 [PLC0415]; environment plugins must load before application code.
# > An eager import precedes isolation; importlib adds dynamic attribute lookup
# > and a cast, while a separate lazy factory module adds indirection to one use.
def make_orchestrator_plan(*, criteria: str, **fields: object) -> OrchestratorPlan:
    """Build a validated plan fixture with the required criterion."""
    # Environment plugins import this package before isolating runtime paths.
    # Load application code only when this fixture factory is exercised.
    from vibesys.hypothesis import OrchestratorPlan  # noqa: PLC0415

    return OrchestratorPlan.model_validate({**fields, "pass_criteria": criteria})


_Command = Sequence[str | PathLike[str]]


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
