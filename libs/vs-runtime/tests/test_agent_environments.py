"""Contracts for runtime-owned scoped agent environments."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from vs_agent.api import NULL_SKILL_SELECTION
from vs_runtime.api.infrastructure import (
    AgentPaths,
    RunEnvironmentRequest,
    RunEnvironmentView,
    SharedAgentEnvironmentConflictError,
    open_agent_execution_environment,
)
from vs_sandbox.api import (
    EnvironmentBindMount,
    HostResource,
    HostResourceAccess,
    ProjectPathPolicy,
)
from vs_sandbox.api.testing import FakeComputeBackend, FakeSandbox

if TYPE_CHECKING:
    from pathlib import Path

    from vs_sandbox.api import Sandbox


@dataclass
class _Session:
    sandbox: Sandbox
    view: RunEnvironmentView
    close_count: int = 0

    def __enter__(self) -> _Session:
        return self

    def __exit__(
        self,
        exc_type: object,
        exc: object,
        tb: object,
    ) -> None:
        del exc_type, exc, tb
        self.close()

    def close(self) -> None:
        self.close_count += 1


def _request(tmp_path: Path) -> RunEnvironmentRequest:
    return RunEnvironmentRequest(
        log_dir=tmp_path / "logs",
        workspace=tmp_path / "workspace",
        ref_dir=None,
        backend=FakeComputeBackend(),
        agent_backend="stub",
        cli_provider="claude",
        run_id="run-1",
        framework_root=tmp_path,
        environment_bind_mounts=(EnvironmentBindMount(tmp_path / "base", "/base", read_only=True),),
        project_path_policy=ProjectPathPolicy(),
    )


def _session(*, sandboxed: bool) -> _Session:
    return _Session(
        FakeSandbox(),
        RunEnvironmentView(
            paths=AgentPaths(),
            cli_sandboxed=sandboxed,
            share_agent_session=False,
        ),
    )


def test_borrowed_environment_never_closes_shared_session(tmp_path: Path) -> None:
    request = _request(tmp_path)
    shared = _session(sandboxed=True)

    environment = open_agent_execution_environment(
        request,
        shared,
        share_session=True,
        skill_source_dirs=(),
        skill_selection=NULL_SKILL_SELECTION,
        host_resources=(),
        open_session=lambda _request: _session(sandboxed=True),
    )
    environment.close()
    environment.close()

    assert environment.session is shared
    assert environment.backends == {"chat": shared.sandbox}
    assert environment.use_docker
    assert shared.close_count == 0


def test_shared_environment_rejects_agent_overrides(tmp_path: Path) -> None:
    request = _request(tmp_path)
    shared = _session(sandboxed=False)

    with pytest.raises(SharedAgentEnvironmentConflictError):
        open_agent_execution_environment(
            request,
            shared,
            share_session=True,
            skill_source_dirs=(),
            skill_selection=NULL_SKILL_SELECTION,
            host_resources=(),
            open_session=lambda _request: _session(sandboxed=True),
            agent_backend="cli",
        )
    with pytest.raises(SharedAgentEnvironmentConflictError):
        open_agent_execution_environment(
            request,
            shared,
            share_session=True,
            skill_source_dirs=(),
            skill_selection=NULL_SKILL_SELECTION,
            host_resources=(),
            open_session=lambda _request: _session(sandboxed=True),
            cli_provider="codex",
        )
    with pytest.raises(SharedAgentEnvironmentConflictError):
        open_agent_execution_environment(
            request,
            shared,
            share_session=True,
            skill_source_dirs=(),
            skill_selection=NULL_SKILL_SELECTION,
            host_resources=(),
            open_session=lambda _request: _session(sandboxed=True),
            mounts=(HostResource(tmp_path / "input"),),
        )


def test_owned_environment_translates_mounts_and_closes_once(tmp_path: Path) -> None:
    request = _request(tmp_path)
    opened = _session(sandboxed=True)
    observed: list[RunEnvironmentRequest] = []

    def open_session(candidate: RunEnvironmentRequest) -> _Session:
        observed.append(candidate)
        return opened

    readonly = HostResource(tmp_path / "read", agent_path="/mnt/read")
    writable = HostResource(
        tmp_path / "write",
        access=HostResourceAccess.READ_WRITE,
    )
    environment = open_agent_execution_environment(
        request,
        _session(sandboxed=False),
        share_session=False,
        skill_source_dirs=(),
        skill_selection=NULL_SKILL_SELECTION,
        host_resources=(),
        mounts=(readonly, writable),
        agent_backend="cli",
        cli_provider="codex",
        open_session=open_session,
    )
    environment.close()
    environment.close()

    assert len(observed) == 1
    candidate = observed[0]
    assert candidate.agent_backend == "cli"
    assert candidate.cli_provider == "codex"
    assert candidate.environment_bind_mounts == (
        *request.environment_bind_mounts,
        EnvironmentBindMount(readonly.path, "/mnt/read", read_only=True),
        EnvironmentBindMount(writable.path, str(writable.path), read_only=False),
    )
    assert environment.backends == {"chat": opened.sandbox}
    assert opened.close_count == 1
