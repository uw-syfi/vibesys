"""No agent evaluation tool call can halt the run: the property, on the cheap layer.

The real shell (`CoreRuntime`, its loop, the in-memory store), the real core step and the
real `AgentEvaluationBridge` run a generated schedule of agent turns. Everything at the far
end is scripted: executors answer from facts, and the agent's workspace is a Fake that
snapshots to a revision per change. No Git, no Slurm, no sockets: the bridge is called with
the wire frames its socket would carry, so an example takes milliseconds and the property
can run hundreds of them. `test_agent_tool_calls_never_halt` keeps two end-to-end examples.

A generated program per turn mixes submits and waits (own, stale, repeated and unknown
handles). Around the turns, the schedule also makes calls after a turn ended (a stale token
that is still valid for the scope), restarts the host between a turn's calls and its yield
(a new bridge, so the old yield and token are gone), and calls after the run stopped.

Properties: the run reaches its terminal state without the loop halting; every call either
succeeds or is refused with a non-empty template text; a call after the run ended raises
only the shell's own commit error; every suspension core accepted is resumed exactly once.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import TypeAdapter
from tests.support.fake_run_clock import FakeRunClock
from tests.support.skeleton_strategy import ATTEMPT, DECLARATION, DIGEST
from tests.support.waiting_loop_strategy import LoopState, LoopStrategy
from tests.vibesys.orchestration.dynamic.strategy._executors import Executors
from tests.vibesys.orchestration.dynamic.strategy._shell import ScriptedExecutors

from vs_core.api import (
    ArtifactId,
    ArtifactRef,
    ClockAdvanced,
    CoreState,
    DispatchTurn,
    Limits,
    OperationRegistry,
    Request,
    ResourceId,
    ResumeSessionTurn,
    RevisionRef,
    RunFacts,
    RunStatus,
    Scope,
    SessionPhase,
    SubmitMeasurement,
    TurnObserved,
)
from vs_core.testing.drive import Answer, Running, Succeeded
from vs_evaluation.api import SocketFailure, SocketSuccess, SubmitCall, WaitCall
from vs_project.api import FakeStateStore
from vs_runtime.api.core import (
    AgentEvaluationBridge,
    AgentEvaluationPolicy,
    CoreRunHost,
    CoreRuntime,
    CoreRuntimeBindings,
    CoreStartup,
    ExecutionResult,
    OrphanWaitError,
    RequestExecutors,
    RunLoopConfig,
    RuntimeCommitError,
    RuntimeCommitUncertainError,
    drive_core,
    empty_catalog,
    handle_for,
    new_core_state,
    start_core,
)
from vs_runtime.api.testing import FakePublicationDelivery
from vs_runtime.contracts import AgentRole, AgentTool

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from vs_core.api import CoreEvent, Transition
    from vs_runtime.api.core import ExecutionContext, SnapshotWorkspace

from tests.support.skeleton_world import IMPLEMENTATION, Implementation

ROLE = AgentRole(
    id="implementer", system_prompt="implement", extra_tools=(AgentTool(id="evaluation"),)
)
POLICY = AgentEvaluationPolicy(
    evaluator_digest=DIGEST,
    workload_digest=DIGEST,
    environment_digest=DIGEST,
    recipe=ArtifactRef(artifact_id=ArtifactId(root="recipe"), digest=DIGEST),
    stages=(("accuracy", 10.0), ("benchmark", 10.0)),
    queue_allowance=880.0,
    accuracy_stage="accuracy",
)
FACTS = RunFacts(
    objective="make candidate.py faster",
    baseline=RevisionRef.of_git_commit("ab" * 20),
    evaluator_digest=DIGEST,
    workload_digest=DIGEST,
    environment_digest=DIGEST,
)
LEASE = 100.0
REPLY = TypeAdapter(SocketSuccess | SocketFailure)

type Step = tuple[Literal["submit"]] | tuple[Literal["wait"], tuple[int, ...]]
type Program = tuple[Step, ...]


@dataclass(frozen=True)
class Turn:
    """What the agent of one fresh turn does, and what the schedule does around it."""

    program: Program = ()
    late: Program = ()
    """Calls made with the turn's token after it ended, before the next request runs."""
    restart: bool = False
    """The host restarts after the turn's calls and before its yield is read."""
    at_start: Callable[[Agent], Awaitable[None]] | None = None
    """A probe that runs when the turn starts, before its program."""
    at_yield: Callable[[Agent, DispatchTurn], Awaitable[bool]] | None = None
    """A probe that runs after the turn's calls, where the bridge would be asked for its yield.

    It returns whether the turn's reply still says it is waiting."""


