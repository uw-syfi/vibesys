"""A custom policy uses only public contracts to drive agents and messages."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
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
from vibesys.orchestration.runtime import RunContext, _Evaluator
from vibesys.run.integration import LocalRunIntegration
from vibesys.sandbox.run_environment import LocalEnvironment, SkyPilotEnvironment
from vs_agent.api import AgentExecutionPolicy, AgentSessionKey, SessionScope
from vs_agent.api.testing import FakeAgentClient
from vs_project.api import OrchestrationRunManifest, Project

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.context import _RunResources
    from vibesys.orchestration.runtime import WorkspaceHandle, _LocalAgentHandle
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


class _PolicyState(BaseModel):
    value: int


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


def test_root_workspace_and_typed_state_capabilities(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    _write_project(project_root)
    integration = LocalRunIntegration()

    async def exercise() -> None:
        setup = RunSetup(state_namespace="public_probe", state_slots={"state.json": _PolicyState})
        async with RunContext.open(_request(project_root), integration, setup=setup) as ctx:
            assert ctx.state.local_path("notes/cursor.json").name == "cursor.json"
            assert ctx.state.artifact_path("reports").is_dir()
            with pytest.raises(TypeError, match="not declared"):
                ctx.state.slot("other.json", _PolicyState)
            await ctx.state.checkpoint(
                sequence=1,
                writes={"state.json": _PolicyState(value=7)},
                candidate=False,
            )
            assert await ctx.state.load(_PolicyState) == _PolicyState(value=7)

            root = ctx.workspaces.root
            original = root.revision
            assert original is not None
            assert await root.trusted_input_changes() == []
            (root.path / "queue.py").write_text("VALUE = 2\n")
            assert "queue.py" in await root.pending_changes()
            changed = await root.snapshot("public runtime candidate")
            assert changed == root.revision
            assert "VALUE = 2" in await root.candidate_patch(changed)
            await root.restore(original)
            assert (root.path / "queue.py").read_text() == "VALUE = 1\n"
            await root.restore(changed)
            assert (root.path / "queue.py").read_text() == "VALUE = 2\n"
            assert (await root.retain("public-probe", changed)).endswith("/candidates/public-probe")
            with pytest.raises(ValueError, match="run root cannot be discarded"):
                await root.discard()
            with pytest.raises(RuntimeError, match="cannot open isolated candidate sandboxes"):
                await ctx.workspaces.fork()

    try:
        asyncio.run(exercise())
    finally:
        integration.close()


def test_root_environment_and_trusted_evaluator_capabilities(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    _write_project(project_root)
    integration = LocalRunIntegration()

    async def exercise() -> None:
        async with RunContext.open(_request(project_root), integration, setup=RunSetup()) as ctx:
            with pytest.raises(TypeError, match="portable state namespace"):
                _ = ctx.state.namespace
            assert ctx.environment.view.env_kind == "local"
            assert ctx.environment.view_for() is ctx.environment.view
            assert ctx.environment.reference_path
            assert ctx.environment.model_name == "gpt-test"
            assert ctx.environment.profiler_kind is ProfilerKind.NONE
            assert ctx.environment.workspace_sources == ()
            assert isinstance(ctx.environment.skill_source_paths, tuple)
            assert ctx.environment.run_log_path.parent == ctx.environment.log_dir
            assert ctx.environment.candidate_runtime(1, 1) is not None
            await ctx.control.boundary()
            await ctx.control.debug_step("host probe")
            ctx.switch_log("host-probe")
            ctx.log("host capability probe")
            assert await ctx.environment.reconcile_model_requests() is None
            await ctx.environment.reselect_device()
            await ctx.environment.teardown_deployment("unused")
            execution = await ctx.environment.execute("printf host-ok")
            assert execution.exit_code == 0
            assert "host-ok" in execution.output

            accuracy = await ctx.gates.check("host-probe", label="host-probe")
            assert accuracy.passed
            reused = await ctx.gates.reuse_accuracy(label="host-probe")
            assert reused.passed
            assert not reused.executed
            benchmark = await ctx.gates.measure("host-probe")
            assert not benchmark.executed

    try:
        asyncio.run(exercise())
    finally:
        integration.close()


def test_scoped_workspace_adopts_candidate_and_closes_its_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = tmp_path / "project"
    _write_project(project_root)
    integration = LocalRunIntegration()
    client = FakeAgentClient(model="scope").enqueue_text("worker", "scoped response")
    monkeypatch.setattr(
        "vibesys.orchestration.runtime.build_agent_client", lambda **_kwargs: client
    )

    async def exercise() -> None:
        async with RunContext.open(_request(project_root), integration, setup=RunSetup()) as ctx:
            # Local worktrees provide a cheap substrate for the generic parallel capability.
            ctx._resources.run_environment_view = replace(  # noqa: SLF001
                ctx.environment.view, supports_parallel_candidate_evaluation=True
            )
            parent_revision = ctx.workspaces.root.revision
            assert parent_revision is not None
            scoped = await ctx.workspaces.fork(parent_revision)
            assert scoped.id is not None
            assert scoped.path != ctx.workspaces.root.path
            assert ctx.environment.view_for(scoped).env_kind == "local"
            agent = await ctx.agents.spawn(
                AgentDefinition("worker", AgentSpec(backend=AgentBackend.STUB, model="scope")),
                scope=scoped,
            )
            assert agent.backend_name == "fake"
            assert agent.driver_name == "fake"
            assert agent.provider == "fake"
            assert agent.model == "scope"
            assert not agent.capabilities.mcp_servers
            assert await agent.turn("work in the fork", label="scoped-turn") == "scoped response"
            (scoped.path / "queue.py").write_text("VALUE = 3\n")
            assert "queue.py" in await scoped.pending_changes()
            revision = await scoped.snapshot("scoped candidate")
            assert scoped.revision == revision
            assert "VALUE = 3" in await scoped.candidate_patch(revision)
            await scoped.restore(parent_revision)
            assert (scoped.path / "queue.py").read_text() == "VALUE = 1\n"
            await scoped.restore(revision)
            assert (scoped.path / "queue.py").read_text() == "VALUE = 3\n"
            assert (ctx.workspaces.root.path / "queue.py").read_text() == "VALUE = 1\n"
            await ctx.workspaces.adopt(revision)
            assert (ctx.workspaces.root.path / "queue.py").read_text() == "VALUE = 3\n"
            await scoped.discard()
            assert client.closed
            with pytest.raises(ValueError, match="closed"):
                _ = scoped.path

    try:
        asyncio.run(exercise())
    finally:
        integration.close()


class _ScopedCloseProbe:
    """Fake scoped agent handle for exercising ``_discard_scope`` cleanup ordering.

    ``close()`` is idempotent (like the real agent handle): a discarded scope's
    agents stay in ``RunContext._agents`` and get a second ``close()`` call
    when the run itself closes, so a fake that kept re-raising on every call
    would fail the run teardown for reasons unrelated to what this test
    covers.
    """

    def __init__(
        self, scope_id: str, name: str, calls: list[str], *, exc: BaseException | None = None
    ) -> None:
        self.scope_id = scope_id
        self._name = name
        self._calls = calls
        self._exc = exc
        self._closed = False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._calls.append(self._name)
        if self._exc is not None:
            raise self._exc


def test_discard_scope_continues_after_a_cancelled_agent_close(tmp_path: Path) -> None:
    """Regression: ``_discard_scope`` used to catch only ``Exception``, so a
    ``CancelledError`` from one agent's ``close()`` (a ``BaseException``, not an
    ``Exception``) escaped immediately -- skipping every remaining agent's
    cleanup and the worktree teardown entirely. It must instead behave like
    ``RunContext.close()``'s sibling cleanup path: catch ``BaseException``,
    keep closing the remaining agents, tear down the worktree, and surface
    every error afterward.
    """
    project_root = tmp_path / "project"
    _write_project(project_root)
    integration = LocalRunIntegration()

    async def exercise() -> None:
        async with RunContext.open(_request(project_root), integration, setup=RunSetup()) as ctx:
            ctx._resources.run_environment_view = replace(  # noqa: SLF001
                ctx.environment.view, supports_parallel_candidate_evaluation=True
            )
            parent_revision = ctx.workspaces.root.revision
            assert parent_revision is not None
            scoped = await ctx.workspaces.fork(parent_revision)
            assert scoped.id is not None
            calls: list[str] = []
            ctx._agents[(scoped.id, "first")] = cast(  # noqa: SLF001
                "_LocalAgentHandle",
                _ScopedCloseProbe(scoped.id, "first", calls, exc=asyncio.CancelledError()),
            )
            ctx._agents[(scoped.id, "second")] = cast(  # noqa: SLF001
                "_LocalAgentHandle", _ScopedCloseProbe(scoped.id, "second", calls)
            )

            with pytest.raises(BaseExceptionGroup) as caught:
                await scoped.discard()

            # Both agents were closed despite the first raising a BaseException.
            assert calls == ["first", "second"]
            assert isinstance(caught.value.exceptions[0], asyncio.CancelledError)
            # The worktree itself was still torn down.
            with pytest.raises(ValueError, match="closed"):
                _ = scoped.path

    try:
        asyncio.run(exercise())
    finally:
        integration.close()


@given(
    exception_specs=st.lists(
        st.sampled_from([None, RuntimeError, asyncio.CancelledError]),
        min_size=1,
        max_size=4,
    )
)
@settings(
    max_examples=15, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_discard_scope_closes_every_agent_for_any_failure_mix(
    exception_specs: list[type[BaseException] | None],
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Property generalizing the CancelledError regression: whatever mix of
    ``Exception``/``BaseException`` failures scoped agents raise on
    ``close()`` -- including several ``CancelledError``s in a row -- discard()
    still attempts every agent's close() exactly once, in order, still tears
    down the worktree, and surfaces every failure (none silently dropped).
    """
    project_root = tmp_path_factory.mktemp("discard_scope_property") / "project"
    _write_project(project_root)
    integration = LocalRunIntegration()

    async def exercise() -> None:
        async with RunContext.open(_request(project_root), integration, setup=RunSetup()) as ctx:
            ctx._resources.run_environment_view = replace(  # noqa: SLF001
                ctx.environment.view, supports_parallel_candidate_evaluation=True
            )
            parent_revision = ctx.workspaces.root.revision
            assert parent_revision is not None
            scoped = await ctx.workspaces.fork(parent_revision)
            assert scoped.id is not None
            calls: list[str] = []
            expected_failures = 0
            names = [f"agent-{index}" for index in range(len(exception_specs))]
            for name, exc_type in zip(names, exception_specs, strict=True):
                exc = exc_type() if exc_type is not None else None
                if exc is not None:
                    expected_failures += 1
                ctx._agents[(scoped.id, name)] = cast(  # noqa: SLF001
                    "_LocalAgentHandle", _ScopedCloseProbe(scoped.id, name, calls, exc=exc)
                )

            if expected_failures:
                with pytest.raises(BaseExceptionGroup) as caught:
                    await scoped.discard()
                assert len(caught.value.exceptions) == expected_failures
            else:
                await scoped.discard()

            # Every agent's close() ran exactly once, regardless of failures.
            assert calls == names
            # The worktree itself was torn down either way.
            with pytest.raises(ValueError, match="closed"):
                _ = scoped.path

    try:
        asyncio.run(exercise())
    finally:
        integration.close()


