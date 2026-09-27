"""Lifecycle contracts for lower-owned run environment resources."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from vs_runtime.api.infrastructure import (
    AgentPaths,
    RunEnvironmentRequest,
    RunEnvironmentView,
    open_run_environment_resources,
)
from vs_sandbox.api import ProjectPathPolicy
from vs_sandbox.api.testing import FakeComputeBackend, FakeSandbox

if TYPE_CHECKING:
    from pathlib import Path

    from vs_sandbox.api import Sandbox


@dataclass
class _Monitor:
    events: list[str]
    start_error: BaseException | None = None

    def start(self) -> None:
        self.events.append("monitor:start")
        if self.start_error is not None:
            raise self.start_error

    def stop(self) -> None:
        self.events.append("monitor:stop")


class _Backend(FakeComputeBackend):
    def __init__(self, monitor: _Monitor) -> None:
        super().__init__()
        self.monitor = monitor
        self._monitor = monitor
        self.reselection_count = 0

    def make_monitor(self, log_dir: Path) -> _Monitor:
        del log_dir
        return self.monitor

    def reselect_device(self) -> None:
        self.reselection_count += 1


@dataclass
class _Session:
    sandbox: Sandbox
    view: RunEnvironmentView
    events: list[str]
    close_error: BaseException | None = None

    def __enter__(self) -> _Session:
        self.events.append("session:enter")
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb
        self.close()

    def close(self) -> None:
        self.events.append("session:close")
        if self.close_error is not None:
            raise self.close_error


def _request(tmp_path: Path, backend: _Backend) -> RunEnvironmentRequest:
    return RunEnvironmentRequest(
        log_dir=tmp_path / "logs",
        workspace=tmp_path / "workspace",
        ref_dir=None,
        backend=backend,
        agent_backend="stub",
        cli_provider="claude",
        run_id="run-1",
        framework_root=tmp_path,
        project_path_policy=ProjectPathPolicy(),
    )


def _session(events: list[str]) -> _Session:
    return _Session(
        FakeSandbox(),
        RunEnvironmentView(paths=AgentPaths()),
        events,
    )


def test_root_resources_close_device_before_session_and_reselect(tmp_path: Path) -> None:
    events: list[str] = []
    backend = _Backend(_Monitor(events))
    session = _session(events)

    resources = open_run_environment_resources(
        _request(tmp_path, backend), lambda _request: session
    )
    resources.reselect_device()
    resources.close()
    resources.close()

    assert backend.reselection_count == 1
    assert events == ["session:enter", "monitor:start", "monitor:stop", "session:close"]


def test_workspace_resources_borrow_device_without_closing_it(tmp_path: Path) -> None:
    events: list[str] = []
    backend = _Backend(_Monitor(events))
    root = open_run_environment_resources(
        _request(tmp_path, backend),
        lambda _request: _session(events),
    )
    workspace_request = _request(tmp_path, backend)
    workspace = root.open_workspace(workspace_request)

    workspace.close()
    workspace.close()
    assert events == ["session:enter", "monitor:start", "session:enter", "session:close"]
    assert workspace.request is workspace_request
    assert workspace.device is root.device

    root.close()
    assert events[-2:] == ["monitor:stop", "session:close"]


def test_monitor_start_failure_preserves_error_and_closes_session(tmp_path: Path) -> None:
    events: list[str] = []
    failure = RuntimeError("monitor failed")
    backend = _Backend(_Monitor(events, start_error=failure))
    session = _session(events)
    session.close_error = RuntimeError("session cleanup failed")

    with pytest.raises(RuntimeError, match="monitor failed") as raised:
        open_run_environment_resources(
            _request(tmp_path, backend),
            lambda _request: session,
        )

    assert raised.value is failure
    assert any("session cleanup failed" in note for note in (failure.__notes__ or ()))
    assert events == ["session:enter", "monitor:start", "monitor:stop", "session:close"]