def _commit(label: str) -> str:
    """A 40-digit object name derived from ``label``."""
    return hashlib.sha256(label.encode()).hexdigest()[:40]


class Workspace:
    """A Fake agent workspace: its revision changes with each turn's edit, as Git content does.

    A snapshot is its own commit, so it never equals the commit the turn reports (the
    retention commit carries its own metadata); the same content gives the same snapshot.
    """

    def __init__(self, edits: list[str]) -> None:
        self._edits = edits

    async def snapshot_and_retain(self, label: str, *, retention_label: str) -> str:
        """The current content as a snapshot revision, the same one until the next edit."""
        del label, retention_label
        return _commit(f"snapshot:{self._edits[-1]}")

    async def matches_revision(self, revision: str) -> bool:
        """Whether nothing changed since the snapshot ``revision``."""
        return revision == _commit(f"snapshot:{self._edits[-1]}")


class Workspaces:
    """Every scope's workspace is one Fake; none is ever gone."""

    def __init__(self) -> None:
        self.edits = [_commit("edit:0")]

    def edit(self) -> None:
        """The agent changed its candidate."""
        self.edits.append(_commit(f"edit:{len(self.edits)}"))

    async def workspace_of(self, scope: Scope) -> SnapshotWorkspace | None:
        """The one workspace."""
        del scope
        return Workspace(self.edits)


class ShellView:
    """The shell as the bridge sees it, with an optional replacement for the state it shows.

    A faithful pass-through: ``admit`` and ``record`` are the real shell's. ``shown``
    stands for the committed state having moved since an earlier read, which a
    concurrent commit would do; it is None except in the one test that needs it.
    """

    def __init__(self, shell: CoreRuntime[LoopState]) -> None:
        self._shell = shell
        self.shown: CoreState | None = None
        self.admitting: CoreRuntime[LoopState] | None = None
        """Another shell to admit through, to stand for a halted or failing one."""
        self.admit_error: Exception | None = None

    @property
    def record(self) -> ShellView:
        """The committed record (this view answers ``envelope.core`` itself)."""
        return self

    @property
    def envelope(self) -> ShellView:
        """The committed envelope."""
        return self

    @property
    def core(self) -> CoreState:
        """The state the bridge reads."""
        return self.shown if self.shown is not None else self._shell.record.envelope.core

    def admit(self, event: CoreEvent, *, now_at: float) -> Transition:
        """The real shell's admission, unless a test made the shell fail."""
        if self.admit_error is not None:
            raise self.admit_error
        return (self.admitting or self._shell).admit(event, now_at=now_at)


def _submitted(request: SubmitMeasurement) -> Answer:
    """An executor that took a submission names its job by the handle core derived."""
    assert request.request_id is not None
    return Running(resource_id=ResourceId(root=handle_for(request.request_id)))


