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
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

import pytest
from hypothesis import HealthCheck, assume, example, given, settings
from hypothesis import strategies as st
from pydantic import TypeAdapter
from tests.support.agent_tool_world import resumes, suspensions
from tests.support.dynamic_script import Executors
from tests.support.owed_submissions import owed_namespace
from tests.support.skeleton_strategy import ATTEMPT, DIGEST, OPERATIONS, SkeletonConfig, skeleton
from tests.support.synthesized_evidence import SynthesizedEvidence

from vs_core.api import (
    ArtifactId,
    ArtifactRef,
    ClockAdvanced,
    CoreState,
    DispatchTurn,
    EvidenceKind,
    ExecuteRegisteredOperation,
    RenderedArtifacts,
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
from vs_core.api.testing import sample_limits
from vs_core.testing.drive import Answer, Harness, Running, Succeeded, new_run
from vs_evaluation.api import SocketFailure, SocketSuccess, SubmitCall, WaitCall
from vs_project.api import FakeStateStore, NestedRepositoryError
from vs_runtime.api.core import (
    AgentEvaluationBridge,
    AgentEvaluationPolicy,
    AgentEvaluationRecords,
    CoreRunHost,
    CoreRuntime,
    CoreRuntimeBindings,
    ExecutionResult,
    OrphanWaitError,
    OwedSubmissions,
    ReceiptStore,
    RequestExecutors,
    RunLoopConfig,
    RunStalledError,
    RuntimeCommitError,
    RuntimeCommitUncertainError,
    drive_core,
    handle_for,
    start_core_awaiting_lease,
)
from vs_runtime.api.executor_contracts import FaultingNamespace, ProcessKilledError
from vs_runtime.api.testing import FakePublicationDelivery, ScriptedExecutors
from vs_runtime.contracts import AgentRole, AgentTool
from vs_sim.api import OsThreads, UnixNetwork, yield_now
from vs_sim.api.testing import VirtualClock, examples, run_virtual

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from vs_core.api import CoreEvent, Transition
    from vs_project.api import StateNamespace
    from vs_runtime.api.core import (
        ExecutionContext,
        Publication,
        PublicationAcknowledgement,
        PublicationContext,
        SnapshotWorkspace,
    )


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
    accuracy_gated=(EvidenceKind.CORRECTNESS, EvidenceKind.BENCHMARK),
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

type Step = (
    tuple[Literal["submit"]]
    | tuple[Literal["lose"]]
    | tuple[Literal["timeout_retry"]]
    | tuple[Literal["wait"], tuple[int, ...]]
)
type Program = tuple[Step, ...]


@dataclass(frozen=True)
class Turn:
    """What the agent of one fresh turn does, and what the schedule does around it."""

    program: Program = ()
    late: Program = ()
    """Calls made with the turn's token after it ended, before the next request runs."""
    restart: bool = False
    edit: bool = True
    """Whether the agent changes its candidate; otherwise the turn starts on the content the last left."""
    """The host restarts after the turn's calls and before its yield is read."""
    at_start: Callable[[Agent], Awaitable[None]] | None = None
    """A probe that runs when the turn starts, before its program."""
    at_yield: Callable[[Agent, DispatchTurn], Awaitable[bool]] | None = None
    """A probe that runs after the turn's calls, where the bridge would be asked for its yield.

    It returns whether the turn's reply still says it is waiting."""


def _commit(label: str) -> str:
    """A 40-digit object name derived from ``label``."""
    return hashlib.sha256(label.encode()).hexdigest()[:40]


class SlowSnapshot:
    """A snapshot that can be held, as one queued behind other work on a lock is.

    While held, a snapshot reports that it has arrived and waits for the release. The test
    decides when the release comes, so how long the submit takes against the agent's tool
    deadline is chosen by the schedule, never measured.
    """

    def __init__(self) -> None:
        self.hold: asyncio.Event | None = None
        self.arrived = asyncio.Event()

    async def pass_through(self) -> None:
        """Wait here while a hold is in place."""
        if self.hold is not None and not self.hold.is_set():
            self.arrived.set()
            await self.hold.wait()


class Workspace:
    """A Fake agent workspace: its revision changes with each turn's edit, as Git content does.

    A snapshot is its own commit, so it never equals the commit the turn reports (the
    retention commit carries its own metadata); the same content gives the same snapshot.
    """

    def __init__(self, edits: list[str], slow: SlowSnapshot, nested: tuple[str, ...]) -> None:
        self._edits = edits
        self._slow = slow
        self._nested = nested

    async def snapshot_and_retain(self, label: str, *, retention_label: str) -> str:
        """The current content as a snapshot revision, the same one until the next edit."""
        del label, retention_label
        await self._slow.pass_through()
        self._refuse_nested()
        return _commit(f"snapshot:{self._edits[-1]}")

    async def matches_revision(self, revision: str) -> bool:
        """Whether nothing changed since the snapshot ``revision``."""
        self._refuse_nested()
        return revision == _commit(f"snapshot:{self._edits[-1]}")

    def _refuse_nested(self) -> None:
        """Refuse as host Git does while the agent keeps a repository in its workspace."""
        if self._nested:
            raise NestedRepositoryError(self._nested)


class Workspaces:
    """Every scope's workspace is one Fake; none is ever gone."""

    def __init__(self) -> None:
        self.edits = [_commit("edit:0")]
        self.slow = SlowSnapshot()
        self.nested: tuple[str, ...] = ()
        """The ``.git`` paths the agent left below its workspace root."""

    def edit(self) -> None:
        """The agent changed its candidate."""
        self.edits.append(_commit(f"edit:{len(self.edits)}"))

    async def workspace_of(self, scope: Scope) -> SnapshotWorkspace | None:
        """The one workspace."""
        del scope
        return Workspace(self.edits, self.slow, self.nested)


class ShellView:
    """The shell as the bridge sees it, with an optional replacement for the state it shows.

    A faithful pass-through: ``admit`` and ``record`` are the real shell's. ``shown``
    stands for the committed state having moved since an earlier read, which a
    concurrent commit would do; it is None except in the one test that needs it.
    """

    def __init__(self, shell: CoreRuntime[Any]) -> None:
        self._shell = shell
        self.shown: CoreState | None = None
        self.admitting: CoreRuntime[Any] | None = None
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

    @property
    def admitted_core(self) -> CoreState:
        """The state the bridge reads, with the shell's admitted inputs."""
        return self.shown if self.shown is not None else self._shell.admitted_core

    def admit(self, event: CoreEvent, *, now_at: float) -> Transition:
        """The real shell's admission, unless a test made the shell fail."""
        if self.admit_error is not None:
            raise self.admit_error
        return (self.admitting or self._shell).admit(event, now_at=now_at)


def _submitted(request: SubmitMeasurement) -> Answer:
    """An executor that took a submission names its job by the handle core derived."""
    assert request.request_id is not None
    return Running(resource_id=ResourceId(root=handle_for(request.request_id)))


@dataclass(frozen=True)
class Crash:
    """How one host process ends.

    ``ledger_at`` is a boundary of the owed record's writes (see ``FaultingNamespace``: an
    even number is just before a write, an odd one just after it). The host dies there, in
    the middle of an agent's call; the agent's later calls fail until a new host starts, and
    the dead host's loop is stopped at its next publication, so what it had admitted and not
    yet committed may or may not reach the record. ``publish_at`` is the n-th publication of
    the process, where it dies between requests, or (``mid_request``) at that publication
    even while requests run. Either, both or neither may be set.
    """

    ledger_at: int | None = None
    publish_at: int | None = None
    mid_request: bool = False


@dataclass
class Mortality:
    """Whether the current host process is dead, and the publication it dies at."""

    dead: bool = False
    publish_at: int | None = None
    published: int = 0


class MortalDelivery:
    """The publication ledger of a host that can die at a publication or once marked dead.

    The loop may deliver a publication while requests run, so the host dies at the first
    publication from the ``publish_at``-th on that ``idle`` allows: one that finds none
    running (a death between requests, which the scripted executors' answers can recover
    from), or any, for a crash drawn ``mid_request``.
    """

    def __init__(
        self, inner: FakePublicationDelivery, mortality: Mortality, idle: Callable[[], bool]
    ) -> None:
        self._inner = inner
        self._mortality = mortality
        self._idle = idle

    def read(self) -> tuple[Publication, ...]:
        """The acknowledged publication history."""
        return self._inner.read()

    async def publish(
        self, publication: Publication, context: PublicationContext
    ) -> PublicationAcknowledgement:
        """Publish, unless the process is dead or dies at this publication."""
        self._mortality.published += 1
        due = self._mortality.publish_at
        if self._mortality.dead or (
            due is not None and self._mortality.published >= due and self._idle()
        ):
            raise ProcessKilledError
        return await self._inner.publish(publication, context)


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
    shell: CoreRuntime[Any] | None = None
    current: Turn | None = None
    mortality: Mortality = field(default_factory=Mortality)
    read: list[tuple[str, str]] = field(default_factory=list)
    """(candidate content, handle) for every handle reply the agent read."""
    budget_refused: list[str] = field(default_factory=list)
    """The candidate content of every submit that core's budget refused."""
    run_over: bool = False
    """Set once the run is terminal: a call may then meet the shell's own commit error."""

    def token(self, scope: Scope) -> str:
        """The token the bridge's tool server hands the agent for ``scope``."""
        assert self.bridge is not None
        (descriptor,) = self.bridge.servers(ROLE, scope)
        return dict(descriptor.runtime_env)["VS_EVALUATION_TOKEN"]

    async def call(self, token: str, step: Step, *, count: bool = True) -> bool:
        """One tool call; True when it succeeded. A refusal must carry its template text.

        A call that meets the host's death fails as a dropped connection does, and so does
        every call until the next host starts."""
        if self.mortality.dead:
            return False
        try:
            return await self._call(token, step, count=count)
        except ProcessKilledError:
            self.mortality.dead = True
            return False

    async def _call(self, token: str, step: Step, *, count: bool = True) -> bool:
        """One tool call on a live host."""
        assert self.bridge is not None
        match step:
            case ("submit",):
                frame = SubmitCall(token=token).model_dump_json().encode()
            case ("lose",):
                # The call completes on the host; the agent's deadline passed, so it never
                # reads the reply and the client never confirms it.
                await self.bridge.answer(SubmitCall(token=token).model_dump_json().encode())
                return True
            case ("timeout_retry",):
                frame = SubmitCall(token=token).model_dump_json().encode()
                return await self._read_and_confirm(
                    step, await self._after_timeout(frame), count=count
                )
            case ("wait", picks):
                names = tuple(
                    dict.fromkeys(
                        self.handles[i] if i < len(self.handles) else f"unknown-{i}" for i in picks
                    )
                )
                frame = WaitCall(token=token, handles=names).model_dump_json().encode()
        try:
            return await self._read_and_confirm(step, await self.bridge.answer(frame), count=count)
        except RuntimeCommitError:
            assert self.run_over, "only a call after the run ended meets the shell's commit error"
            return False

    async def _read_and_confirm(
        self, step: Step, answered: tuple[bytes, Callable[[], None]], *, count: bool
    ) -> bool:
        """The agent reads the reply, and only then does its client confirm the receipt.

        A confirmation made before the reply is read (as the bridge once did in a ``handle``
        shortcut) would let a host that dies right after it leave the reply unread with the
        owed record already cleared. That state cannot occur: the transport asks for the
        clearing only after the client has read the whole reply."""
        reply, confirm = answered
        ok = await self._record(step, reply, count=count)
        confirm()
        return ok

    async def _after_timeout(self, frame: bytes) -> tuple[bytes, Callable[[], None]]:
        """A submit that outlasts the agent's deadline, then the agent's retry.

        The first call is held in its snapshot while the agent gives up (nobody reads its
        reply), the retry arrives and queues behind it, and the snapshot then finishes.
        """
        assert self.bridge is not None
        slow = self.workspaces.slow
        slow.hold, slow.arrived = asyncio.Event(), asyncio.Event()
        first = asyncio.create_task(self.bridge.answer(frame))
        arrived = asyncio.create_task(slow.arrived.wait())
        await asyncio.wait({first, arrived}, return_when=asyncio.FIRST_COMPLETED)
        retry = asyncio.create_task(self.bridge.answer(frame))
        await yield_now()
        slow.hold.set()
        arrived.cancel()
        _, answered = await asyncio.gather(first, retry, return_exceptions=True)
        if isinstance(answered, BaseException):
            raise answered
        return answered

    async def _record(self, step: Step, raw: bytes, *, count: bool) -> bool:
        """File one reply: an error with its text, a new handle, or an accepted wait."""
        reply = REPLY.validate_json(raw)
        if isinstance(reply, SocketFailure):
            assert reply.error
            self.errors.append(reply.error)
            if step[0] in ("submit", "timeout_retry") and "submission budget" in reply.error:
                self.budget_refused.append(self.workspaces.edits[-1])
            return False
        if step[0] in ("submit", "timeout_retry") and "handle_id" in reply.result:  # type: ignore[operator]
            self.handles.append(reply.result["handle_id"])  # type: ignore[index]
            self.read.append((self.workspaces.edits[-1], reply.result["handle_id"]))  # type: ignore[index]
        elif count:
            self.waits_accepted += 1
        return True

    async def turn(self, scope: Scope) -> bool:
        """Run the next fresh turn's program; True when a wait was accepted."""
        index = self.seen
        self.seen += 1
        turn = self.turns[index] if index < len(self.turns) else Turn()
        self.current = turn
        if turn.edit:
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
        """Calls of turns that already ended, with the token those turns held.

        A token names a scope, not a turn, so a late wait the bridge accepts joins the turn
        that is executing in that scope; it counts as an accepted wait.
        """
        pending, self.pending = self.pending, ()
        for scope, program in pending:
            token = self.token(scope)
            for step in program:
                await self.call(token, step)


class AgentExecutors(ScriptedExecutors):
    """The scripted executors, with the agent running inside each agent turn."""

    def __init__(
        self,
        agent: Agent,
        core: Callable[[], CoreState],
        *,
        finished_in_submit_round: bool = False,
    ) -> None:
        self._agent = agent
        self._finished_in_submit_round = finished_in_submit_round
        self._evaluator = Executors(submit=_submitted)
        self._reply = ""
        super().__init__(self._answer, core, OPERATIONS.registry(), skeleton.reply_schemas)

    def _answer(self, request: Request, core: CoreState) -> Answer:
        if isinstance(request, DispatchTurn | ResumeSessionTurn):
            lease = ResourceId(root=f"lease:{request.turn.session.session_id.root}")
            return Succeeded(output_json=self._reply, resource_id=lease)
        if isinstance(request, ExecuteRegisteredOperation):
            # The skeleton asks for nothing but its prompt to be rendered.
            return Succeeded(
                outcome=RenderedArtifacts(
                    status="succeeded",
                    prompts=(
                        ArtifactRef(
                            artifact_id=ArtifactId(root=f"prompt:{request.operation_id.root}"),
                            digest="prompt",
                        ),
                    ),
                )
            )
        if (
            self._finished_in_submit_round
            and isinstance(request, SubmitMeasurement)
            and request.plan.purpose == "local-validation"
        ):
            return self._evaluator.finished_agent_evaluation(request)
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

    shell: CoreRuntime[Any]
    agent: Agent
    view: ShellView
    directory: tempfile.TemporaryDirectory[str] | None = None


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Project state is written below the test's own directory."""
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path / "operator-state"))


