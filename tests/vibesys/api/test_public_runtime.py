"""A custom policy uses only public contracts to drive agents and messages."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from vibesys.api import (
    AgentBackend,
    AgentDefinition,
    AgentSpec,
    ComputeBackend,
    Config,
    ConfigurationError,
    CoreEvent,
    HostResource,
    HostResourceAccess,
    OrchestrationDescriptor,
    OrchestrationRegistry,
    ProfilerKind,
    ResumeRef,
    RunRequest,
    VibeSysRuntime,
    create_session,
    open_run_store,
)
from vibesys.api._orchestrations.runtime import _LocalVibeSysRuntime
from vibesys.api.request import RunEnvironmentSpec, load_input_bundle
from vibesys.api.session import _OpenedAgentEnvironment
from vibesys.events import CoreEventType
from vibesys.run.integration import LocalRunIntegration
from vibesys.sandbox.run_environment import LocalEnvironment
from vs_agent.api import AgentExecutionPolicy
from vs_agent.api.testing import FakeAgentClient
from vs_project.api import OrchestrationRunManifest, Project

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.sandbox.run_environment import RunEnvironmentRequest, RunEnvironmentSession


class _ThreeAgentPolicy:
    def __init__(self, resource: HostResource) -> None:
        self.resource = resource
        self.workspace: Path | None = None

    def execute(self, request: RunRequest, runtime: VibeSysRuntime) -> bool:
        assert request.orchestration is not None
        rounds = request.orchestration.options["rounds"]
        assert isinstance(rounds, int)
        self.workspace = runtime.workspace
        planner = runtime.spawn_agent(
            AgentDefinition("planner", AgentSpec(backend=AgentBackend.STUB, model="planner-model"))
        )
        worker = runtime.spawn_agent(
            AgentDefinition(
                "worker",
                AgentSpec(backend=AgentBackend.STUB, model="worker-model"),
                resources=(self.resource,),
            )
        )
        reviewer = runtime.spawn_agent(
            AgentDefinition(
                "reviewer", AgentSpec(backend=AgentBackend.STUB, model="reviewer-model")
            )
        )
        feedback = "begin"
        for round_number in range(1, rounds + 1):
            label = f"round{round_number:03d}"
            plan = planner.turn(feedback, label=label)
            implementation = worker.turn(plan, label=label)
            feedback = reviewer.turn(implementation, label=label)
        return feedback == "review 2"


class _CleanupError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("agent cleanup failed")


class _PolicyError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("policy failed")


class _CloseProbe:
    def __init__(self, name: str, calls: list[str], *, fail: bool = False) -> None:
        self.name = name
        self.calls = calls
        self.fail = fail

    def close(self) -> None:
        self.calls.append(self.name)
        if self.fail:
            raise _CleanupError


class _UnsupportedExecutionPolicy:
    def execute(self, request: RunRequest, runtime: VibeSysRuntime) -> bool:
        del request
        runtime.spawn_agent(
            AgentDefinition(
                "worker",
                AgentSpec(
                    backend=AgentBackend.STUB,
                    execution=AgentExecutionPolicy(
                        host_resources=(HostResource(runtime.workspace),)
                    ),
                ),
            )
        )
        return True


def _write_project(root: Path) -> None:
    root.mkdir()
    (root / "OBJECTIVE.md").write_text("Improve the queue.\n")
    (root / "queue.py").write_text("VALUE = 1\n")
    (root / "vibesys.input.toml").write_text(
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n[benchmark]\ncommand = ["true"]\n'
    )


def _request(project_root: Path) -> RunRequest:
    return RunRequest(
        project_root=project_root,
        orchestration=OrchestrationDescriptor(
            id="three-agent-rounds", config_version=1, options={"rounds": 2}
        ),
        config=Config.model_validate({"model": {"name": "gpt-test"}}),
        input_bundle=load_input_bundle(project_root),
        objective="Improve the queue.",
        exp_name="team-demo",
        run_environment=RunEnvironmentSpec("local"),
        agent_backend="stub",
        cli_provider="claude",
        profiler_kind=ProfilerKind.NONE,
        backend=ComputeBackend.CPU,
    )


def _assert_message_handoffs(clients: dict[str, FakeAgentClient]) -> None:
    assert clients["planner-model"].calls_for("planner")[0].user_prompt == "begin"
    assert [call.user_prompt for call in clients["worker-model"].calls_for("worker")] == [
        "plan 1",
        "plan 2",
    ]
    assert [call.user_prompt for call in clients["reviewer-model"].calls_for("reviewer")] == [
        "impl 1",
        "impl 2",
    ]
    assert clients["planner-model"].calls_for("planner")[1].user_prompt == "review 1"


def _capture_environments(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[RunEnvironmentRequest], list[str]]:
    requests: list[RunEnvironmentRequest] = []
    closed: list[str] = []
    original_open = LocalEnvironment.open
    original_close = _OpenedAgentEnvironment.close

    def open_environment(
        self: LocalEnvironment, request: RunEnvironmentRequest
    ) -> RunEnvironmentSession:
        requests.append(request)
        return original_open(self, request)

    def close_environment(self: _OpenedAgentEnvironment) -> None:
        closed.append(self.run_id)
        original_close(self)

    monkeypatch.setattr(LocalEnvironment, "open", open_environment)
    monkeypatch.setattr(_OpenedAgentEnvironment, "close", close_environment)
    return requests, closed


def test_public_runtime_runs_three_agent_rounds_with_grants_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = tmp_path / "project"
    _write_project(project_root)
    grant_path = tmp_path / "evidence.txt"
    grant_path.write_text("read only evidence\n")
    grant = HostResource(grant_path, HostResourceAccess.READ_ONLY, "worker evidence")
    policy = _ThreeAgentPolicy(grant)
    clients = {
        "planner-model": FakeAgentClient().enqueue_text("planner", "plan 1", "plan 2"),
        "worker-model": FakeAgentClient().enqueue_text("worker", "impl 1", "impl 2"),
        "reviewer-model": FakeAgentClient().enqueue_text("reviewer", "review 1", "review 2"),
    }
    granted: dict[str, tuple[HostResource, ...]] = {}

    def build_client(**kwargs: object) -> FakeAgentClient:
        spec = kwargs["spec"]
        assert isinstance(spec, AgentSpec)
        assert spec.model is not None
        resources = kwargs["host_resources"]
        assert isinstance(resources, tuple)
        granted[spec.model] = resources
        return clients[spec.model]

    def reject_default_client(**_kwargs: object) -> None:
        raise AssertionError

    environment_requests, closed_environments = _capture_environments(monkeypatch)
    monkeypatch.setattr("vibesys.api._orchestrations.runtime.build_agent_client", build_client)
    monkeypatch.setattr("vibesys.context.build_agent_client", reject_default_client)
    monkeypatch.setattr("vibesys.context.agent_spec_from_config", reject_default_client)

    request = _request(project_root)
    registry = OrchestrationRegistry()
    registry.register("three-agent-rounds", policy)
    events: list[CoreEvent] = []

    def record(event: CoreEvent) -> None:
        events.append(event)

    session = create_session(request, sink=record, registry=registry)
    session.start()
    result = asyncio.run(session.await_result())

    assert result.succeeded
    assert result.loop == "three-agent-rounds"
    assert session.view().run_id == result.run_id
    assert policy.workspace == project_root
    _assert_message_handoffs(clients)
    assert grant in granted["worker-model"]
    assert grant not in granted["planner-model"]
    assert (
        sum(
            mount.host_path == grant_path
            for request in environment_requests
            for mount in request.environment_bind_mounts
        )
        == 1
    )
    assert environment_requests[0].cli_provider == "claude"
    assert all(request.cli_provider == "codex" for request in environment_requests[1:])
    assert all(request.agent_backend == "stub" for request in environment_requests[1:])
    assert all(client.closed for client in clients.values())
    assert len(closed_environments) == 3
    assert (
        len([event for event in events if event.type is CoreEventType.AGENT_EXECUTION_STARTED]) == 6
    )
    manifest = Project.open(project_root).state.load_run(result.run_id)
    assert isinstance(manifest, OrchestrationRunManifest)
    assert manifest.orchestration == request.orchestration
    assert (
        open_run_store(Project.open(project_root)).get_run(result.run_id).loop
        == "three-agent-rounds"
    )


def test_custom_resume_requires_policy_checkpoint_contract(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    _write_project(project_root)
    request = _request(project_root).model_copy(update={"resume": ResumeRef(run_id="prior-run")})
    registry = OrchestrationRegistry()
    registry.register("three-agent-rounds", _ThreeAgentPolicy(HostResource(tmp_path)))

    def discard(event: CoreEvent) -> None:
        del event

    with pytest.raises(ConfigurationError, match="policy-owned checkpoint contract"):
        asyncio.run(create_session(request, sink=discard, registry=registry).await_result())

    active_profiler = _request(project_root).model_copy(update={"profiler_kind": ProfilerKind.NSYS})
    with pytest.raises(ConfigurationError, match="does not yet provide a profiler capability"):
        asyncio.run(create_session(active_profiler, sink=discard, registry=registry).await_result())


def test_skypilot_custom_runtime_is_rejected_before_environment_open(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    _write_project(project_root)
    request = _request(project_root).model_copy(
        update={"run_environment": RunEnvironmentSpec("skypilot")}
    )
    registry = OrchestrationRegistry()
    registry.register("three-agent-rounds", _ThreeAgentPolicy(HostResource(tmp_path)))

    def discard(event: CoreEvent) -> None:
        del event

    with pytest.raises(ConfigurationError, match="per-agent bridge ownership"):
        asyncio.run(create_session(request, sink=discard, registry=registry).await_result())
    assert not (project_root / ".vibesys").exists()


def test_agent_spec_execution_policy_is_rejected_explicitly(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    _write_project(project_root)
    registry = OrchestrationRegistry()
    registry.register("three-agent-rounds", _UnsupportedExecutionPolicy())

    def discard(event: CoreEvent) -> None:
        del event

    with pytest.raises(ValueError, match=r"AgentSpec\.execution is not supported"):
        asyncio.run(
            create_session(_request(project_root), sink=discard, registry=registry).await_result()
        )


def test_runtime_closes_all_agents_and_preserves_policy_failure() -> None:
    integration = LocalRunIntegration()
    runtime = _LocalVibeSysRuntime(
        RunRequest.model_construct(loop=None), integration, open_agent_environment=None
    )
    closed: list[str] = []
    vars(runtime)["_context"] = _CloseProbe("context", closed)
    vars(runtime)["_agents"] = {
        "first": _CloseProbe("first", closed),
        "middle": _CloseProbe("middle", closed, fail=True),
        "last": _CloseProbe("last", closed),
    }
    try:
        with pytest.raises(_PolicyError) as caught, runtime:
            raise _PolicyError
        assert closed == ["last", "middle", "first", "context"]
        assert any("cleanup also failed" in note for note in caught.value.__notes__)
    finally:
        integration.close()