@dataclass
class Agent:
    """The scripted agent: runs a turn's program through the bridge's wire frames."""

    turns: tuple[Turn, ...]
    workspaces: Workspaces
    make_bridge: Callable[[], AgentEvaluationBridge]
    bridge: AgentEvaluationBridge | None = None
    handles: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    waits_accepted: int = 0
    seen: int = 0
    pending: tuple[tuple[Scope, Program], ...] = ()
    diagnostics: list[str] = field(default_factory=list)
    view: ShellView | None = None
    shell: CoreRuntime[LoopState] | None = None
    current: Turn | None = None
    run_over: bool = False
    """Set once the run is terminal: a call may then meet the shell's own commit error."""

    def token(self, scope: Scope) -> str:
        """The token the bridge's tool server hands the agent for ``scope``."""
        assert self.bridge is not None
        (descriptor,) = self.bridge.servers(ROLE, scope)
        return dict(descriptor.runtime_env)["VS_EVALUATION_TOKEN"]

    async def call(self, token: str, step: Step, *, count: bool = True) -> bool:
        """One tool call; True when it succeeded. A refusal must carry its template text."""
        assert self.bridge is not None
        match step:
            case ("submit",):
                frame = SubmitCall(token=token).model_dump_json().encode()
            case ("wait", picks):
                names = tuple(
                    dict.fromkeys(
                        self.handles[i] if i < len(self.handles) else f"unknown-{i}" for i in picks
                    )
                )
                frame = WaitCall(token=token, handles=names).model_dump_json().encode()
        try:
            reply = REPLY.validate_json(await self.bridge.handle(frame))
        except RuntimeCommitError:
            assert self.run_over, "only a call after the run ended meets the shell's commit error"
            return False
        if isinstance(reply, SocketFailure):
            assert reply.error
            self.errors.append(reply.error)
            return False
        if step[0] == "submit" and "handle_id" in reply.result:  # type: ignore[operator]
            self.handles.append(reply.result["handle_id"])  # type: ignore[index]
        elif count:
            self.waits_accepted += 1
        return True

    async def turn(self, scope: Scope) -> bool:
        """Run the next fresh turn's program; True when a wait was accepted."""
        index = self.seen
        self.seen += 1
        turn = self.turns[index] if index < len(self.turns) else Turn()
        self.current = turn
        self.workspaces.edit()
        token = self.token(scope)
        waiting = False
        if turn.at_start is not None:
            await turn.at_start(self)
        for step in turn.program:
            if await self.call(token, step) and step[0] == "wait":
                waiting = True
        self.pending = (*self.pending, (scope, turn.late))
        if turn.restart:
            # The yield is held in memory and is lost with the old bridge, so the turn ends
            # plainly and its measurement reaches the strategy as an ordinary result. The
            # scripted strategy keys on the reply, so the reply says what happened.
            self.bridge = self.make_bridge()
            return False
        return waiting

    async def late_calls(self) -> None:
        """Calls of turns that already ended, with the token those turns held."""
        pending, self.pending = self.pending, ()
        for scope, program in pending:
            token = self.token(scope)
            for step in program:
                await self.call(token, step, count=False)


class AgentExecutors(ScriptedExecutors):
    """The scripted executors, with the agent running inside each agent turn."""

    def __init__(self, agent: Agent, core: Callable[[], CoreState]) -> None:
        self._agent = agent
        self._evaluator = Executors(submit=_submitted)
        self._reply = ""
        super().__init__(self._answer, core, OperationRegistry(), {IMPLEMENTATION: Implementation})

    def _answer(self, request: Request, core: CoreState) -> Answer:
        if isinstance(request, DispatchTurn | ResumeSessionTurn):
            lease = ResourceId(root=f"lease:{request.turn.session.session_id.root}")
            return Succeeded(output_json=self._reply, resource_id=lease)
        return self._evaluator(request, core)

    async def execute(self, request: Request, context: ExecutionContext) -> ExecutionResult:
        """Late calls first; then, for a turn, the agent and its yield; else as scripted."""
        await self._agent.late_calls()
        if not isinstance(request, DispatchTurn | ResumeSessionTurn):
            return await super().execute(request, context)
        waiting = False
        if request.turn.charge_class != "resume":
            waiting = await self._agent.turn(request.scope)
            current = self._agent.current
            if current is not None and current.at_yield is not None:
                assert isinstance(request, DispatchTurn)
                waiting = await current.at_yield(self._agent, request)
        commit = self._agent.workspaces.edits[-1]
        self._reply = json.dumps({"commit": commit, "waiting": waiting})
        result = await super().execute(request, context)
        assert self._agent.bridge is not None
        suspension = (
            self._agent.bridge.yielded(request) if isinstance(request, DispatchTurn) else None
        )
        events = tuple(
            event.model_copy(update={"suspension": suspension})
            if isinstance(event, TurnObserved)
            else event
            for event in result.owner_events
        )
        return ExecutionResult(observation=result.observation, owner_events=events)


