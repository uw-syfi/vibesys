"""A toy search on core's agent pipeline, and the scripted world around it (test support).

The search is the smallest one that exercises what core owns of agent turns: it opens a
workspace per workstream, asks an implementer (a write turn) and then a judge (a read-only turn)
in it, and lets the workspace settle. Both roles may end a turn by waiting for an evaluation
they submitted. Whatever core says (a refusal, a failed turn, a wait that is refused), the search
takes it as the typed failure it is and goes on, so a run that does not end is core's doing. The
root just returns: the framework adopts the input and ends the run.

`ToyHost` plays everything outside core: it answers each request the way an executor would, and
lets the turns that wait submit their evaluation through core's agent-call admission (the event
the production bridge admits). It never decides what core concludes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Literal

import vs_core.api as core
from vs_core.api.search import search
from vs_core.api.testing import (
    Answer,
    Failed,
    Faults,
    Harness,
    Running,
    Succeeded,
    Trace,
    drive,
    sample_limits,
    sample_retries,
)

from .test_measurements import plan as measurement_plan

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pydantic import BaseModel

type Ending = Literal["wait", "result", "invalid"]
ENDINGS: tuple[Ending, ...] = ("wait", "result", "invalid")
"""How a turn that validated its wait can end: the waiting reply, its final result, junk."""

ROLES = ("implementer", "judge")


# -- the operations and roles the search declares ----------------------------------------------


class Prompt(core.Value):
    """What a toy agent is asked."""

    text: str


class Result(core.RevisionReply):
    """What an implementer answers."""

    summary: str


class Verdict(core.Value):
    """What a judge answers."""

    passed: bool


class Render(core.RenderRoleArtifactsBase[Prompt]):
    """Render one role prompt."""

    kind: Literal["toy.render_role_artifacts"] = "toy.render_role_artifacts"


class Verify(core.VerifyParentRevisionBase):
    """Verify a parent revision."""

    kind: Literal["toy.verify_parent_revision"] = "toy.verify_parent_revision"


class Readings(core.EvidenceReadingsBase[core.Value]):
    """No readings: the toy search never interprets evidence."""


class Interpret(core.InterpretEvidenceBase):
    """Decode evidence."""

    kind: Literal["toy.interpret_evidence"] = "toy.interpret_evidence"
    outcome_model: ClassVar[type[BaseModel]] = Readings


OPERATIONS = core.StrategyOperations("toy", Render, Verify, Interpret)
_TURN_TIME = 10_000.0

EXCHANGES = core.Exchanges(
    correction="toy/correction.j2", resume="toy/resume.j2", wait_unrecorded="toy/unrecorded.j2"
)
IMPLEMENTER = core.Role(
    name="implementer",
    title="Implementer",
    prompt="toy/implementer.j2",
    reply=Result,
    workspace=core.WorkspaceMode.ISOLATED_CHILD,
    access=core.Access.WRITE_CANDIDATE,
    turn_time=_TURN_TIME,
    corrections=1,
    lost_turns=1,
    charge=core.Charge.ATTEMPT,
    may_wait_for_evaluations=True,
    exchanges=EXCHANGES,
)
"""The implementer writes a candidate."""
JUDGE = core.Role(
    name="judge",
    title="Judge",
    prompt="toy/judge.j2",
    reply=Verdict,
    workspace=core.WorkspaceMode.READ_ONLY_REVISION,
    access=core.Access.READ_ONLY,
    turn_time=_TURN_TIME,
    corrections=1,
    lost_turns=1,
    charge=core.Charge.TURN,
    may_wait_for_evaluations=True,
    exchanges=EXCHANGES,
)
"""The judge only reads the candidate."""

# -- the search ----------------------------------------------------------------------------------


class Config(core.Value):
    """How many workstreams the toy search runs, and how many at once."""

    workstreams: int = 1
    in_flight: int = 1


class Done(core.Value):
    """What a workstream ended with: which asks were answered, and how its workspace closed."""

    key: str
    asked: tuple[str, ...] = ()
    """The roles whose ask ended in a reply."""
    failed: tuple[str, ...] = ()
    """The roles whose ask ended in a typed failure."""
    opened: bool = True
    closed: bool = False


class Summary(core.Value):
    """What the search published: every workstream's end."""

    done: tuple[Done, ...] = ()


