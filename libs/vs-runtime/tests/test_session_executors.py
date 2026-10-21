"""The production builder gives every session executor the one access settlement and fence."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import pytest
from tests.support.session_lifecycle_world import cancel_request
from tests.support.session_world import (
    ROLE,
    SCHEMA,
    Reply,
    dispatch_request,
    ensure_request,
)
from tests.support.workspace_world import RUN_ID, WorkspaceEnv, open_workspace_env

from vs_agent.api import (
    AgentBackend,
    AgentClient,
    AgentExecutionPolicy,
    AgentSessionSpec,
    AgentSpec,
    AgentTurnRequest,
)
from vs_agent.api.testing import FakeAgentInvocationStore, FakeProvider
from vs_core.api import ArtifactId, ArtifactRef, CancelTurn, DispatchTurn, ObservationStatus
from vs_runtime.api import AgentRole, ArtifactStore, RuntimeContractError, WorkspaceAccess
from vs_runtime.api.core import (
    ExecutionResult,
    ReceiptStore,
    ResolverInputs,
    SessionExecutors,
    StoreWorkspaceReceipts,
    open_session_requests,
    report_turns,
)
from vs_runtime.api.executor_contexts import context_for
from vs_runtime.api.infrastructure import (
    AgentExecutionConfiguration,
    AgentExecutionFinished,
    AgentExecutionStarted,
    AgentExecutionStatus,
    create_run_control_channel,
)
from vs_runtime.api.testing import FakeAgentExecutionLifecycleSink, FakeRunControlEventSink
from vs_sim.api.testing import Gate, InlineBlockingRunner, ManualClock, SimThreads

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from vs_core.api import RequestBase
    from vs_runtime.api.infrastructure import RunControlChannel
    from vs_sim.api import BlockingRunner

WRITTEN = "candidate.py"


@pytest.fixture(autouse=True)
def isolated_project_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path / "operator-state"))


@dataclass
class Provider:
    """A read-only role's provider that overwrites a file in the workspace, then may die."""

    env: WorkspaceEnv
    dies: bool = False
    turns: list[AgentTurnRequest] = field(default_factory=list)

    def on_turn(self, turn: AgentTurnRequest) -> None:
        self.turns.append(turn)
        (self.env.hosts[-1].root.path / WRITTEN).write_text("VALUE = 99\n", encoding="utf-8")
        if self.dies:
            message = "provider died after writing"
            raise ConnectionError(message)


@dataclass
class Run:
    """One run's disk state; ``start`` builds a host's executors only through the builder."""

    env: WorkspaceEnv
    provider: Provider
    journal: FakeAgentInvocationStore = field(default_factory=FakeAgentInvocationStore)
    lifecycle: FakeAgentExecutionLifecycleSink = field(
        default_factory=FakeAgentExecutionLifecycleSink
    )
    blocking: BlockingRunner = field(default_factory=InlineBlockingRunner)
    control: RunControlChannel | None = None
    executors: SessionExecutors = field(init=False)
    artifacts: ArtifactStore = field(init=False)

    def __post_init__(self) -> None:
        self.start()

    def start(self) -> None:
        """A host: fresh workspaces and executors over the same disk and journal."""
        if len(self.env.hosts) > 1 or hasattr(self, "executors"):
            self.env.start_host()
        store = ReceiptStore(self.env.receipts_namespace(), threads=SimThreads())
        artifacts = ArtifactStore(self.env.project.state.local_namespace(RUN_ID, "artifacts"))
        self.artifacts = artifacts

        def spec(selected: AgentRole, path: Path) -> AgentSessionSpec:
            return AgentSessionSpec(
                role=selected.id,
                provider="fake",
                workspace=path,
                policy=AgentExecutionPolicy(require_enforcement=False),
            )

        inputs = ResolverInputs(
            control=self.control,
            roles=(
                AgentRole(
                    id=ROLE.root,
                    system_prompt="You are a worker.",
                    workspace_access=WorkspaceAccess.READ_ONLY,
                ),
            ),
            schemas={SCHEMA: Reply},
            workspaces=self.env.hosts[-1],
            workspace_receipts=StoreWorkspaceReceipts(store),
            artifacts=artifacts,
            configuration=lambda selected: AgentExecutionConfiguration(
                selected.id, AgentSpec(backend=AgentBackend.STUB, cli_timeout=45)
            ),
            session_spec=spec,
        )
        client = report_turns(
            AgentClient(
                FakeProvider(
                    answer={"value": 7}, on_turn=self.provider.on_turn, threads=SimThreads()
                ),
                clock=ManualClock(),
                threads=SimThreads(),
            ),
            self.lifecycle,
            control=self.control,
        )
        self.executors = open_session_requests(
            inputs,
            client=client,
            invocation_slot=self.journal,
            store=store,
            blocking=self.blocking,
            threads=SimThreads(),
        )

    def dispatch(self) -> DispatchTurn:
        """The default turn, with a prompt that exists in the artifact store."""
        receipt = self.artifacts.write(b"Work.")
        prompt = ArtifactRef(
            artifact_id=ArtifactId(root=f"artifact-{receipt.sha256[:8]}"), digest=receipt.sha256
        )
        request = dispatch_request()
        return request.model_copy(
            update={"turn": request.turn.model_copy(update={"prompts": (prompt,)})}
        )

    @property
    def path(self) -> Path:
        return self.env.hosts[-1].root.path

    async def execute(self, request: RequestBase) -> ExecutionResult:
        executor = (
            self.executors.lifecycle if isinstance(request, CancelTurn) else self.executors.turns
        )
        outcome = await executor.execute(cast("Any", request), context_for(request))
        assert isinstance(outcome, ExecutionResult), outcome
        return outcome