@dataclass
class Played:
    """One finished run."""

    shell: CoreRuntime[LoopState]
    agent: Agent
    view: ShellView


def play(turns: tuple[Turn, ...], total: int) -> Played:
    """Drive a fresh run, with ``turns`` as the agent's fresh turns, to its end."""
    limits = Limits(max_turns=4 * total + 4, max_measurement_submissions=64)
    state = new_core_state(
        "run",
        FACTS,
        DECLARATION,
        offered=empty_catalog(),
        startup=CoreStartup(deadline_at=1000.0, limits=limits),
    )
    store = FakeStateStore()
    clock = FakeRunClock(1.0)
    workspaces = Workspaces()
    shells: list[CoreRuntime[LoopState]] = []
    views: list[ShellView] = []
    diagnostics: list[str] = []

    def make_bridge() -> AgentEvaluationBridge:
        bridge = AgentEvaluationBridge(Path("unused.sock"), POLICY, workspaces, diagnostics.append)
        bridge.attach(views[0], clock)
        return bridge

    agent = Agent(turns, workspaces, make_bridge, diagnostics=diagnostics)
    executors = AgentExecutors(agent, lambda: shells[0].record.envelope.core)
    shell = cast(
        "CoreRuntime[LoopState]",
        CoreRuntime(
            store,
            LoopStrategy(total=total),  # type: ignore[arg-type]
            state,
            bindings=CoreRuntimeBindings(
                registry=OperationRegistry(),
                executors=RequestExecutors(
                    workspaces=executors,  # type: ignore[arg-type]  # one scripted object serves each role
                    sessions=executors,  # type: ignore[arg-type]
                    evaluation=executors,  # type: ignore[arg-type]
                    operations=executors,  # type: ignore[arg-type]
                    semantic_events=executors,  # type: ignore[arg-type]
                ),
            ),
        ),
    )
    shells.append(shell)
    views.append(ShellView(shell))
    agent.view, agent.shell = views[0], shell
    agent.bridge = make_bridge()
    host = CoreRunHost(shell, FakePublicationDelivery(store), clock)
    config = RunLoopConfig(host_id="property", lease_duration=LEASE, max_dispatches=400)
    start_core(host, config)
    asyncio.run(drive_core(host, config))
    return Played(shell, agent, views[0])


def check(played: Played) -> None:
    """The run ended terminally and every accepted suspension was resumed once."""
    state = played.shell.record.envelope.core
    strategy = played.shell.record.envelope.strategy
    assert isinstance(strategy, LoopState)
    assert state.run.status == RunStatus.TERMINAL
    assert strategy.suspensions == strategy.resumes
    assert strategy.suspensions <= played.agent.waits_accepted
    assert not [row for row in state.sessions.invocations if row.phase == SessionPhase.EXECUTING], (
        "no turn is left executing"
    )


def after_run(played: Played) -> None:
    """Calls once the run is over: typed replies, or only the shell's own commit error."""
    played.agent.run_over = True

    async def go() -> None:
        agent = played.agent
        scope = Scope(owner=ATTEMPT.attempt_id, generation=0)
        token = agent.token(scope)
        for step in (("submit",), ("wait", (0,))):
            await agent.call(token, step, count=False)
        await agent.late_calls()

    asyncio.run(go())


_STEPS = st.one_of(
    st.just(("submit",)),
    st.tuples(st.just("wait"), st.lists(st.integers(0, 4), min_size=1, max_size=3).map(tuple)),
)
_PROGRAM = st.lists(_STEPS, max_size=3).map(tuple)
_TURNS = st.lists(
    st.builds(Turn, program=_PROGRAM, late=_PROGRAM, restart=st.booleans()), min_size=1, max_size=3
).map(tuple)


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(turns=_TURNS, total=st.integers(1, 2))
def test_no_tool_call_sequence_halts_the_run(turns: tuple[Turn, ...], total: int) -> None:
    """Whatever the agents call, before, between and after their turns, the run goes on."""
    played = play(turns, total)
    check(played)
    after_run(played)
    assert all(isinstance(text, str) and text for text in played.agent.errors)