class _FakeMutationHost:
    """Stand in for a RunContext's one parent-tree mutation lock (R6)."""

    def __init__(self) -> None:
        self._parent_mutation_lock = asyncio.Lock()


def test_gate_lock_shares_the_parent_mutation_lock_domain() -> None:
    """R6 regression: the evaluator no longer keeps its own lock for the
    parent tree. Old code failed this because ``_Evaluator.__init__`` built
    a private ``asyncio.Lock()`` for ``scope=None`` instead of reusing
    ``_parent_mutation_lock``.
    """
    host = cast("RunContext", _FakeMutationHost())
    evaluator = _Evaluator(host)
    assert evaluator._lock_for(None) is host._parent_mutation_lock  # noqa: SLF001
    root_handle = cast("WorkspaceHandle", SimpleNamespace(id=None))
    assert evaluator._lock_for(root_handle) is host._parent_mutation_lock  # noqa: SLF001


def test_gate_and_adopt_cannot_interleave_on_the_parent_tree() -> None:
    """R6 regression: a gate and an adopt/checkpoint on the parent tree
    must fully serialize. Old code let ``ctx.gates.check``/``measure``
    run concurrently with ``ctx.workspaces.adopt``/``ctx.state.checkpoint``
    because they held different lock objects; this asserts the order is
    never interleaved.
    """
    host = cast("RunContext", _FakeMutationHost())
    evaluator = _Evaluator(host)
    events: list[str] = []

    async def gate() -> None:
        async with evaluator._lock_for(None):  # noqa: SLF001
            events.append("gate-start")
            await asyncio.sleep(0.01)
            events.append("gate-end")

    async def adopt() -> None:
        async with host._parent_mutation_lock:  # noqa: SLF001
            events.append("adopt-start")
            await asyncio.sleep(0.01)
            events.append("adopt-end")

    async def exercise() -> None:
        await asyncio.gather(gate(), adopt())

    asyncio.run(exercise())
    assert events in (
        ["gate-start", "gate-end", "adopt-start", "adopt-end"],
        ["adopt-start", "adopt-end", "gate-start", "gate-end"],
    )