async def workstream(ctx: core.Flow, key: str, number: int, roles: tuple[core.Role, ...]) -> Done:
    """Open a workspace, ask each role in turn in it, let the workspace settle."""
    asked: list[str] = []
    failed: list[str] = []
    async with ctx.workspaces.open(
        attempt=core.AttemptId(root=key),
        item=core.ItemId(root=key),
        plan=core.WorkspacePlan(mode=core.WorkspaceMode.ISOLATED_CHILD, base=ctx.facts.baseline),
        budget=core.AttemptBudget(admission_charge=1, paid_invocation_limit=6),
    ) as workspace:
        if isinstance(workspace, core.NotOpened):
            return Done(key=key, opened=False)
        revision = ctx.facts.baseline
        for role in roles:
            session = ctx.agents.session(role, workspace=workspace, revision=revision)
            event = await session.turn(Prompt(text=f"{role.name} {number}"))
            match event:
                case core.TurnFinished(checkpoint=core.Retained(revision=retained)):
                    revision = retained
                    asked.append(role.name)
                case core.TurnFinished():
                    asked.append(role.name)
                case core.Failed():
                    failed.append(role.name)
                    break
        if revision != ctx.facts.baseline:
            ctx.workspaces.keep(
                workspace, revision, core.Assessment(outcome="failed", retention="discard")
            )
    closed = not isinstance(workspace, core.NotOpened) and workspace.settlement is not None
    return Done(key=key, asked=tuple(asked), failed=tuple(failed), closed=closed)


def toy_search(
    implementer: core.Role = IMPLEMENTER, judge: core.Role = JUDGE
) -> core.SearchDef[Config]:
    """The toy search over these two roles (a test varies a role to see what it governs)."""
    roles = (implementer, judge)

    @search("toy", config=Config, operations=OPERATIONS, roles=roles)
    async def toy(ctx: core.Flow, config: Config) -> None:
        """Run the workstreams ``in_flight`` at a time; the framework then adopts the input."""
        started = 0
        done: list[Done] = []
        async with ctx.group(limit=config.in_flight) as group:
            while len(done) < config.workstreams:
                while started < config.workstreams and group.free:
                    group.start(f"w{started}", workstream, f"w{started}", started, roles)
                    started += 1
                ended = await ctx.first(group)
                if not isinstance(ended, Done):
                    break  # the run was stopped: the group wound its workstreams down
                done.append(ended)
        ctx.publish(Summary(done=tuple(done)))

    return toy


toy = toy_search()


# -- the world around core ------------------------------------------------------------------------

FACTS = core.RunFacts(
    objective="make it faster",
    baseline=core.RevisionRef.of_git_commit("abc123"),
    evaluator_digest="evaluator",
    workload_digest="workload",
    environment_digest="environment",
)
LIMITS = sample_limits(
    max_attempts=8,
    max_turns=60,
    max_parallel=4,
    request_retries=sample_retries(2),
    max_refunds=2,
)


def revision_of(attempt: str) -> core.RevisionRef:
    """The commit the workspace executor retains for an attempt (fixed by the attempt)."""
    return core.RevisionRef.of_git_commit(hashlib.sha256(attempt.encode()).hexdigest()[:40])


def waiting_reply(handles: Sequence[str] = ("h",)) -> str:
    """The reply of a turn that waits for the evaluations it submitted."""
    return json.dumps({"kind": "waiting_for_evaluation", "handles": list(handles)})


def _final(role: str) -> str:
    return json.dumps({"summary": "done"} if role == "implementer" else {"passed": True})


