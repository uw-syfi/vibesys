"""Public auxiliary-agent boundary and its product composition."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError
from tests.support.run_execution import run_execution_record

from vibesys.api import AuxiliaryAgentLaunch, AuxiliaryReadableInput, RunReady
from vibesys.api.session import (
    _LocalRunSession,  # test-isolation: compose the real public session over deterministic resource fakes.
)
from vibesys.config import Config
from vibesys.constants import ComputeBackend
from vibesys.orchestration.contracts import OrchestrationRegistry
from vibesys.run.integration import RunResources
from vs_project.api import OrchestrationDescriptor, Project, RunEnvironmentRecord
from vs_runtime.api import OrchestrationPlugin, RunHost
from vs_runtime.api import RunStatus as PluginRunStatus
from vs_runtime.api.infrastructure import LocalEnvironmentFacts, RunEnvironmentPresentation
from vs_sandbox.api import EnvironmentBindMount, ProjectPathPolicy

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.api.contracts import EventSink
    from vibesys.orchestration.request import RunRequest


@dataclass(frozen=True)
class _EnvironmentRequest:
    environment_bind_mounts: tuple[EnvironmentBindMount, ...] = ()
    agent_backend: str | None = "stub"
    cli_provider: str | None = "codex"


class _EnvironmentSession:
    def __init__(self) -> None:
        self.sandbox = SimpleNamespace(agent_path=_identity_path)
        self.view = SimpleNamespace(cli_sandboxed=False, isolated=False)
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _identity_path(host: object) -> str:
    return str(host)


class _Environment:
    """Deterministic fake for the private run-environment resource."""

    def __init__(self) -> None:
        self.requests: list[_EnvironmentRequest] = []
        self.sessions: list[_EnvironmentSession] = []

    def prepare(self, request: _EnvironmentRequest) -> _PreparedEnvironment:
        return _PreparedEnvironment(self, request)

    def _open(self, request: _EnvironmentRequest) -> _EnvironmentSession:
        self.requests.append(request)
        session = _EnvironmentSession()
        self.sessions.append(session)
        return session


class _PreparedEnvironment:
    """Prepared local environment with product presentation supplied explicitly."""

    presentation_facts = LocalEnvironmentFacts()

    def __init__(self, environment: _Environment, request: _EnvironmentRequest) -> None:
        self._environment = environment
        self._request = request

    def open(self, presentation: RunEnvironmentPresentation) -> _EnvironmentSession:
        assert presentation == RunEnvironmentPresentation(prompt_notes="")
        return self._environment._open(self._request)  # noqa: SLF001  # lint-waiver: LW-948034 [SLF001]; this prepared fake completes its paired environment's open phase.


class _StubOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def _run_stub(host: RunHost, options: BaseModel) -> PluginRunStatus:
    del host, options
    return PluginRunStatus.SUCCEEDED


def _session() -> _LocalRunSession:
    registry = OrchestrationRegistry()
    registry.register_plugin(
        OrchestrationPlugin(id="stub", agents=(), options=_StubOptions, orchestrate=_run_stub)
    )
    request = SimpleNamespace(
        orchestration=OrchestrationDescriptor(id="stub", config_version=1, options={})
    )
    return _LocalRunSession(
        cast("RunRequest", request),
        sink=cast("EventSink", lambda _event: None),
        registry=registry,
        agent_client_factory=None,
        backend_factory=None,
    )


def _resources(tmp_path: Path, environment: _Environment) -> RunResources:
    workspace = tmp_path / "workspace"
    logs = tmp_path / "logs"
    project_root = tmp_path / "project"
    workspace.mkdir()
    logs.mkdir()
    project_root.mkdir()
    (project_root / "OBJECTIVE.md").write_text("Test the auxiliary API.\n", encoding="utf-8")
    project = Project.open(project_root)
    project.state.create_project("test")
    project.state.create_run(
        project.state.new_run_manifest(
            "test",
            run_id="run-1",
            branch="vibesys/run-1",
            vibesys_version="test",
            run_environment=RunEnvironmentRecord(name="local"),
            execution=run_execution_record(),
            orchestration=OrchestrationDescriptor(id="stub", config_version=1, options={}),
            trusted_input_baseline="0" * 40,
        )
    )
    return RunResources(
        project=project,
        run_id="run-1",
        workspace=workspace,
        log_dir=logs,
        agent_backend="stub",
        driver="agentshim",
        provider="codex",
        model="gpt-test",
        role_models=("gpt-worker",),
        config=Config.model_validate(
            {
                "model": {"name": "gpt-test"},
                "agent": {"backend": "stub", "cli_provider": "codex"},
            }
        ),
        compute_backend=ComputeBackend.CPU,
        skill_source_dirs=(),
        environment=cast("Any", environment),
        environment_request=cast("Any", _EnvironmentRequest()),
        run_environment_sandboxed=False,
        project_path_policy=ProjectPathPolicy(),
        host_resources=(),
    )


def _launch(readable_path: Path) -> AuxiliaryAgentLaunch:
    return AuxiliaryAgentLaunch(
        role="chat",
        member_id="thread-1",
        driver="agentshim",
        provider="codex",
        model="gpt-test",
        system_prompt="Investigate read-only evidence.",
        continuation_prompt="Continue the investigation.",
        readable_inputs=(
            AuxiliaryReadableInput(
                path=readable_path,
                environment_variable="VIBESYS_TEST_EVIDENCE",
                purpose="test evidence",
            ),
        ),
    )


def test_ready_projection_exposes_no_runtime_resources(tmp_path: Path) -> None:
    environment = _Environment()
    session = _session()
    observed: list[RunReady] = []
    session.on_ready(observed.append)

    session._handle_resources(_resources(tmp_path, environment))  # noqa: SLF001  # lint-waiver: LW-948027 [SLF001]; exercise the private composition input and assert only its public projection escapes.

    assert observed == [
        RunReady(
            record=observed[0].record,
            log_directory=tmp_path / "logs",
            frontend_state_directory=observed[0].frontend_state_directory,
            agent_driver="agentshim",
            agent_provider="codex",
            agent_model="gpt-test",
            role_models=("gpt-worker",),
        )
    ]
    assert not hasattr(observed[0], "config")
    assert not hasattr(observed[0], "environment")
    assert not hasattr(observed[0], "host_resources")


def test_managed_agent_hides_environment_and_owns_cleanup(tmp_path: Path) -> None:
    environment = _Environment()
    session = _session()
    session._handle_resources(_resources(tmp_path, environment))  # noqa: SLF001  # lint-waiver: LW-948029 [SLF001]; exercise real product composition over deterministic resources.
    evidence = tmp_path / "evidence"
    evidence.mkdir()

    agent = session.create_auxiliary_agent(_launch(evidence))

    assert (
        agent.turn("What happened?") == "Stub agent inspected the available experiment trajectory."
    )
    assert environment.requests[-1].environment_bind_mounts == (
        EnvironmentBindMount(evidence.resolve(), str(evidence.resolve()), read_only=True),
    )
    assert not hasattr(agent, "sandbox")
    assert not hasattr(agent, "config")
    session.close()
    session.close()
    assert environment.sessions[-1].closed
    with pytest.raises(RuntimeError, match="closed"):
        agent.turn("Another question")


def test_auxiliary_agent_creation_requires_readiness_and_existing_inputs(
    tmp_path: Path,
) -> None:
    session = _session()
    missing = tmp_path / "missing"

    with pytest.raises(RuntimeError, match="not ready"):
        session.create_auxiliary_agent(_launch(missing))

    session._handle_resources(_resources(tmp_path, _Environment()))  # noqa: SLF001  # lint-waiver: LW-948030 [SLF001]; inject the private composition fact needed to test public rejection.
    with pytest.raises(FileNotFoundError, match="does not exist"):
        session.create_auxiliary_agent(_launch(missing))


def test_auxiliary_launch_is_strict_and_rejects_duplicate_paths(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        AuxiliaryAgentLaunch.model_validate(
            {
                **_launch(tmp_path).model_dump(),
                "sandbox": "docker",
            }
        )
    readable_input = AuxiliaryReadableInput(
        path=tmp_path,
        environment_variable="VIBESYS_TEST_EVIDENCE",
        purpose="test evidence",
    )
    with pytest.raises(ValidationError, match="must be unique"):
        AuxiliaryAgentLaunch(
            **_launch(tmp_path).model_dump(exclude={"readable_inputs"}),
            readable_inputs=(readable_input, readable_input),
        )