@given(
    kinds=st.lists(st.sampled_from(["gate", "adopt", "checkpoint"]), min_size=2, max_size=8),
    delays=st.lists(
        st.floats(min_value=0.0, max_value=0.005, allow_nan=False, allow_infinity=False),
        min_size=2,
        max_size=8,
    ),
)
@settings(
    max_examples=50, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_parent_tree_lock_domain_serializes_any_gate_adopt_checkpoint_mix(
    kinds: list[str], delays: list[float]
) -> None:
    """Property: whatever mix and interleaving of gate/adopt/checkpoint
    tasks race on the parent tree's mutation lock, at most one holds it at
    a time (R6: one lock domain for the parent tree).
    """
    host = cast("RunContext", _FakeMutationHost())
    evaluator = _Evaluator(host)
    held = 0
    max_held = 0

    def _lock_for(kind: str) -> asyncio.Lock:
        if kind == "gate":
            return evaluator._lock_for(None)  # noqa: SLF001
        return host._parent_mutation_lock  # noqa: SLF001

    async def task(kind: str, delay: float) -> None:
        nonlocal held, max_held
        async with _lock_for(kind):
            held += 1
            max_held = max(max_held, held)
            await asyncio.sleep(delay)
            held -= 1

    async def exercise() -> None:
        pairs = list(zip(kinds, delays, strict=False))
        await asyncio.gather(*(task(kind, delay) for kind, delay in pairs))

    asyncio.run(exercise())
    assert max_held <= 1
    assert held == 0
