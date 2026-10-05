"""The headless command exits nonzero for every run that did not succeed."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, cast

import pytest
from tests.entrypoints.test_headless import _write_input_project

from entrypoints import cli
from vibesys.api import (
    ConfigurationError,
    RunFailure,
    RunFailureKind,
    RunRequest,
    RunResult,
    RunSession,
    RunStatus,
    RunStopped,
)
from vibesys.api.testing import FakeRunHandle

if TYPE_CHECKING:
    from pathlib import Path


class _Session:
    """A session whose run ends with a scripted result or raises a scripted error."""

    def __init__(self, ending: RunResult | BaseException) -> None:
        self._ending = ending

    def start(self) -> None:
        pass

    def close(self) -> None:
        pass

    def stop(self) -> None:
        pass

    async def await_result(self) -> RunResult | None:
        if isinstance(self._ending, BaseException):
            raise self._ending
        return self._ending


class _Runs:
    def __init__(self, ending: RunResult | BaseException) -> None:
        self._ending = ending

    def start(self, request: RunRequest) -> FakeRunHandle:  # type: ignore[type-arg]
        del request
        handle = FakeRunHandle("exit-status")
        handle.bind(cast("RunSession", _Session(self._ending)))
        handle.start()
        return handle

    def resume(self, request: RunRequest) -> FakeRunHandle:  # type: ignore[type-arg]
        return self.start(request)

    def attach(self, run_id: str) -> FakeRunHandle:  # type: ignore[type-arg]
        raise KeyError(run_id)

    def list_active(self) -> tuple[FakeRunHandle, ...]:  # type: ignore[type-arg]
        return ()


def _result(
    *,
    succeeded: bool,
    status: Literal[RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.STOPPED],
    failure: RunFailure | None = None,
) -> RunResult:
    return RunResult(
        run_id="exit-status",
        loop="agent",
        succeeded=succeeded,
        status=status,
        failure=failure,
    )


def _failure(kind: RunFailureKind, kept: int) -> RunFailure:
    return RunFailure(
        kind=kind,
        reason="why",
        workstreams_started=1,
        workstream_budget=1,
        candidates_kept=kept,
    )


ENDINGS: dict[str, RunResult | BaseException] = {
    **{
        f"{kind.value}-kept-{kept}": _result(
            succeeded=False, status=RunStatus.FAILED, failure=_failure(kind, kept)
        )
        for kind in RunFailureKind
        for kept in (0, 2)
    },
    "failed-without-reason": _result(succeeded=False, status=RunStatus.FAILED),
    "stopped": _result(succeeded=False, status=RunStatus.STOPPED),
    "halted": RuntimeError("the runtime halted"),
    "stopped-by-operator": RunStopped(),
}


@pytest.mark.parametrize("name", list(ENDINGS))
def test_every_unsuccessful_ending_exits_nonzero(name: str, tmp_path: Path) -> None:
    project = _write_input_project(tmp_path)
    argv = ["--outer-loop", "agent", "--input", str(project)]

    with pytest.raises((SystemExit, RuntimeError, RunStopped, ConfigurationError)) as ended:
        cli.dispatch(argv, _Runs(ENDINGS[name]))

    if isinstance(ended.value, SystemExit):
        assert ended.value.code not in (0, None)
    else:
        assert not isinstance(ended.value, ConfigurationError), ended.value


def test_a_successful_run_returns_without_exiting(tmp_path: Path) -> None:
    project = _write_input_project(tmp_path)
    ok = _result(succeeded=True, status=RunStatus.COMPLETED)

    cli.dispatch(["--outer-loop", "agent", "--input", str(project)], _Runs(ok))