_IN_FLIGHT = frozenset(
    {core.RequestPhase.PREPARED, core.RequestPhase.DISPATCHED, core.RequestPhase.RECONCILING}
)


def _control(action: Literal["pause", "resume"], now_at: float) -> core.RunControlEvent:
    control = (
        core.ControlInput(
            control_id=core.ControlId(root=action), action=action, owner=core.PauseOwner.OPERATOR
        )
        if action == "pause"
        else core.ControlInput(control_id=core.ControlId(root=action), action=action, when="now")
    )
    return core.RunControlEvent(control=control, now_at=now_at)


def _lease(session_id: core.SessionId) -> core.ResourceId:
    return core.ResourceId(root=f"lease:{session_id.root}")


@dataclass
class ToyHost:
    """Scripted executors for one run.

    ``waits`` says, for the n-th fresh turn of a role in ``roles`` (cycling), whether it
    submits an evaluation of its candidate and ends waiting for it; ``polls`` how many polls the
    n-th job reports running; ``ends`` how the n-th waiting turn ends once it validated its wait.
    ``shares`` makes every turn of an attempt ask for the same candidate evaluation (as an
    implementer and its judge do); else each submits its own candidate.
    """

    waits: Sequence[bool] = ()
    polls: Sequence[int] = (0,)
    ends: Sequence[Ending] = ("wait",)
    roles: Sequence[str] = ROLES
    submit_only: Sequence[str] = ()
    junk: Sequence[str] = ()
    """Roles whose turns end with a reply that does not parse."""
    events: tuple[core.CoreEvent, ...] = ()
    """Operator controls delivered before the first decision."""
    pause_at: int | None = None
    """The operator pauses the run just before the executor of the n-th request answers (from 0),
    if the run is open then, and resumes it once nothing of the paused run is in flight."""
    paused_with: str | None = None
    """The kind of the request the pause came before, once the operator paused."""
    shares: bool = True
    calls: list[tuple[str, str, str]] = field(default_factory=list)
    """(attempt, role, call id) of every evaluation a turn asked for."""
    _fresh: int = 0
    _jobs: int = 0
    _requests: int = 0
    _running: dict[str, int] = field(default_factory=dict)
    _cancelled: set[str] = field(default_factory=set)
    """Jobs core cancelled: the scheduler reports each ended failed at its next poll."""
    _asking: dict[str, Ending] = field(default_factory=dict)

    # -- the turns' tool calls --------------------------------------------------------------

    def during_turn(
        self, request: core.Request, state: core.CoreState
    ) -> tuple[core.CoreEvent, ...]:
        """The operator's pause, then the evaluation a fresh turn submits while it runs."""
        pause = self._pause(request, state)
        if not isinstance(request, core.DispatchTurn) or request.request_id is None:
            return pause
        role = request.turn.session.role_id.root.removeprefix("toy-")
        if role in self.submit_only:
            return (*pause, self._call(request, role))
        if role in self.roles and self.waits:
            ordinal = self._fresh
            self._fresh += 1
            if self.waits[ordinal % len(self.waits)]:
                self._asking[request.request_id.root] = self.ends[ordinal % len(self.ends)]
                return (*pause, self._call(request, role))
        return pause

    # -- the operator ----------------------------------------------------------------------

    def _pause(self, request: core.Request, state: core.CoreState) -> tuple[core.CoreEvent, ...]:
        ordinal = self._requests
        self._requests += 1
        run = state.run
        if ordinal != self.pause_at or run.status != core.RunStatus.RUNNING or run.result:
            return ()
        self.paused_with = type(request).__name__
        return (_control("pause", run.now_at),)

    def operator(self, state: core.CoreState) -> tuple[core.CoreEvent, ...]:
        """Resume a paused run once nothing of it is in flight (asked while nothing dispatches).

        That is where the run sits while it waits on the operator: whatever it was doing when
        it was paused has finished, and what it decided since is held until the resume.
        """
        if state.run.status != core.RunStatus.PAUSED:
            return ()
        if core.project(state).next_observe_at is not None or any(
            record.phase in _IN_FLIGHT for record in state.records.records
        ):
            return ()
        return (_control("resume", state.run.now_at),)

    def _call(self, request: core.DispatchTurn, role: str) -> core.AgentMeasurementRequested:
        attempt = request.scope.owner.root
        candidate = revision_of(attempt if self.shares else request.turn.invocation_id.root)
        call = f"call-{len(self.calls)}"
        self.calls.append((attempt, role, call))
        return core.AgentMeasurementRequested(
            scope=request.scope,
            plan=measurement_plan(purpose="local-validation", candidate=candidate),
            within=core.Within(limit=500.0),
            call_id=call,
        )

    # -- the executors ----------------------------------------------------------------------

    def __call__(self, request: core.Request, state: core.CoreState) -> Answer:
        """What the executor of ``request`` reports."""
        match request:
            case core.ObserveOwnedJob():
                return self._observe(request, state)
            case core.SubmitMeasurement():
                return self._submit(request)
            case core.CancelOwnedJob():
                self._cancelled.add(request.resource_id.root)
                return Succeeded(resource_id=request.resource_id)
            case core.DispatchTurn() | core.ResumeSessionTurn():
                return self._turn(request, state)
            case core.ExecuteRegisteredOperation():
                prompt = core.ArtifactId(root=f"prompt:{request.operation_id.root}")
                return Succeeded(
                    outcome=core.RenderedArtifacts(
                        status="succeeded",
                        prompts=(core.ArtifactRef(artifact_id=prompt, digest="prompt"),),
                    )
                )
            case _:
                return self._resource(request)

    @staticmethod
    def _resource(request: core.Request) -> Answer:
        """What the executors of workspaces, sessions, retention and adoption report."""
        revision: core.RevisionRef | None = None
        resource: core.ResourceId | None = None
        match request:
            case core.EnsureWorkspace():
                root = f"workspace:{request.scope.owner.root}:{request.attempt.generation}"
                resource, revision = core.ResourceId(root=root), request.plan.base
            case core.EnsureSession():
                resource = _lease(request.spec.session_id)
            case core.CloseSession():
                resource = _lease(request.session_id)
            case core.SnapshotAndRetain():
                revision = revision_of(request.attempt.attempt_id.root)
            case core.RetainRevision():
                revision = request.revision
            case core.AdoptRevision() | core.VerifyAdoption():
                revision = request.selection.revision
        return Succeeded(resource_id=resource, revision=revision)

    def _submit(self, request: core.SubmitMeasurement) -> Answer:
        assert request.request_id is not None
        self._jobs += 1
        job = f"job:{self._jobs}"
        self._running[job] = self.polls[(self._jobs - 1) % len(self.polls)]
        return Running(resource_id=core.ResourceId(root=job))

    def _observe(self, request: core.ObserveOwnedJob, state: core.CoreState) -> Answer:
        job = request.resource_id.root
        if job in self._cancelled:
            return Failed(accepted=True)
        left = self._running.get(job, 0)
        if left > 0:
            self._running[job] = left - 1
            return Running(resource_id=request.resource_id)
        owned = next(
            row
            for row in state.evaluation.jobs
            if isinstance(row, core.OwnedJob) and row.resource_id == request.resource_id
        )
        stages = tuple(
            core.EvaluationStageResult(
                stage_id=stage.stage_id, outcome=core.EvaluationStageOutcome.PASSED
            )
            for stage in owned.plan.stages
        )
        plan = owned.plan
        assert isinstance(plan.candidate, core.RevisionRef)
        evidence = tuple(
            core.EvidenceRef(
                evidence_id=core.EvidenceId(root=f"{stage.stage_id}:{owned.submission_id.root}"),
                kind=core.EvidenceKind.BENCHMARK,
                purpose=plan.purpose,
                scope=owned.scope,
                source_request=owned.submission_id,
                candidate=plan.candidate,
                observation_sequence=0,
                evaluator_digest=plan.evaluator_digest,
                workload_digest=plan.workload_digest,
                environment_digest=plan.environment_digest,
                provenance="trusted",
                status=core.ObservationStatus.SUCCEEDED,
            )
            for stage in plan.stages
        )
        return Succeeded(
            resource_id=request.resource_id,
            evidence=evidence,
            facts=core.EvaluationTerminalFacts(stages=stages, accuracy_passed=True),
        )

    def _turn(
        self, request: core.DispatchTurn | core.ResumeSessionTurn, state: core.CoreState
    ) -> Answer:
        role = request.turn.session.role_id.root.removeprefix("toy-")
        lease = _lease(request.turn.session.session_id)
        if isinstance(request, core.DispatchTurn) and request.request_id is not None:
            ending = self._asking.pop(request.request_id.root, None)
            if ending is not None:
                return self._waiting_turn(request, state, ending, role, lease)
        return Succeeded(
            output_json="not json" if role in self.junk else _final(role), resource_id=lease
        )

    @staticmethod
    def _waiting_turn(
        request: core.DispatchTurn,
        state: core.CoreState,
        ending: Ending,
        role: str,
        lease: core.ResourceId,
    ) -> Answer:
        jobs = tuple(
            job.resource_id
            for job in state.evaluation.jobs
            if isinstance(job, core.OwnedJob) and job.scope == request.scope
        )
        deadlines = [
            call.deadline_at
            for call in state.evaluation.agent_calls
            if call.scope == request.scope and call.deadline_at is not None
        ]
        replies = {
            "wait": waiting_reply([job.root for job in jobs]),
            "result": _final(role),
            "invalid": "not json",
        }
        current = core.InvocationRef(
            session_id=request.turn.session.session_id,
            invocation_id=request.turn.invocation_id,
            generation=request.scope.generation,
        )
        resumed = core.InvocationId(root=f"{current.invocation_id.root}/resume")
        suspension = core.Continuation(
            continuation_id=core.ContinuationId(root=f"{current.invocation_id.root}/evaluation"),
            invocation=current,
            next_invocation=current.model_copy(update={"invocation_id": resumed}),
            jobs=jobs,
            deadline_at=min(deadlines),
            phase=core.ContinuationPhase.WAITING,
        )
        return Succeeded(output_json=replies[ending], resource_id=lease, suspension=suspension)