class UnrecoverableRestartError(Exception):
    """The host died with a request in flight, and the scripted executors cannot inspect it.

    Recovering such a request is core's and the executors' (their contract suites cover it);
    the scripts here answer only requests they are asked to run, never an inspection."""


def play(
    turns: tuple[Turn, ...],
    total: int,
    *,
    finished_in_submit_round: bool = False,
    crashes: tuple[Crash, ...] = (),
) -> Played:
    """Drive a fresh run (see ``_play``) with owed submissions kept in a temporary project."""
    directory = tempfile.TemporaryDirectory()
    played = _play(
        turns,
        total,
        namespace=owed_namespace(Path(directory.name)),
        finished_in_submit_round=finished_in_submit_round,
        crashes=crashes,
    )
    played.directory = directory  # removed when the run is dropped
    return played


def _play(
    turns: tuple[Turn, ...],
    total: int,
    *,
    namespace: StateNamespace,
    finished_in_submit_round: bool = False,
    crashes: tuple[Crash, ...] = (),
) -> Played:
    """Drive a fresh run, with ``turns`` as the agent's fresh turns, to its end.

    With ``finished_in_submit_round`` the executor answers each of the agent's own submissions as
    already finished, with its evidence, as a verdict-cache replay does; otherwise the job is
    still running. Each of ``crashes`` ends one host process; the next one starts over the same
    durable run record and the same owed record, as after a restart."""
    limits = sample_limits(max_turns=4 * total + 4)
    selected = SkeletonConfig(turns=total)
    state = new_run(
        skeleton.driver(selected, OPERATIONS.registry()),
        Harness(
            registry=OPERATIONS.registry(),
            facts=FACTS,
            lifecycle=frozenset({"suspend"}),
            limits=limits,
            deadline_at=1000.0,
        ),
    )
    store = FakeStateStore()
    clock = VirtualClock(1.0)
    workspaces = Workspaces()
    shells: list[CoreRuntime[Any]] = []
    views: list[ShellView] = []
    ledgers: list[OwedSubmissions] = []
    diagnostics: list[str] = []

    def make_bridge() -> AgentEvaluationBridge:
        bridge = AgentEvaluationBridge(
            Path("unused.sock"),
            POLICY,
            workspaces,
            AgentEvaluationRecords(ledgers[-1], SynthesizedEvidence()),
            diagnostics.append,
            network=UnixNetwork(),
            threads=OsThreads(),
        )
        bridge.attach(views[-1], clock)
        return bridge

    delivery = FakePublicationDelivery()  # the subscribers outlive the host
    mortality = Mortality()
    agent = Agent(turns, workspaces, make_bridge, diagnostics=diagnostics, mortality=mortality)
    executors = AgentExecutors(
        agent,
        lambda: shells[-1].record.envelope.core,
        finished_in_submit_round=finished_in_submit_round,
    )

    def boot(crash: Crash) -> CoreRunHost:
        """Start a host process over the durable record, dying where ``crash`` says."""
        shell = cast(
            "CoreRuntime[Any]",
            CoreRuntime(
                store,
                # A driver holds its run's live flow: each host process starts its own.
                skeleton.driver(selected, OPERATIONS.registry()),
                state,
                config=selected,
                bindings=CoreRuntimeBindings(
                    registry=OPERATIONS.registry(),
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
        ledgers.append(
            OwedSubmissions(
                ReceiptStore(
                    cast("StateNamespace", FaultingNamespace(namespace, crash.ledger_at)),
                    threads=OsThreads(),
                )
            )
        )
        agent.view, agent.shell = views[-1], shell
        agent.bridge = make_bridge()
        mortality.dead, mortality.published, mortality.publish_at = False, 0, crash.publish_at
        return CoreRunHost(
            shell,
            MortalDelivery(delivery, mortality, lambda: crash.mid_request or not shell.in_flight),
            clock,
        )

    config = RunLoopConfig(host_id="property", lease_duration=LEASE, max_dispatches=400)
    died = False
    for crash in (*crashes, Crash()):
        host = boot(crash)

        async def run(host: CoreRunHost = host) -> None:
            await start_core_awaiting_lease(host, config)
            await drive_core(host, config)

        try:
            run_virtual(clock, run())
        except ProcessKilledError:
            died = True
            continue
        except (RunStalledError, OrphanWaitError) as error:
            # A host that died with a request in flight leaves one the scripts cannot inspect:
            # core blocks it, and either stalls on it or finds a job nothing will end.
            if died:
                raise UnrecoverableRestartError from error
            raise
        break
    return Played(shells[-1], agent, views[-1])


def check(played: Played) -> None:
    """The run ended terminally and every accepted suspension was resumed once."""
    state = played.shell.record.envelope.core
    assert state.run.status == RunStatus.TERMINAL
    assert suspensions(state) == resumes(state)
    assert suspensions(state) <= played.agent.waits_accepted
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

    run_virtual(VirtualClock(), go())


_STEPS = st.one_of(
    st.just(("submit",)),
    st.tuples(st.just("wait"), st.lists(st.integers(0, 4), min_size=1, max_size=3).map(tuple)),
)
_PROGRAM = st.lists(_STEPS, max_size=3).map(tuple)
_TURNS = st.lists(
    st.builds(Turn, program=_PROGRAM, late=_PROGRAM, restart=st.booleans(), edit=st.booleans()),
    min_size=1,
    max_size=3,
).map(tuple)


# A pull request draws a small sample (the pinned examples and regressions below carry the
# regression power); the nightly depth draws the full count. An example costs 0.2 to 0.4 s (the
# core validates the whole run record at every step), so the full count is a half-minute run.
_EXAMPLES = examples(pr=6, nightly=200)


@settings(
    max_examples=_EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(turns=_TURNS, total=st.integers(1, 2), finished_in_submit_round=st.booleans())
def test_no_tool_call_sequence_halts_the_run(
    turns: tuple[Turn, ...], total: int, *, finished_in_submit_round: bool
) -> None:
    """Whatever the agents call, before, between and after their turns, the run goes on.

    A submission's job may still be running or already finished when the executor accepts it."""
    played = play(turns, total, finished_in_submit_round=finished_in_submit_round)
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
        "agent evaluation call refused as busy: AdmissionBusyError: admission needs an idle input queue"
    ]


async def _nest_a_repository_then_submit(agent: Agent) -> None:
    """The agent clones a repository into its workspace and submits; then it removes it."""
    agent.workspaces.nested = ("vendor/lib/.git",)
    scope = Scope(owner=ATTEMPT.attempt_id, generation=0)
    assert not await agent.call(agent.token(scope), ("submit",), count=False)
    agent.workspaces.nested = ()


def test_a_submit_from_a_workspace_holding_a_nested_repository_is_refused_not_halted() -> None:
    """Host Git refuses the snapshot; the agent is told how to fix it and the run goes on."""
    played = play((Turn(at_start=_nest_a_repository_then_submit),), 1)
    check(played)
    assert len(played.agent.errors) == 1
    assert "nested Git repository" in played.agent.errors[0]


def _failing_admission(error: Exception | None) -> Callable[[Agent], Awaitable[None]]:
    """A probe whose submit meets ``error`` from the shell (an unstarted shell when None)."""

    async def probe(agent: Agent) -> None:
        assert agent.view is not None
        scope = Scope(owner=ATTEMPT.attempt_id, generation=0)
        frame = SubmitCall(token=agent.token(scope)).model_dump_json().encode()
        if error is None:
            idle = cast(
                "CoreRuntime[Any]",
                CoreRuntime(
                    FakeStateStore(),
                    skeleton.driver(SkeletonConfig(), OPERATIONS.registry()),
                    agent.view.core,  # type: ignore[arg-type]
                    config=SkeletonConfig(),
                ),
            )
            agent.view.admitting = idle
        else:
            agent.view.admit_error = error
        try:
            assert agent.bridge is not None
            with pytest.raises(RuntimeCommitError) as raised:
                await agent.bridge.answer(frame)
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
        RuntimeCommitError("admission needs an idle input queue"),
    ],
    ids=["halted-or-inactive-shell", "uncertain-commit", "orphan-wait", "busy-lookalike"],
)
def test_a_shell_that_cannot_be_trusted_is_never_reported_to_the_agent_as_busy(
    error: Exception | None,
) -> None:
    """Only ``AdmissionBusyError`` is busy, whatever the text; every other failure reaches the halt."""
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


@pytest.mark.parametrize(
    "turns",
    [
        (Turn(late=(("submit",), ("wait", (0,)))),),
        (Turn(late=(("submit",),)), Turn(program=(("submit",), ("wait", (0,))))),
    ],
    ids=["late-submit-then-wait", "late-submit-then-next-turn-waits"],
)
@pytest.mark.parametrize("total", [1, 2])
def test_a_submission_still_running_at_a_resume_does_not_halt_the_run(
    turns: tuple[Turn, ...], total: int
) -> None:
    """A resume is never authorized over an attempt history that a running submission leaves open."""
    played = play(turns, total)
    check(played)


@pytest.mark.parametrize(
    "turns",
    [
        (Turn(program=(("submit",), ("wait", (0,)))),),
        (Turn(late=(("submit",), ("wait", (0,)))),),
        (Turn(late=(("submit",),)), Turn(program=(("submit",), ("wait", (0,))))),
    ],
    ids=["submit-then-wait", "late-submit-then-wait", "late-submit-then-next-turn-waits"],
)
@pytest.mark.parametrize("total", [1, 2])
def test_a_submission_finished_in_its_submit_round_does_not_halt_the_run(
    turns: tuple[Turn, ...], total: int
) -> None:
    """An executor that answers a submission as finished wakes the turns that wait on it."""
    check(play(turns, total, finished_in_submit_round=True))


_LOSSY_STEP = st.sampled_from((("submit",), ("lose",), ("timeout_retry",)))


@settings(
    max_examples=_EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(program=st.lists(_LOSSY_STEP, min_size=1, max_size=4).map(tuple))
def test_a_repeated_submit_is_answered_with_the_one_evaluation_whatever_the_latency(
    program: Program,
) -> None:
    """However many times the agent asks, lost replies and retries included, it gets one handle.

    The candidate is charged once, by the first call that reaches the host. Every later call
    for it (a retry of a lost reply, or a repeat after the agent read the handle) is answered
    with that submission and refused never. A submission whose last reply was lost is listed
    as owed, so a stalled run can name it.
    """
    played = play((Turn(program=program),), 1)
    check(played)
    state = played.shell.record.envelope.core
    admitted = {c.request_id for c in state.evaluation.agent_calls if c.request_id is not None}
    assert len(admitted) == 1
    (request,) = admitted
    assert played.agent.errors == []
    assert set(played.agent.handles) <= {handle_for(request)}
    assert len(played.agent.handles) == sum(kind != "lose" for kind, *_ in program)
    assert played.agent.bridge is not None
    notes = played.agent.bridge.unacknowledged()
    assert len(notes) == (1 if program[-1][0] == "lose" else 0)
    assert all(handle_for(request) in note for note in notes)


def test_a_submit_slower_than_the_agents_deadline_is_one_submission_the_agent_can_wait_on() -> None:
    """The failing live run: the first call outlasts the deadline, the retry must get its handle.

    Before the fix the retry was refused as an exhausted budget while the lost call's
    measurement ran unseen, and the run ended stalled with ``open intents []``.
    """
    turn = Turn(program=(("timeout_retry",), ("wait", (0,))))
    played = play((turn,), 1)
    check(played)
    state = played.shell.record.envelope.core
    assert played.agent.errors == []
    assert len(played.agent.handles) == 1
    assert [c.request_id for c in state.evaluation.agent_calls if c.request_id is not None] != []
    assert played.agent.waits_accepted == 1, "the agent could wait on the submission it retried"


_RESTART_STEPS = st.one_of(
    st.sampled_from((("submit",), ("lose",), ("timeout_retry",))),
    st.tuples(st.just("wait"), st.lists(st.integers(0, 2), min_size=1, max_size=2).map(tuple)),
)
_CRASH = st.builds(
    Crash,
    ledger_at=st.none() | st.integers(0, 10),
    publish_at=st.none() | st.integers(1, 12),
    # A death with a request running ends loudly (UnrecoverableRestartError) or recovers.
    mid_request=st.booleans(),
)


def check_owed(played: Played) -> None:
    """What every run owes the agent, whatever restarts it went through.

    The budget refuses a second submission of the same candidate only after the agent has
    read the first one's handle; every handle read is one core charged, and one candidate has
    one handle; each charged submission the agent never read is in the stall report.
    """
    agent = played.agent
    state = played.shell.record.envelope.core
    charged = {
        handle_for(call.request_id)
        for call in state.evaluation.agent_calls
        if call.request_id is not None
    }
    read_contents = {content for content, _ in agent.read}
    assert set(agent.budget_refused) <= read_contents, "refused a retry whose handle was never read"
    assert {handle for _, handle in agent.read} <= charged
    for content in read_contents:
        assert len({handle for seen, handle in agent.read if seen == content}) == 1
    assert agent.bridge is not None
    noted = agent.bridge.unacknowledged()
    unread = charged - {handle for _, handle in agent.read}
    assert all(any(handle in note for handle in charged) for note in noted)
    assert all(any(handle in note for note in noted) for handle in unread)


@settings(
    max_examples=_EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@example(
    # A host that dies right after the owed record is cleared: the clearing must follow the
    # agent's read of the reply, so this kill leaves the submission owed and reported.
    programs=[(("submit",),)],
    edits=[False, False, False],
    crashes=(Crash(ledger_at=3, publish_at=None),),
)
@given(
    programs=st.lists(st.lists(_RESTART_STEPS, max_size=4).map(tuple), min_size=1, max_size=3),
    edits=st.lists(st.booleans(), min_size=3, max_size=3),
    crashes=st.lists(_CRASH, max_size=2).map(tuple),
)
def test_no_restart_loses_or_repeats_a_charged_submission(
    programs: list[Program], edits: list[bool], crashes: tuple[Crash, ...]
) -> None:
    """Hosts die at any request and at any write of the owed record; the promise to the agent holds.

    The agent's calls are lost, retried, repeated across turns that may leave the candidate
    unchanged, and cut by the host's death. After each death a new host starts over the same
    run record and the same owed record."""
    turns = tuple(
        Turn(program=program, edit=edit) for program, edit in zip(programs, edits, strict=False)
    )
    try:
        played = play(turns, len(turns), crashes=crashes)
    except UnrecoverableRestartError:
        assume(condition=False)
    check_owed(played)


@pytest.mark.parametrize("dies_at_request", range(1, 7))
def test_a_submission_the_agent_never_read_is_its_retry_after_a_host_restart(
    dies_at_request: int,
) -> None:
    """The failing live shape: a lost reply, a restart, then the retry on unchanged content.

    The first turn's reply is lost (the connection was abandoned) and the host dies at some
    later request, with the charge committed or not depending on where. The second turn finds
    the candidate as it was and submits again. Before the fix the owed submission lived in the
    first host's memory: the retry was refused as an exhausted budget and the first measurement
    ran on with nobody waiting for it.
    """
    turns = (Turn(program=(("lose",),)), Turn(program=(("submit",),), edit=False))
    played = play(turns, 2, crashes=(Crash(publish_at=dies_at_request),))
    check_owed(played)
    state = played.shell.record.envelope.core
    charged = [c.request_id for c in state.evaluation.agent_calls if c.request_id is not None]
    assert len(charged) == 1, "the budget is charged once"
    assert played.agent.errors == []
    assert played.agent.handles == [handle_for(charged[0])], "the retry got the first handle"
    jobs = [
        job.resource_id.root
        for job in state.evaluation.jobs
        if job.scope.owner == ATTEMPT.attempt_id
    ]
    assert jobs.count(handle_for(charged[0])) == 1, "the first measurement is the one that runs"
    assert played.agent.bridge is not None
    assert played.agent.bridge.unacknowledged() == ()


_ROLE_STEP = st.sampled_from((("submit",), ("lose",), ("timeout_retry",)))


@settings(
    max_examples=_EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(
    programs=st.lists(st.lists(_ROLE_STEP, max_size=3).map(tuple), min_size=1, max_size=4),
    edits=st.lists(st.booleans(), min_size=4, max_size=4),
    crashes=st.lists(_CRASH, max_size=2).map(tuple),
)
def test_roles_sharing_a_scope_get_one_evaluation_for_each_candidate(
    programs: list[Program], edits: list[bool], crashes: tuple[Crash, ...]
) -> None:
    """Successive roles of one workstream ask for the same and for new candidates, across restarts.

    Each turn stands for a role that holds the attempt's scope (the implementer, then the
    judge reviewing its candidate, then another turn). A turn that leaves the candidate as
    it was asks for an evaluation that exists, and a turn that edits asks for a new one.
    Without a restart, each distinct candidate is charged once and every ask for it, from
    any turn, is answered with that handle and never refused. With restarts, no ask is
    refused for budget either, and a candidate's handle never differs between reads.
    """
    turns = tuple(
        Turn(program=program, edit=edit) for program, edit in zip(programs, edits, strict=False)
    )
    try:
        played = play(turns, len(turns), crashes=crashes)
    except UnrecoverableRestartError:
        assume(condition=False)
    check_owed(played)
    agent = played.agent
    assert agent.budget_refused == []
    assert not [text for text in agent.errors if "budget" in text]
    if crashes:
        return
    contents = 0
    asked: set[int] = set()
    for turn in turns:
        contents += turn.edit
        if turn.program:
            asked.add(contents)
    state = played.shell.record.envelope.core
    charged = {c.request_id for c in state.evaluation.agent_calls if not c.repeat and c.request_id}
    assert len(charged) == len(asked)
    assert {handle for _, handle in agent.read} <= {handle_for(r) for r in charged}
