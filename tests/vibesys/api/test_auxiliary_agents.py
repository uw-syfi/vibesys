"""Public auxiliary-agent boundary and its product composition."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from pydantic import ValidationError
from tests.support.core_plugins import failing_plugin
from tests.support.docker_environment import fake_docker_environment, host_container_backend
from tests.support.real_git import REAL_PROCESSES
from tests.support.task_project import write_task

from entrypoints.production import production_timing
from launch import LaunchSettings, create_session, default_runs
from launch.agents import BuiltInSessionAgents
from launch.testing import FakeSessionAgents
from vibesys.api import (
    AuxiliaryAgentLaunch,
    AuxiliaryAgents,
    AuxiliaryReadableInput,
    ProfilerKind,
    RunReady,
    RunRequest,
)
from vibesys.api.request import load_task
from vibesys.config import Config
from vibesys.constants import ComputeBackend
from vibesys.plugin_catalog import OrchestrationRegistry
from vibesys.run.integration import RunResources
from vs_agent.api import NULL_AGENT_EVENT_SINK, AgentCapabilities
from vs_agent.api.testing import FakeAgentClient
from vs_project.api import OrchestrationDescriptor, Project, RunEnvironmentRecord
from vs_project.api.testing import run_execution_record
from vs_runtime.api.infrastructure import HostEnvironmentFacts, RunEnvironmentPresentation
from vs_sandbox.api import EnvironmentBindMount, ProjectPathPolicy
from vs_sim.api import OsThreads, ThreadBlockingRunner, UnixNetwork
from vs_sim.api.testing import FakePipedProcessLauncher, ManualClock

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.api import ManagedAgent
    from vibesys.api._session import _LocalRunSession
    from vibesys.api.contracts import EventSink


@dataclass(frozen=True)
class _EnvironmentRequest:
    workspace: Path
    log_dir: Path
    agent_homes_dir: Path | None = None
    environment_bind_mounts: tuple[EnvironmentBindMount, ...] = ()
    agent_backend: str | None = "stub"
    cli_provider: str | None = "codex"
    project_path_policy: ProjectPathPolicy = field(default_factory=ProjectPathPolicy)


class _EnvironmentSession:
    def __init__(self, *, path_error: BaseException | None = None, close_failures: int = 0) -> None:
        def agent_path(host: object) -> str:
            if path_error is not None:
                raise path_error
            return _identity_path(host)

        self.sandbox = SimpleNamespace(agent_path=agent_path)
        self.view = SimpleNamespace(cli_sandboxed=False, isolated=False)
        self.close_count = 0
        self.close_failures = close_failures

    def close(self) -> None:
        self.close_count += 1
        if self.close_count <= self.close_failures:
            message = "docker rm -f was killed"
            raise RuntimeError(message)


def _identity_path(host: object) -> str:
    return str(host)


class _Environment:
    """Deterministic fake for the private run-environment resource."""

    def __init__(self, *, path_error: BaseException | None = None, close_failures: int = 0) -> None:
        self._path_error = path_error
        self._close_failures = close_failures
        self.requests: list[_EnvironmentRequest] = []
        self.sessions: list[_EnvironmentSession] = []

    def prepare(self, request: _EnvironmentRequest) -> _PreparedEnvironment:
        return _PreparedEnvironment(self, request)

    def _open(self, request: _EnvironmentRequest) -> _EnvironmentSession:
        self.requests.append(request)
        session = _EnvironmentSession(
            path_error=self._path_error, close_failures=self._close_failures
        )
        self.sessions.append(session)
        return session


@dataclass(frozen=True)
class _EnvironmentResources:
    """Fake the lower owner while preserving its session-opening behavior."""

    request: _EnvironmentRequest
    session: _EnvironmentSession
    environment: _Environment

    def open_session(self, request: _EnvironmentRequest) -> _EnvironmentSession:
        return self.environment.prepare(request).open(RunEnvironmentPresentation(prompt_notes=""))


class _PreparedEnvironment:
    """Prepared local environment with product presentation supplied explicitly."""

    presentation_facts = HostEnvironmentFacts()

    def __init__(self, environment: _Environment, request: _EnvironmentRequest) -> None:
        self._environment = environment
        self._request = request

    def open(self, presentation: RunEnvironmentPresentation) -> _EnvironmentSession:
        assert presentation == RunEnvironmentPresentation(prompt_notes="")
        return self._environment._open(self._request)  # noqa: SLF001  # lint-waiver: LW-948034 [SLF001]; this prepared fake completes its paired environment's open phase.


def _resumable_client(**_kwargs: object) -> FakeAgentClient:
    return FakeAgentClient(
        capabilities=AgentCapabilities(provider_session_resume=True), threads=OsThreads()
    )


@pytest.fixture(params=[False, True], ids=["built-in", "fake"])
def agent_settings(request: pytest.FixtureRequest) -> LaunchSettings:
    return LaunchSettings(
        blocking=ThreadBlockingRunner(),
        network=UnixNetwork(),
        timing=production_timing(),
        processes=REAL_PROCESSES,
        piped_processes=FakePipedProcessLauncher(),
        agents=FakeSessionAgents(OsThreads()) if request.param else None,
        backend_factory=host_container_backend,
    )


def _request(tmp_path: Path) -> tuple[RunRequest, OrchestrationRegistry]:
    registry = OrchestrationRegistry()
    registry.register_plugin(failing_plugin("stub"))
    input_root = tmp_path / "input"
    input_root.mkdir()
    write_task(
        input_root,
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n[benchmark]\ncommand = ["true"]\n',
        objective="Test auxiliary agents.\n",
    )
    request = RunRequest(
        run_environment=fake_docker_environment(),
        project_root=input_root,
        input_bundle=load_task(input_root),
        orchestration=OrchestrationDescriptor(id="stub", config_version=1, options={}),
        config=Config.model_validate({"model": {"name": "gpt-test"}, "agent": {"backend": "stub"}}),
        exp_name="auxiliary-test",
        profiler_kind=ProfilerKind.NONE,
    )
    return request, registry


def _session(tmp_path: Path, settings: LaunchSettings | None = None) -> _LocalRunSession:
    request, registry = _request(tmp_path)
    return cast(
        "_LocalRunSession",
        create_session(
            request,
            sink=cast("EventSink", lambda _event: None),
            registry=registry,
            settings=(
                LaunchSettings(
                    timing=production_timing(),
                    processes=REAL_PROCESSES,
                    piped_processes=FakePipedProcessLauncher(),
                    blocking=ThreadBlockingRunner(),
                    network=UnixNetwork(),
                )
                if settings is None
                else settings
            ),
        ),
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
    project.state.create_project("test", now=datetime(2026, 1, 1, tzinfo=UTC))
    project.state.create_run(
        project.state.new_run_manifest(
            "test",
            run_id="run-1",
            branch="vibesys/run-1",
            vibesys_version="test",
            run_environment=RunEnvironmentRecord(name="docker"),
            execution=run_execution_record(),
            orchestration=OrchestrationDescriptor(id="stub", config_version=1, options={}),
            trusted_input_baseline="0" * 40,
            now=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )
    shared_session = _EnvironmentSession()
    environment_request = _EnvironmentRequest(workspace=workspace, log_dir=logs)
    return RunResources(
        project_resources=cast(
            "Any",
            SimpleNamespace(
                project=project,
                state=SimpleNamespace(run_id="run-1"),
            ),
        ),
        environment_resources=cast(
            "Any",
            _EnvironmentResources(environment_request, shared_session, environment),
        ),
        agent_backend="stub",
        provider="codex",
        model="gpt-test",
        role_models=("gpt-worker",),
        config=Config.model_validate(
            {
                "model": {"name": "gpt-test"},
                "agent": {"backend": "stub", "cli_provider": "codex"},
            }
        ),
        backend=ComputeBackend.CPU,
        skill_source_dirs=(),
        host_resources=(),
        clock=ManualClock(),
        threads=OsThreads(),
    )


def _launch(readable_path: Path) -> AuxiliaryAgentLaunch:
    return AuxiliaryAgentLaunch(
        role="chat",
        member_id="thread-1",
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
    session = _session(tmp_path)
    observed: list[RunReady] = []
    session.on_ready(observed.append)

    session._handle_resources(_resources(tmp_path, environment))  # noqa: SLF001  # lint-waiver: LW-948027 [SLF001]; exercise the private composition input and assert only its public projection escapes.

    assert observed == [
        RunReady(
            record=observed[0].record,
            log_directory=tmp_path / "logs",
            frontend_state_directory=observed[0].frontend_state_directory,
            agent_provider="codex",
            agent_model="gpt-test",
            agent_providers=("claude", "codex", "gemini", "opencode"),
            role_models=("gpt-worker",),
        )
    ]
    assert not hasattr(observed[0], "config")
    assert not hasattr(observed[0], "environment")
    assert not hasattr(observed[0], "host_resources")


def test_ready_projection_rejects_inconsistent_agent_defaults(tmp_path: Path) -> None:
    environment = _Environment()
    session = _session(tmp_path)
    observed: list[RunReady] = []
    session.on_ready(observed.append)
    session._handle_resources(_resources(tmp_path, environment))  # noqa: SLF001  # lint-waiver: LW-101220 [SLF001]; exercise the private composition input and validate its public projection contract.
    payload = observed[0].model_dump()

    for providers, message in (
        (["codex", "codex"], "must be unique"),
        ([], "at least one provider"),
        ([""], "must not be empty"),
        (["claude"], "is not supported"),
    ):
        with pytest.raises(ValidationError, match=message):
            RunReady.model_validate(payload | {"agent_providers": providers})


def test_managed_agent_hides_environment_and_owns_cleanup(
    tmp_path: Path, agent_settings: LaunchSettings
) -> None:
    environment = _Environment()
    session = _session(tmp_path, agent_settings)
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
    assert environment.sessions[-1].close_count == 1
    with pytest.raises(RuntimeError, match="closed"):
        agent.turn("Another question")


@pytest.mark.parametrize("failures", [1, 2])
def test_an_environment_whose_close_failed_is_closed_again_on_the_next_session_close(
    tmp_path: Path, agent_settings: LaunchSettings, failures: int
) -> None:
    """Regression: a failed close latched the session and its scope, so a retry closed nothing."""
    environment = _Environment(close_failures=failures)
    session = _session(tmp_path, agent_settings)
    session._handle_resources(_resources(tmp_path, environment))  # noqa: SLF001  # lint-waiver: LW-948032 [SLF001]; exercise real product composition over deterministic resources.
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    session.create_auxiliary_agent(_launch(evidence))

    for _ in range(failures):
        with pytest.raises(RuntimeError, match="killed"):
            session.close()
        with pytest.raises(RuntimeError, match="closed"):
            session.create_auxiliary_agent(_launch(evidence))
    session.close()
    session.close()

    assert environment.sessions[-1].close_count == failures + 1


def test_auxiliary_agent_creation_requires_readiness_and_existing_inputs(
    tmp_path: Path,
    agent_settings: LaunchSettings,
) -> None:
    session = _session(tmp_path, agent_settings)
    missing = tmp_path / "missing"

    with pytest.raises(RuntimeError, match="not ready"):
        session.create_auxiliary_agent(_launch(missing))

    session._handle_resources(_resources(tmp_path, _Environment()))  # noqa: SLF001  # lint-waiver: LW-948030 [SLF001]; inject the private composition fact needed to test public rejection.
    with pytest.raises(FileNotFoundError, match="does not exist"):
        session.create_auxiliary_agent(_launch(missing))


def test_auxiliary_agent_projection_failure_closes_pending_environment(
    tmp_path: Path, agent_settings: LaunchSettings
) -> None:
    environment = _Environment(path_error=RuntimeError("path projection failed"))
    session = _session(tmp_path, agent_settings)
    session._handle_resources(_resources(tmp_path, environment))  # noqa: SLF001  # lint-waiver: LW-101221 [SLF001]; exercise public construction cleanup over deterministic resource fakes.
    evidence = tmp_path / "evidence"
    evidence.mkdir()

    with pytest.raises(RuntimeError, match="path projection failed"):
        session.create_auxiliary_agent(_launch(evidence))

    assert environment.sessions[-1].close_count == 1


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


def test_auxiliary_selection_rejected_before_environment_acquisition(
    tmp_path: Path,
    agent_settings: LaunchSettings,
) -> None:
    """Every implementation rejects invalid selection before provisioning."""
    environment = _Environment()
    resources = _resources(tmp_path, environment)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    provider = "unknown"
    launch = AuxiliaryAgentLaunch.model_validate(
        _launch(evidence).model_dump() | {"provider": provider}
    )
    agents = agent_settings.agents or BuiltInSessionAgents(client_factory=_resumable_client)

    with pytest.raises(ValueError, match=provider):
        agents.create_agent(launch, resources, NULL_AGENT_EVENT_SINK)

    assert environment.requests == []
    assert environment.sessions == []


def test_independent_auxiliary_scope_survives_session_close(
    tmp_path: Path, agent_settings: LaunchSettings
) -> None:
    """The transferred scope owns conversations beyond run completion."""
    request, registry = _request(tmp_path)
    request = request.model_copy(
        update={"backend": ComputeBackend.CPU, "run_environment": fake_docker_environment()}
    )
    evidence = tmp_path / "evidence"
    evidence.mkdir()

    # The built-in agents answer through the run's own client; the fake ones answer on their own.
    answer = (
        "Fake agent inspected the trajectory."
        if agent_settings.agents is None
        else "Stub agent inspected the available experiment trajectory."
    )

    async def execute() -> None:
        settings = LaunchSettings(
            blocking=ThreadBlockingRunner(),
            network=UnixNetwork(),
            timing=production_timing(),
            processes=REAL_PROCESSES,
            piped_processes=FakePipedProcessLauncher(),
            registry=registry,
            backend_factory=host_container_backend,
            agents=agent_settings.agents,
            agent_client_factory=_resumable_client,
        )
        handle = default_runs(settings).start(request)
        scopes: list[AuxiliaryAgents] = []
        conversations: list[ManagedAgent] = []

        def ready(_ready: RunReady) -> None:
            scope = handle.session.open_auxiliary_agents()
            scopes.append(scope)
            conversations.append(scope.create_auxiliary_agent(_launch(evidence)))

        handle.session.on_ready(ready)
        try:
            result = await handle.result()
            assert result.loop == "stub"
            assert len(scopes) == len(conversations) == 1
            scope = scopes[0]
            first = conversations[0]
            assert first.turn("Continue after completion") == answer
            second = scope.create_auxiliary_agent(_launch(evidence))
            assert second.turn("New terminal conversation") == answer
            scope.close()
            scope.close()
            with pytest.raises(RuntimeError, match="closed"):
                first.turn("After scope close")
            with pytest.raises(RuntimeError, match="closed"):
                second.turn("After scope close")
            with pytest.raises(RuntimeError, match="closed"):
                scope.create_auxiliary_agent(_launch(evidence))
            with pytest.raises(RuntimeError, match="closed"):
                handle.session.open_auxiliary_agents()
        finally:
            for scope in scopes:
                scope.close()

    asyncio.run(execute())


def test_independent_auxiliary_scope_requires_run_readiness(tmp_path: Path) -> None:
    session = _session(tmp_path)
    with pytest.raises(RuntimeError, match="not ready"):
        session.open_auxiliary_agents()