def toy_harness(
    host: ToyHost,
    *,
    limits: core.Limits = LIMITS,
    definition: core.SearchDef[Config] = toy,
    reducers: core.CoreReducers | None = None,
) -> Harness:
    """The harness the toy search runs on the real core under, against ``host``."""
    return Harness(
        registry=OPERATIONS.registry(),
        facts=FACTS,
        limits=limits,
        lifecycle=frozenset({"suspend"}),
        during_turn=host.during_turn,
        operator=host.operator,
        shell_events=True,
        schemas=definition.reply_schemas,
        envelope_type=core.RunEnvelope[core.FlowState],
        events=host.events,
        reducers=reducers,
    )


def run_toy(
    host: ToyHost,
    config: Config | None = None,
    *,
    limits: core.Limits = LIMITS,
    faults: Faults | None = None,
    definition: core.SearchDef[Config] = toy,
) -> Trace:
    """Drive the toy search on the real core against ``host`` until nothing more happens."""
    chosen = config or Config()
    harness = toy_harness(host, limits=limits, definition=definition)
    return drive(definition.driver(chosen, OPERATIONS.registry()), chosen, host, harness, faults)


def summary(trace: Trace) -> Summary:
    """What the search published at the end of the run."""
    assert isinstance(trace.state, core.FlowState), type(trace.state)
    published = core.FlowDriver.published(trace.state, Summary)
    assert published is not None, "the search published no summary"
    return published
