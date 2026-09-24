"""A custom policy uses only public contracts to drive agents and messages."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from pydantic import BaseModel

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
    RunRequest,
    create_session,
    open_run_store,
)
from vibesys.api.request import RunEnvironmentSpec, load_input_bundle
from vibesys.api.session import _OpenedAgentEnvironment
from vibesys.context import RunSetup, borrow_run_agent_environment
from vibesys.events import AgentExecutionFinishedData, CoreEventType
from vibesys.orchestration.runtime import RunContext
from vibesys.run.integration import LocalRunIntegration
from vibesys.sandbox.run_environment import LocalEnvironment, SkyPilotEnvironment
from vs_agent.api import AgentExecutionPolicy, AgentSessionKey, SessionScope
from vs_agent.api.testing import FakeAgentClient
from vs_project.api import OrchestrationRunManifest, Project

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.context import _RunResources
    from vibesys.sandbox.run_environment import RunEnvironmentRequest, RunEnvironmentSession


class _ThreeAgentPolicy:
    resource: HostResource
    workspace: Path | None = None

    def __init__(self, descriptor: OrchestrationDescriptor) -> None:
        self.rounds = descriptor.options["rounds"]
        self.setup = RunSetup()

    async def run(self, ctx: RunContext) -> bool:
        rounds = self.rounds
        assert isinstance(rounds, int)
        type(self).workspace = ctx.workspaces.root.path
        planner = await ctx.agents.spawn(
            AgentDefinition("planner", AgentSpec(backend=AgentBackend.STUB, model="planner-model"))
        )
        worker = await ctx.agents.spawn(
            AgentDefinition(
                "worker",
                AgentSpec(backend=AgentBackend.STUB, model="worker-model"),
                resources=(type(self).resource,),
            )
        )
        reviewer = await ctx.agents.spawn(
            AgentDefinition(
                "reviewer", AgentSpec(backend=AgentBackend.STUB, model="reviewer-model")
            )
        )
        feedback = "begin"
        for round_number in range(1, rounds + 1):
            label = f"round{round_number:03d}"
            plan = await planner.turn(feedback, label=label)
            implementation = await worker.turn(plan, label=label)
            feedback = await reviewer.turn(implementation, label=label)
        return feedback == "review 2"


class _TypedPlan(BaseModel):
    task: str


class _TypedPolicy:
    def __init__(self, descriptor: OrchestrationDescriptor) -> None:
        assert descriptor.id == "three-agent-rounds"
        self.setup = RunSetup()

    async def run(self, ctx: RunContext) -> bool:
        planner = await ctx.agents.spawn(
            AgentDefinition("planner", AgentSpec(backend=AgentBackend.STUB, model="typed"))
        )
        plan = await planner.turn_structured(
            "choose task",
            response_cls=_TypedPlan,
            fallback_factory=lambda: _TypedPlan(task="fallback"),
            system_prompt="plan carefully",
            label="round-1-plan",
            session_key=AgentSessionKey(SessionScope.ROLE, "planner"),
            reuse_session=True,
        )
        return plan.task == "implement"


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


class _AsyncCloseProbe:
    def __init__(self, name: str, calls: list[str], *, fail: bool = False) -> None:
        self._probe = _CloseProbe(name, calls, fail=fail)

    async def close(self) -> None:
        self._probe.close()


class _UnsupportedExecutionPolicy:
    def __init__(self, descriptor: OrchestrationDescriptor) -> None:
        assert descriptor.id == "three-agent-rounds"
        self.setup = RunSetup()

    async def run(self, ctx: RunContext) -> bool:
        await ctx.agents.spawn(
            AgentDefinition(
                "worker",
                AgentSpec(
                    backend=AgentBackend.STUB,
                    execution=AgentExecutionPolicy(
                        host_resources=(HostResource(ctx.workspaces.root.path),)
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
    _ThreeAgentPolicy.resource = grant
    _ThreeAgentPolicy.workspace = None
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

    environment_requests, closed_environments = _capture_environments(monkeypatch)
    monkeypatch.setattr("vibesys.orchestration.runtime.build_agent_client", build_client)

    request = _request(project_root)
    registry = OrchestrationRegistry()
    registry.register("three-agent-rounds", _ThreeAgentPolicy)
    events: list[CoreEvent] = []

    def record(event: CoreEvent) -> None:
        events.append(event)

    session = create_session(request, sink=record, registry=registry)
    session.start()
    result = asyncio.run(session.await_result())

    assert result.succeeded
    assert result.loop == "three-agent-rounds"
    assert session.view().run_id == result.run_id
    assert _ThreeAgentPolicy.workspace == project_root
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


def test_structured_turn_preserves_schema_session_and_event_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = tmp_path / "project"
    _write_project(project_root)
    client = FakeAgentClient().enqueue("planner", _TypedPlan(task="implement"))
    monkeypatch.setattr(
        "vibesys.orchestration.runtime.build_agent_client", lambda **_kwargs: client
    )
    request = _request(project_root)
    registry = OrchestrationRegistry()
    registry.register("three-agent-rounds", _TypedPolicy)
    events: list[CoreEvent] = []

    def record(event: CoreEvent) -> None:
        events.append(event)

    session = create_session(request, sink=record, registry=registry)

    session.start()
    assert asyncio.run(session.await_result()).succeeded

    call = client.calls_for("planner")[0]
    assert call.method == "invoke"
    assert call.response_cls is _TypedPlan
    assert call.session_key == AgentSessionKey(SessionScope.ROLE, "planner")
    assert call.reuse_session is True
    assert call.system_prompt == "plan carefully"
    assert call.user_prompt == "choose task"
    finished = next(
        event for event in events if event.type is CoreEventType.AGENT_EXECUTION_FINISHED
    )
    assert isinstance(finished.data, AgentExecutionFinishedData)
    assert finished.data.result == {"task": "implement"}
    assert finished.round_label == "round-1-plan"


def test_skypilot_agents_borrow_the_workspace_session() -> None:
    environment = SkyPilotEnvironment.from_options({"profile": "test-cluster"})
    assert environment.config.profile == "test-cluster"
    closed: list[bool] = []
    session = SimpleNamespace(
        view=SimpleNamespace(cli_sandboxed=False),
        close=lambda: closed.append(True),
    )
    context = cast(
        "_RunResources",
        SimpleNamespace(
            environment_request=SimpleNamespace(
                agent_backend="stub", cli_provider="claude", project_path_policy=None
            ),
            run_environment_session=session,
            skill_source_paths=(),
            backend=ComputeBackend.CPU,
            agent_host_resources=(),
        ),
    )
    borrowed = borrow_run_agent_environment(context, agent_backend="stub", cli_provider="claude")
    assert borrowed.session is session
    borrowed.close()
    assert not closed
    with pytest.raises(ConfigurationError, match="must use its configured backend"):
        borrow_run_agent_environment(context, cli_provider="codex")


def test_agent_spec_execution_policy_is_rejected_explicitly(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    _write_project(project_root)
    registry = OrchestrationRegistry()
    registry.register("three-agent-rounds", _UnsupportedExecutionPolicy)

    def discard(event: CoreEvent) -> None:
        del event

    with pytest.raises(ValueError, match=r"AgentSpec\.execution is not supported"):
        asyncio.run(
            create_session(_request(project_root), sink=discard, registry=registry).await_result()
        )


def test_runtime_closes_all_agents_and_preserves_policy_failure() -> None:
    integration = LocalRunIntegration()
    closed: list[str] = []

    class _ProbeRunContext(RunContext):
        def _prepare(self) -> None:
            vars(self)["_resource_owner"] = _CloseProbe("context", closed)
            vars(self)["_agents"] = {
                (None, "first"): _AsyncCloseProbe("first", closed),
                (None, "middle"): _AsyncCloseProbe("middle", closed, fail=True),
                (None, "last"): _AsyncCloseProbe("last", closed),
            }

    async def fail_inside_runtime() -> None:
        async with _ProbeRunContext.open(
            RunRequest.model_construct(), integration, setup=RunSetup()
        ):
            raise _PolicyError

    try:
        with pytest.raises(_PolicyError) as caught:
            asyncio.run(fail_inside_runtime())
        assert closed == ["last", "middle", "first", "context"]
        assert any("runtime cleanup also failed" in note for note in caught.value.__notes__)
    finally:
        integration.close()