def _failed_calls(played: Played) -> list[str]:
    return played.agent.errors


async def _queue_input_then_submit(agent: Agent) -> None:
    """Admission needs an idle queue: with an input queued the shell says so, transiently."""
    assert agent.shell is not None
    now = agent.shell.record.envelope.core.run.now_at
    agent.shell.submit(ClockAdvanced(now_at=now), now_at=now)
    scope = Scope(owner=ATTEMPT.attempt_id, generation=0)
    assert not await agent.call(agent.token(scope), ("submit",), count=False)


def test_an_admission_refused_for_a_busy_queue_is_busy_and_is_journaled() -> None:
    """The one transient shell refusal tells the agent to call again, and leaves a trace."""
    played = play((Turn(at_start=_queue_input_then_submit),), 1)
    check(played)
    assert played.agent.errors == [
        "the run could not take this call right now; call the tool again"
    ]
    assert played.agent.diagnostics == [
        "agent evaluation call refused as busy: RuntimeCommitError: admission needs an idle input queue"
    ]


def _failing_admission(error: Exception | None) -> Callable[[Agent], Awaitable[None]]:
    """A probe whose submit meets ``error`` from the shell (an unstarted shell when None)."""

    async def probe(agent: Agent) -> None:
        assert agent.view is not None
        scope = Scope(owner=ATTEMPT.attempt_id, generation=0)
        frame = SubmitCall(token=agent.token(scope)).model_dump_json().encode()
        if error is None:
            idle = cast(
                "CoreRuntime[LoopState]",
                CoreRuntime(
                    FakeStateStore(),
                    LoopStrategy(total=1),
                    agent.view.core,  # type: ignore[arg-type]
                ),
            )
            agent.view.admitting = idle
        else:
            agent.view.admit_error = error
        try:
            assert agent.bridge is not None
            with pytest.raises(RuntimeCommitError) as raised:
                await agent.bridge.handle(frame)
            assert type(raised.value) is type(error or RuntimeCommitError())
        finally:
            agent.view.admitting = None
            agent.view.admit_error = None

    return probe


@pytest.mark.parametrize(
    "error",
    [
        None,
        RuntimeCommitUncertainError(candidate_visible=True),
        OrphanWaitError(()),
    ],
    ids=["halted-or-inactive-shell", "uncertain-commit", "orphan-wait"],
)
def test_a_shell_that_cannot_be_trusted_is_never_reported_to_the_agent_as_busy(
    error: Exception | None,
) -> None:
    """Only the idle-queue refusal is busy; every other shell failure reaches the halt."""
    played = play((Turn(at_start=_failing_admission(error)),), 1)
    check(played)
    assert played.agent.errors == []
    assert played.agent.diagnostics == []


async def _stop_the_run_before_the_yield(agent: Agent, request: DispatchTurn) -> bool:
    """The run stops taking suspensions between the wait and the end of the turn."""
    assert agent.view is not None
    assert agent.bridge is not None
    core = agent.view.core
    agent.view.shown = core.model_copy(
        update={"run": core.run.model_copy(update={"status": RunStatus.CLOSING})}
    )
    try:
        assert agent.bridge.yielded(request) is None, "the gate refuses at the end of the turn"
    finally:
        agent.view.shown = None
    # The turn is over for the bridge: the handles it waited on are no longer its own.
    scope = Scope(owner=ATTEMPT.attempt_id, generation=0)
    assert not await agent.call(agent.token(scope), ("wait", (0,)), count=False)
    assert "were not submitted by this agent in this turn" in agent.errors[-1]
    return False


def test_a_turn_whose_wait_is_refused_at_its_end_releases_the_handles_it_waited_on() -> None:
    """The gate fails when the turn ends: no yield, and the turn's handles are forgotten."""
    turn = Turn(program=(("submit",), ("wait", (0,))), at_yield=_stop_the_run_before_the_yield)
    played = play((turn,), 1)
    check(played)
    assert played.agent.waits_accepted == 1
    assert played.shell.record.envelope.core.evaluation.continuations == ()
