"""A run whose agent calls the evaluation tools in its turns: a vs-dst world on the step loop.

The production step loop hosts the skeleton search. The far side answers every request from
scripts, except the agent's turns: in each fresh turn a scripted agent calls the real
`AgentEvaluationBridge` with the wire frames its socket would carry (no sockets: the bridge's
own `answer`), through the host's `CoreInputs`, as the production host's bridge is attached. The
agent's workspace is a Fake that snapshots to a revision per change. A turn that ends waiting
yields its continuation through the bridge, as the session executor asks it.

The bridge is the host's memory (`Serving`): every host builds its own, over the one durable
owed-submission record on disk, so a host killed between a charge and its acknowledgement leaves
exactly what the production host's death leaves. The agent's own memory (which handles it read)
is the world's position.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import TypeAdapter
from tests.support.dynamic_script import Executors
from tests.support.owed_submissions import owed_namespace
from tests.support.skeleton_strategy import (
    ATTEMPT,
    DIGEST,
    OPERATIONS,
    SkeletonConfig,
    skeleton,
)
from tests.support.synthesized_evidence import SynthesizedEvidence

from vs_core.api import (
    ArtifactId,
    ArtifactRef,
    Continuation,
    ContinuationId,
    ContinuationPhase,
    DispatchTurn,
    EnsureWorkspace,
    EvidenceKind,
    ExecuteRegisteredOperation,
    InvocationId,
    InvocationRef,
    Reading,
    RenderedArtifacts,
    ResourceId,
    ResumeSessionTurn,
    RevisionRef,
    RunFacts,
    RunStatus,
    Scope,
    SubmitMeasurement,
    TaskObjective,
    TaskStage,
    TaskTerms,
)
from vs_core.api.testing import Harness, Running, Succeeded, sample_limits, sample_retries
from vs_dst.api import RunSetup
from vs_evaluation.api import SocketFailure, SocketSuccess, SubmitCall, WaitCall
from vs_project.api import NestedRepositoryError
from vs_runtime.api.core import (
    AgentEvaluationBridge,
    AgentEvaluationPolicy,
    AgentEvaluationRecords,
    OwedSubmissions,
    ReceiptStore,
    handle_for,
)
from vs_runtime.api.testing import Launch
from vs_runtime.contracts import AgentRole, AgentTool
from vs_sim.api import yield_now
from vs_sim.api.testing import SimNetwork, loop_threads

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_core.api import CoreEvent, CoreState, Request, Transition
    from vs_core.api.testing import Answer
    from vs_dst.api import Admission
    from vs_runtime.api.core import SnapshotWorkspace
    from vs_sim.api import Clock

ROLE = AgentRole(
    id="implementer", system_prompt="implement", extra_tools=(AgentTool(id="evaluation"),)
)
PROCESS_DIGEST = "cd" * 32
"""The environment identity a resumed process computes afresh, which is not the run's own."""
POLICY = AgentEvaluationPolicy(
    evaluator_digest=DIGEST,
    workload_digest=DIGEST,
    environment_digest=PROCESS_DIGEST,
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
SCOPE = Scope(owner=ATTEMPT.attempt_id, generation=0)
"""The scope of the skeleton's one attempt, which every turn of its implementer works in."""
BASE_INPUT = "input:base"
"""What the evaluation reads at the revision every attempt's workspace opens on."""
BASE_READING = 100.0
CANDIDATE_READING = 125.0
"""What a measured world's base and any candidate read: a candidate is 25% over its base."""
BASE_STALENESS_S = 60.0
"""Seconds after which the base's sample no longer counts on the run clock, in a measured world."""
TASK = TaskTerms(
    recipe=ArtifactRef(artifact_id=ArtifactId(root="recipe"), digest=DIGEST),
    stages=(
        TaskStage(stage_id="accuracy", execution_seconds=10.0),
        TaskStage(stage_id="benchmark", execution_seconds=10.0),
    ),
    measured=("accuracy", "benchmark"),
    queue_allowance=880.0,
    accuracy_stage="accuracy",
    benchmark_stage="benchmark",
    accuracy_gated=(EvidenceKind.CORRECTNESS, EvidenceKind.BENCHMARK),
    operation_seconds=10.0,
    objectives=(TaskObjective(name="throughput", direction="max"),),
    base_staleness=BASE_STALENESS_S,
)
MEASURED_FACTS = FACTS.model_copy(update={"task": TASK})
"""The run's facts when its measurements carry readings: the task compares a candidate with its base."""
REPLY = TypeAdapter(SocketSuccess | SocketFailure)

type Step = (
    tuple[Literal["submit"]]
    | tuple[Literal["lose"]]
    | tuple[Literal["timeout_retry"]]
    | tuple[Literal["wait"], tuple[int, ...]]
    | tuple[Literal["forged"], Literal["foreign", "garbage"]]
    | tuple[Literal["raw"], bytes]
)
type Program = tuple[Step, ...]


@dataclass(frozen=True)
class Turn:
    """What the agent of one fresh turn does, and what the schedule does around it."""

    program: Program = ()
    late: Program = ()
    """Calls made with the turn's token after it ended, before the next request runs."""
    edit: bool = True
    """Whether the agent changes its candidate; otherwise the turn starts on the content the last left."""
    nested: bool = False
    """The agent clones a repository into its workspace and submits, then removes it."""
    closing_at_yield: bool = False
    """The run stops taking suspensions between the agent's wait and the end of its turn."""
    foreign_wait: str | None = None
    """The turn ends waiting on this job, which the run does not own, in place of what it waited on."""
    thinks_s: float = 0.0
    """How long the agent works before its first call, on the run clock."""


def _commit(label: str) -> str:
    """A 40-digit object name derived from ``label``."""
    return hashlib.sha256(label.encode()).hexdigest()[:40]


class _Directory:
    """A directory that outlives a host, and is copied with the outside world."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.copies = 0

    def __deepcopy__(self, memo: dict[int, object]) -> _Directory:
        self.copies += 1
        duplicate = _Directory(
            self.path.with_name(f"{self.path.name}-copy{id(memo) % 10**6}-{self.copies}")
        )
        memo[id(self)] = duplicate
        shutil.copytree(self.path, duplicate.path)
        return duplicate


class HeldSnapshot:
    """A snapshot that can be held, as one queued behind other work on a lock is."""

    def __init__(self) -> None:
        self.hold: asyncio.Event | None = None
        self.arrived = asyncio.Event()

    async def pass_through(self) -> None:
        """Wait here while a hold is in place."""
        if self.hold is not None and not self.hold.is_set():
            self.arrived.set()
            await self.hold.wait()


class _Workspace:
    """The agent's Fake workspace: its revision changes with each turn's edit, as Git content does.

    A snapshot is its own commit, so it never equals the commit the turn reports (the retention
    commit carries its own metadata); the same content gives the same snapshot.
    """

    def __init__(self, world: AgentToolWorld, held: HeldSnapshot) -> None:
        self._world = world
        self._held = held

    async def snapshot_and_retain(self, label: str, *, retention_label: str) -> str:
        """The current content as a snapshot revision, the same one until the next edit."""
        del label, retention_label
        await self._held.pass_through()
        self._refuse_nested()
        return _commit(f"snapshot:{self._world.edits[-1]}")

    async def measured_input(self, revision: str) -> str:
        """Each snapshot is its own measured input."""
        return f"input:{revision}"

    async def matches_revision(self, revision: str) -> bool:
        """Whether nothing changed since the snapshot ``revision``."""
        self._refuse_nested()
        return revision == _commit(f"snapshot:{self._world.edits[-1]}")

    def _refuse_nested(self) -> None:
        """Refuse as host Git does while the agent keeps a repository in its workspace."""
        if self._world.nested:
            raise NestedRepositoryError(self._world.nested)


class _Workspaces:
    """Every scope's workspace is the one Fake."""

    def __init__(self, world: AgentToolWorld, held: HeldSnapshot) -> None:
        self._workspace = _Workspace(world, held)

    async def workspace_of(self, scope: Scope) -> SnapshotWorkspace | None:
        """The one workspace."""
        del scope
        return self._workspace


class _ShownAdmission:
    """The admission with the run shown as another state, as a commit concurrent with a call would."""

    def __init__(self, inner: Admission, shown: CoreState) -> None:
        self._inner = inner
        self._shown = shown

    @property
    def admitted_core(self) -> CoreState:
        """The state shown in place of the committed one."""
        return self._shown

    def admit(self, event: CoreEvent, *, now_at: float) -> Transition:
        """The real admission."""
        return self._inner.admit(event, now_at=now_at)


def _submitted(request: SubmitMeasurement) -> Answer:
    """An executor that took a submission names its job by the handle core derived."""
    assert request.request_id is not None
    return Running(resource_id=ResourceId(root=handle_for(request.request_id)))


class AgentToolWorld:
    """The skeleton run, its scripted far side, and an agent that calls the evaluation tools.

    ``turns`` are what the agent does in each fresh turn, in order; a turn past them does
    nothing. With ``finished_in_submit_round`` the executor answers each of the agent's own
    submissions as already finished, with its evidence, as a verdict-cache replay does;
    otherwise the job is still running.
    """

    def __init__(
        self,
        directory: Path,
        turns: tuple[Turn, ...],
        *,
        total: int,
        finished_in_submit_round: bool = False,
        measured: bool = False,
    ) -> None:
        """Script a run of ``total`` implementer turns, with the owed record below ``directory``.

        With ``measured`` every measurement ends as it is accepted with a headline reading (the
        base's and each candidate's), and the task states how to compare them, so a reply to a
        call the run already measured carries the framework's comparison.
        """
        selected = SkeletonConfig(turns=total)
        self.setup = RunSetup(
            new_launch=lambda: Launch(skeleton, selected),
            harness=Harness(
                registry=OPERATIONS.registry(),
                facts=MEASURED_FACTS if measured else FACTS,
                lifecycle=frozenset({"suspend"}),
                limits=sample_limits(max_turns=4 * total + 4, request_retries=sample_retries(2)),
                deadline_at=1000.0,
                schemas=skeleton.reply_schemas,
            ),
            schemas=skeleton.reply_schemas,
        )
        self.turns = turns
        self.finished_in_submit_round = finished_in_submit_round
        self.measured = measured
        self._evaluator = Executors(submit=_submitted)
        self.owed = _Directory(directory / "owed")
        self.owed.path.mkdir(parents=True)
        # The agent's own position and memory.
        self.edits = [_commit("edit:0")]
        self.nested: tuple[str, ...] = ()
        """The ``.git`` paths the agent left below its workspace root."""
        self.seen = 0
        self.pending: tuple[Program, ...] = ()
        self.waiting = False
        self.yielded: Continuation | None = None
        """The continuation the bridge yielded for the turn in flight, which its answer carries."""
        self.handles: list[str] = []
        self.errors: list[str] = []
        self.waits_accepted = 0
        self.suspended = 0
        """How many turns ended with a continuation the bridge yielded."""
        self.read: list[tuple[str, str]] = []
        """(candidate content, handle) for every handle reply the agent read."""
        self.comparisons: list[tuple[str, dict[str, object] | None]] = []
        """(candidate content, the comparison the reply carried) for every handle reply the agent read."""
        self.budget_refused: list[str] = []
        """The candidate content of every submit that core's budget refused."""
        self.after_run: list[str] = []
        """The refusals of the calls made once the run had ended: every one a typed reply."""
        self.halts: list[str] = []
        """What a call raised instead of answering: run state the bridge cannot trust."""
        self.unacknowledged: tuple[str, ...] = ()
        """The charged submissions whose handle never reached the agent, as the last host ended."""

    def serve(self, admission: Admission, clock: Clock) -> _Host:
        """The bridge and the agent's calls of a new host."""
        return _Host(self, admission, clock)

    def __call__(self, request: Request, core: CoreState) -> Answer:
        """What the far side answers: the agent's reply to a turn, scripts for everything else."""
        if isinstance(request, DispatchTurn | ResumeSessionTurn):
            lease = ResourceId(root=f"lease:{request.turn.session.session_id.root}")
            reply = {"commit": self.edits[-1], "waiting": self.waiting}
            suspension, self.yielded = self.yielded, None
            return Succeeded(
                output_json=json.dumps(reply), resource_id=lease, suspension=suspension
            )
        if isinstance(request, ExecuteRegisteredOperation):
            # The skeleton asks for nothing but its prompt to be rendered.
            prompt = ArtifactRef(
                artifact_id=ArtifactId(root=f"prompt:{request.operation_id.root}"), digest="prompt"
            )
            return Succeeded(outcome=RenderedArtifacts(status="succeeded", prompts=(prompt,)))
        if self.measured:
            measured = self._measured(request, core)
            if measured is not None:
                return measured
        if (
            self.finished_in_submit_round
            and isinstance(request, SubmitMeasurement)
            and request.plan.purpose == "local-validation"
        ):
            return self._evaluator.finished_agent_evaluation(request)
        return self._evaluator(request, core)

    def _measured(self, request: Request, core: CoreState) -> Answer | None:
        """What a measured world's evaluator answers: the base input, and finished readings."""
        if isinstance(request, EnsureWorkspace):
            opened = self._evaluator(request, core)
            assert isinstance(opened, Succeeded)
            return replace(opened, measured_input=BASE_INPUT)
        if not isinstance(request, SubmitMeasurement):
            return None
        finished = self._evaluator.finished_agent_evaluation(request)
        assert isinstance(finished, Succeeded)
        value = BASE_READING if request.plan.purpose == "baseline" else CANDIDATE_READING
        reading = Reading(metric="throughput", value=value, direction="max", unit="tokens/s")
        return replace(
            finished,
            evidence=tuple(
                ref.model_copy(update={"reading": reading})
                if ref.kind is EvidenceKind.BENCHMARK
                else ref
                for ref in finished.evidence
            ),
        )


def _foreign_wait(request: DispatchTurn, job: str) -> Continuation:
    """A wait on a job the run does not own, which core refuses when it commits the suspension."""
    turn = request.turn
    name = turn.invocation_id.root
    session, generation = turn.session.session_id, request.scope.generation
    return Continuation(
        continuation_id=ContinuationId(root=f"{name}/evaluation"),
        invocation=InvocationRef(
            session_id=session, invocation_id=turn.invocation_id, generation=generation
        ),
        next_invocation=InvocationRef(
            session_id=session,
            invocation_id=InvocationId(root=f"{name}/resume"),
            generation=generation,
        ),
        jobs=(ResourceId(root=job),),
        deadline_at=500.0,
        phase=ContinuationPhase.WAITING,
    )


def new_bridge(
    world: AgentToolWorld,
    held: HeldSnapshot | None = None,
    diagnostics: Callable[[str], None] = print,
) -> AgentEvaluationBridge:
    """The evaluation bridge of one host: the agent's Fake workspace, the world's owed record."""
    return AgentEvaluationBridge(
        Path("unused.sock"),
        POLICY,
        _Workspaces(world, held or HeldSnapshot()),
        AgentEvaluationRecords(
            OwedSubmissions(ReceiptStore(owed_namespace(world.owed.path), threads=loop_threads())),
            SynthesizedEvidence(),
        ),
        diagnostics,
        network=SimNetwork(loop_threads()),
        threads=loop_threads(),
    )


class _Host:
    """One host's bridge and the agent's calls through it; it ends with the host."""

    def __init__(self, world: AgentToolWorld, admission: Admission, clock: Clock) -> None:
        self._world = world
        self._admission = admission
        self._clock = clock
        self._diagnostics: list[str] = []
        self._held = HeldSnapshot()
        self._bridge = new_bridge(world, self._held, self._diagnostics.append)
        self._bridge.attach(admission, clock)

    # the service the host runs beside the loop

    async def during(self, request: Request, core: CoreState) -> None:
        """Calls of turns that already ended, then, in a fresh turn, the agent's program."""
        del core
        await self._late_calls()
        if isinstance(request, DispatchTurn | ResumeSessionTurn) and (
            request.turn.charge_class != "resume"
        ):
            await self._turn()
            if isinstance(request, DispatchTurn):
                # A turn that ended waiting yields its continuation, as the session executor asks.
                self._world.yielded = self._yielded(request)
                self._world.suspended += (
                    self._world.yielded is not None
                    and self._world.turns[self._world.seen - 1].foreign_wait is None
                )
                if self._world.turns[self._world.seen - 1].closing_at_yield:
                    # The turn is over for the bridge: the handles it waited on are no longer its own.
                    await self._call(self._token(), ("wait", (0,)), count=False)
                    if not self._world.errors or (
                        "were not submitted by this agent in this turn"
                        not in self._world.errors[-1]
                    ):
                        # Not an assertion: the loop would charge a raising effect and run it again.
                        self._world.halts.append("a wait after the turn's yield was not refused")

    async def close(self) -> None:
        """The host ended: call as an agent holding an old token does, then close the bridge."""
        core = self._admission.admitted_core
        if core.run.status == RunStatus.TERMINAL:
            # A call after the run ended is refused in the bridge's words, never raises.
            before = len(self._world.errors)
            token = self._token()
            for step in (("submit",), ("wait", (0,))):
                await self._call(token, step, count=False)
            await self._late_calls()
            self._world.after_run = self._world.errors[before:]
            del self._world.errors[before:]
        self._world.unacknowledged = self._bridge.unacknowledged()
        await self._bridge.close()
        assert self._world.halts == [], self._world.halts

    # the agent

    def _token(self) -> str:
        (descriptor,) = self._bridge.servers(ROLE, SCOPE)
        return dict(descriptor.runtime_env)["VS_EVALUATION_TOKEN"]

    def _yielded(self, request: DispatchTurn) -> Continuation | None:
        """The bridge's yield for the turn, with the run shown closing if the turn asked for it."""
        turn = self._world.turns[self._world.seen - 1] if self._world.seen else Turn()
        if turn.foreign_wait is not None:
            return _foreign_wait(request, turn.foreign_wait)
        if not turn.closing_at_yield:
            return self._bridge.yielded(request)
        core = self._admission.admitted_core
        closing = core.model_copy(
            update={"run": core.run.model_copy(update={"status": RunStatus.CLOSING})}
        )
        self._bridge.attach(_ShownAdmission(self._admission, closing), self._clock)
        try:
            if self._bridge.yielded(request) is not None:
                # Not an assertion: the loop would charge a raising effect and run it again.
                self._world.halts.append("the gate did not refuse the wait at the end of the turn")
        finally:
            self._bridge.attach(self._admission, self._clock)
        return None

    async def _turn(self) -> None:
        """Run the next fresh turn's program; the reply says waiting if a wait was accepted."""
        world = self._world
        index = world.seen
        world.seen += 1
        turn = world.turns[index] if index < len(world.turns) else Turn()
        if turn.edit:
            world.edits.append(_commit(f"edit:{len(world.edits)}"))
        token = self._token()
        world.waiting = False
        if turn.thinks_s:
            await self._clock.sleep(turn.thinks_s)
        if turn.nested:
            world.nested = ("vendor/lib/.git",)
            await self._call(token, ("submit",), count=False)
            world.nested = ()
        for step in turn.program:
            if await self._call(token, step) and step[0] == "wait":
                world.waiting = True
        world.pending = (*world.pending, turn.late)

    async def _late_calls(self) -> None:
        """Calls of turns that already ended, with the token those turns held.

        A token names a scope, not a turn, so a late wait the bridge accepts joins the turn that
        is executing in that scope; it counts as an accepted wait.
        """
        pending, self._world.pending = self._world.pending, ()
        token = self._token()
        for program in pending:
            for step in program:
                await self._call(token, step)

    async def _call(self, token: str, step: Step, *, count: bool = True) -> bool:
        """One tool call; True when it succeeded. A refusal must carry its template text."""
        try:
            return await self._attempt(token, step, count=count)
        except Exception as error:  # noqa: BLE001  # lint-waiver: LW-241301 [BLE001]; the test records what a call raised, any type, as a halt.
            # > A narrower catch would let an unexpected type escape as a test error instead of
            # > naming the call that halted the run.
            self._world.halts.append(f"{step}: {type(error).__name__}: {error}")
            return False

    async def _attempt(self, token: str, step: Step, *, count: bool) -> bool:
        match step:
            case ("forged", kind):
                # A token the bridge did not issue (another's, or none): never honoured.
                forged = (
                    token[:-1] + ("0" if token[-1] != "0" else "1") if kind == "foreign" else "x"
                )
                return await self._refused(SubmitCall(token=forged).model_dump_json().encode())
            case ("raw", frame):
                return await self._refused(frame)
            case ("submit",):
                frame = SubmitCall(token=token).model_dump_json().encode()
            case ("lose",):
                # The call completes on the host; the agent's deadline passed, so it never reads
                # the reply and the client never confirms it.
                await self._bridge.answer(SubmitCall(token=token).model_dump_json().encode())
                return True
            case ("timeout_retry",):
                frame = SubmitCall(token=token).model_dump_json().encode()
                return await self._read_and_confirm(
                    step, await self._after_timeout(frame), count=count
                )
            case ("wait", picks):
                handles = self._world.handles
                names = tuple(
                    dict.fromkeys(handles[i] if i < len(handles) else f"unknown-{i}" for i in picks)
                )
                frame = WaitCall(token=token, handles=names).model_dump_json().encode()
        return await self._read_and_confirm(step, await self._bridge.answer(frame), count=count)

    async def _refused(self, frame: bytes) -> bool:
        """A frame the bridge must refuse in words; answering it with a success is a halt."""
        reply, confirm = await self._bridge.answer(frame)
        confirm()
        answer = REPLY.validate_json(reply)
        if not isinstance(answer, SocketFailure) or not answer.error:
            self._world.halts.append(f"a frame that is no valid call was honoured: {frame!r}")
        return False

    async def _read_and_confirm(
        self, step: Step, answered: tuple[bytes, Callable[[], None]], *, count: bool
    ) -> bool:
        """The agent reads the reply, and only then does its client confirm the receipt."""
        reply, confirm = answered
        ok = self._record(step, reply, count=count)
        confirm()
        return ok

    async def _after_timeout(self, frame: bytes) -> tuple[bytes, Callable[[], None]]:
        """A submit that outlasts the agent's deadline, then the agent's retry.

        The first call is held in its snapshot while the agent gives up (nobody reads its reply),
        the retry arrives and queues behind it, and the snapshot then finishes.
        """
        held = self._held
        held.hold, held.arrived = asyncio.Event(), asyncio.Event()
        first = asyncio.create_task(self._bridge.answer(frame))
        arrived = asyncio.create_task(held.arrived.wait())
        await asyncio.wait({first, arrived}, return_when=asyncio.FIRST_COMPLETED)
        retry = asyncio.create_task(self._bridge.answer(frame))
        await yield_now()
        held.hold.set()
        arrived.cancel()
        _, answered = await asyncio.gather(first, retry, return_exceptions=True)
        if isinstance(answered, BaseException):
            raise answered
        return answered

    def _record(self, step: Step, raw: bytes, *, count: bool) -> bool:
        """File one reply: an error with its text, a new handle, or an accepted wait."""
        world = self._world
        reply = REPLY.validate_json(raw)
        if isinstance(reply, SocketFailure):
            assert reply.error
            world.errors.append(reply.error)
            if step[0] in ("submit", "timeout_retry") and "submission budget" in reply.error:
                world.budget_refused.append(world.edits[-1])
            return False
        result = reply.result or {}
        if step[0] in ("submit", "timeout_retry") and "handle_id" in result:
            handle = str(result["handle_id"])
            assert any(
                call.request_id is not None and handle_for(call.request_id) == handle
                for call in self._admission.admitted_core.evaluation.agent_calls
            ), "the charge must be durable before the agent reads its handle"
            world.handles.append(handle)
            world.read.append((world.edits[-1], handle))
            comparison = result.get("comparison")
            world.comparisons.append(
                (world.edits[-1], comparison if isinstance(comparison, dict) else None)
            )
        elif count:
            world.waits_accepted += 1
        return True


__all__ = [
    "BASE_READING",
    "BASE_STALENESS_S",
    "CANDIDATE_READING",
    "FACTS",
    "ROLE",
    "SCOPE",
    "AgentToolWorld",
    "HeldSnapshot",
    "Program",
    "Step",
    "Turn",
    "new_bridge",
]