def digest(path: Path) -> str:
    return hashlib.sha256((path / WRITTEN).read_bytes()).hexdigest()


@pytest.fixture
def run(tmp_path: Path) -> Iterator[Run]:
    with open_workspace_env(tmp_path) as env:
        yield Run(env, Provider(env))
        for workspaces in env.hosts:
            asyncio.run(workspaces.close())


def status(result: ExecutionResult) -> ObservationStatus:
    return result.observation.observation.status


@pytest.mark.asyncio
async def test_a_turn_dispatched_through_the_builder_has_its_write_reverted(run: Run) -> None:
    before = digest(run.path)
    await run.execute(ensure_request())
    result = await run.execute(run.dispatch())
    assert status(result) is not ObservationStatus.SUCCEEDED  # the violation is reported
    assert digest(run.path) == before
    assert run.provider.turns


@pytest.mark.asyncio
async def test_the_builders_lifecycle_executor_settles_what_a_dead_provider_wrote(
    run: Run,
) -> None:
    before = digest(run.path)
    run.provider.dies = True
    await run.execute(ensure_request())
    lost = await run.execute(run.dispatch())
    assert status(lost) is ObservationStatus.UNKNOWN
    assert digest(run.path) != before  # written and not yet judged
    cancelled = await run.execute(cancel_request())
    assert status(cancelled) is ObservationStatus.CANCELLED
    assert digest(run.path) == before


@pytest.mark.asyncio
async def test_a_snapshot_right_after_a_restart_is_refused_until_settlement_reverts_the_write(
    run: Run,
) -> None:
    before = digest(run.path)
    run.provider.dies = True
    await run.execute(ensure_request())
    await run.execute(run.dispatch())
    assert digest(run.path) != before

    run.start()  # a fresh host over the same Project; no executor has run a request yet
    written = digest(run.path)
    with pytest.raises(RuntimeContractError, match="fenced"):
        await run.env.hosts[-1].root.snapshot("early")
    assert digest(run.path) == written  # refused without adopting the write

    run.provider.dies = False
    cancelled = await run.execute(cancel_request())
    assert status(cancelled) is ObservationStatus.CANCELLED
    assert digest(run.path) == before
    assert await run.env.hosts[-1].root.snapshot("after")


@pytest.mark.asyncio
@pytest.mark.parametrize("dies", [False, True])
async def test_each_dispatched_turn_is_one_start_and_one_finish_and_a_replay_adds_none(
    run: Run, *, dies: bool
) -> None:
    run.provider.dies = dies
    request = run.dispatch()
    await run.execute(ensure_request())
    await run.execute(request)

    started, finished = run.lifecycle.events
    assert isinstance(started, AgentExecutionStarted)
    assert isinstance(finished, AgentExecutionFinished)
    assert (started.agent_id, started.execution_id) == (ROLE.root, request.turn.invocation_id.root)
    assert started.execution_id == finished.execution_id
    assert started.system_prompt == "You are a worker."
    assert started.user_prompt.strip()
    if dies:
        assert finished.status is AgentExecutionStatus.FAILED
        assert "provider died after writing" in (finished.error or "")
    else:
        assert finished.status is AgentExecutionStatus.COMPLETED
        assert finished.result == '{"value":7}'

    await run.execute(request)
    # A sealed turn replays; an unknown one is inspected, never re-dispatched.
    assert len(run.lifecycle.events) == 2


class _PreparationBoundary:
    """Faithfully execute blocking calls, holding one selected preparation boundary."""

    def __init__(self, boundary: int | None = None) -> None:
        self.boundary = boundary
        self.calls = 0
        self.arrived = Gate()
        self.release = Gate()

    async def run[**P, T](self, function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
        self.calls += 1
        if self.calls == self.boundary:
            self.arrived.open()
            await self.release.wait()
        return await InlineBlockingRunner().run(function, *args, **kwargs)


async def test_pause_drain_cancels_every_admitted_preparation_boundary(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    counter = _PreparationBoundary()
    with open_workspace_env(baseline) as env:
        run = Run(env, Provider(env), blocking=counter)
        await run.execute(ensure_request())
        before = counter.calls
        await run.execute(run.dispatch())
        boundaries = counter.calls - before
        for host in env.hosts:
            await host.close()
    assert boundaries > 1
    for boundary in range(1, boundaries + 1):
        root = tmp_path / str(boundary)
        root.mkdir()
        runner = _PreparationBoundary()
        control = create_run_control_channel(FakeRunControlEventSink(), threads=SimThreads())
        with open_workspace_env(root) as env:
            run = Run(env, Provider(env), blocking=runner, control=control)
            await run.execute(ensure_request())
            runner.boundary = runner.calls + boundary
            dispatch = asyncio.create_task(run.execute(run.dispatch()))
            await runner.arrived.wait(dispatch)
            control.land_park()
            runner.release.open()
            result = await dispatch
            if run.provider.turns:
                # Settlement follows an execution that already crossed its boundary.
                assert status(result) is not ObservationStatus.CANCELLED
            else:
                assert status(result) is ObservationStatus.CANCELLED
                assert run.lifecycle.events == []
                assert "pause drain" in result.observation.observation.diagnostic
                saved = run.journal.load_optional()
                assert saved is not None
                assert all(record.interrupted for record in saved.invocations.values())
                assert await run.execute(run.dispatch()) == result
            for host in env.hosts:
                await host.close()
