"""What the dynamic search knows of its run, and the plain decisions that follow from it.

`RunRecord` is the root flow's one piece of mutable state. The flows it starts report back
through `planned` and `finished`, which fold their results in; nothing else writes it. It holds
the facts (hypotheses, finished work, what is running, the parents on offer) and answers the
questions the root flow asks of them: may a plan start, is the search over, which plans become
workstreams, who is ahead. It reads nothing from the run and awaits nothing, so every rule here
is a plain function of its record. Replay rebuilds it after a restart of the same code; after a
code change the root resumes it from its checkpoint (`DynamicResume`, `RunRecord.state`).
"""

from dataclasses import dataclass, field
from typing import Literal

from vibesys.candidates import api as candidates
from vibesys.hypothesis import RunEnding
from vibesys.orchestration.dynamic.models import ProfileField, ProfileOffer
from vibesys.orchestration.dynamic.search import _hypotheses
from vibesys.orchestration.dynamic.search._config import DynamicConfig
from vibesys.orchestration.dynamic.search._context import (
    PlanWorld,
    free_slots,
    offered_parents,
    remaining,
)
from vibesys.orchestration.dynamic.search._handoff import HandoffWritten
from vibesys.orchestration.dynamic.search._ids import INPUT_KEY, planning_key, workstream_key
from vibesys.orchestration.dynamic.search._main import MainLine, MainState
from vibesys.orchestration.dynamic.search._outcomes import (
    WorkstreamDone,
    WorkstreamStart,
    snapshot_of,
)
from vibesys.orchestration.dynamic.search._parents import (
    ParentConflictError,
    ParentOption,
    ParentSnapshot,
    ingest,
    resolve,
)
from vibesys.orchestration.dynamic.search._plan import PlanCheck
from vibesys.orchestration.dynamic.search._prompts import (
    DroppedEvaluation,
    EvaluationQueueView,
    InputOverdueView,
    QueuedEvaluation,
)
from vibesys.orchestration.dynamic.search._queue import (
    Bound,
    EvaluationQueue,
    QueueSnapshot,
    QueueState,
    SlotVerdict,
    Submission,
)
from vibesys.orchestration.dynamic.search._reports import InputOverdue
from vibesys.orchestration.dynamic.search._steps import (
    DONE,
    StepState,
    built_on,
    ready_steps,
    step_plan,
    step_states,
)
from vibesys.orchestration.dynamic.search._values import (
    CombineSource,
    FinishedWork,
    HypothesisRecord,
    MainMeasurement,
    MergeRecord,
    MergeSource,
    PlanProblem,
    Step,
    Unreachable,
    WorkKind,
    WorkPlan,
)
from vibesys.search_spans.api import adoption_label
from vs_core.api.search import INPUT, Input, Kept, Label, NoWinner, RevisionRef, TaskTerms, Value

_DROPPED_SHOWN = 8
"""The most recent dropped evaluations the orchestrator is shown."""


@dataclass(frozen=True)
class Scheduled:
    """A planning call ended with a plan to schedule, and the handoff file it left.

    ``blind`` says the plan answered a prompt asked before the input's measurement landed.
    """

    check: PlanCheck
    blind: bool = False
    handoff: HandoffWritten | None = None
    """The call's handoff file and every one the owner holds; None when its write failed."""
    planner: str | None = None
    """The planner agent whose reply this plan is: it starts the workstreams the plan schedules."""


@dataclass(frozen=True)
class NoPlan:
    """A planning call ended with nothing valid.

    ``stuck`` says nothing is in flight to wait for, so planning has failed for good;
    ``blocked_at_done`` is the number of finished workstreams planning waits to exceed.
    """

    problem: PlanProblem | None
    stuck: bool
    blocked_at_done: int


@dataclass(frozen=True)
class Outlined:
    """The outline call ended with the steps of the plan the run follows."""

    steps: tuple[Step, ...]
    outliner: str | None = None
    """The outliner agent whose reply the steps are: it starts the steps' workstreams."""


type PlanOutcome = Scheduled | NoPlan | Outlined


@dataclass
class PlannerGate:
    """Whether planning may start again, and why the last call ended empty."""

    failed: bool = False
    """The last call ended with nothing valid and nothing in flight to wait for."""
    blocked_at_done: int | None = None
    """Finished workstreams when a call scheduled nothing; planning resumes after another one."""
    last_error: PlanProblem | None = None
    """Shown in the first prompt of the next call."""


class Running(Value):
    """A workstream in flight, as the published record shows it."""

    sequence: int
    plan: WorkPlan
    parent: RevisionRef | None = None
    """The revision it builds on, frozen when it was scheduled."""


class RunSnapshot(Value):
    """What the search publishes of its record, for the projector and for tests."""

    kind: Literal["dynamic-run-snapshot"] = "dynamic-run-snapshot"
    baseline: candidates.Baseline | None
    """The input's measurement; None while it is still being measured."""
    hypotheses: tuple[HypothesisRecord, ...] = ()
    finished: tuple[FinishedWork, ...] = ()
    running: tuple[Running, ...] = ()
    parents: tuple[ParentSnapshot, ...] = ()
    unreachable: tuple[Unreachable, ...] = ()
    leading: str | None = None
    """The hypothesis (or merge) whose candidate leads now; None while the input does."""
    main: RevisionRef | None = None
    """The run's main revision now: where new work that names no parent starts."""
    main_measurement: MainMeasurement | None = None
    merges: tuple[MergeRecord, ...] = ()
    steps: tuple[Step, ...] = ()
    """The outline of the plan the run follows; empty while exploring or not yet outlined."""
    step_states: tuple[tuple[str, StepState], ...] = ()
    """Each step's state, in the outline's order."""
    evaluations: QueueSnapshot | None = None
    """The trusted evaluations: what holds a slot, what waits for one, what was dropped."""
    input_overdue: InputOverdue | None = None
    """The first workstream that waited longer than the run allows for the input's measurement."""


class RecordState(Value):
    """Everything a `RunRecord` knows, so a restart after a code change resumes it as it was.

    What the run fixes (its config, input revision and task terms) is not kept: the record is
    resumed with them (`RunRecord.resumed`).
    """

    baseline: candidates.Baseline | None
    hypotheses: tuple[tuple[str, HypothesisRecord], ...]
    finished_work: tuple[FinishedWork, ...]
    running: tuple[WorkstreamStart, ...]
    accepted: tuple[WorkPlan, ...]
    parents: tuple[ParentSnapshot, ...]
    withheld: tuple[str, ...]
    refunded: int
    unreachable: tuple[Unreachable, ...]
    gate_failed: bool
    gate_blocked_at_done: int | None
    gate_last_error: PlanProblem | None
    calls: int
    handoffs: tuple[str, ...]
    kept: tuple[tuple[str, Kept], ...]
    main: MainState
    main_seen: tuple[tuple[str, int], ...]
    outline: tuple[Step, ...] | None
    submitted: tuple[Submission, ...]
    evaluations: QueueState
    input_overdue: InputOverdue | None
    planned_by: tuple[tuple[str, str], ...] = ()
    """The agent that planned each queued or started plan, by work id."""


class DynamicResume(Value):
    """What the dynamic root checkpoints, and resumes from after a code change: config and record.

    ``version`` is this model's own: a change to what it holds that an older checkpoint does not
    decode as is a new version, and the root takes the union of the versions it can resume.
    """

    kind: Literal["dynamic-resume"] = "dynamic-resume"
    version: Literal[1] = 1
    config: DynamicConfig
    record: RecordState


@dataclass
class RunRecord:
    """Hypotheses, finished work and parents of one dynamic run, and the slots left to fill."""

    config: DynamicConfig
    base: RevisionRef
    """The revision the run started from."""
    baseline: candidates.Baseline | None
    """The input's measurement; None while it is still being measured (see `measured`)."""
    terms: TaskTerms
    """What the task fixes: the space candidates are ranked in, and the capture's fields."""
    hypotheses: dict[str, HypothesisRecord] = field(default_factory=dict)
    finished_work: list[FinishedWork] = field(default_factory=list)
    running: dict[str, WorkstreamStart] = field(default_factory=dict)
    accepted: list[WorkPlan] = field(default_factory=list)
    """Plans a planning call accepted, not yet started as workstreams."""
    parents: tuple[ParentSnapshot, ...] = ()
    """The candidates the planner may build on."""
    withheld: tuple[str, ...] = ()
    """Parent revisions whose verification failed; never offered to the planner again."""
    refunded: int = 0
    """Workstreams refunded because they never opened."""
    unreachable: list[Unreachable] = field(default_factory=list)
    gate: PlannerGate = field(default_factory=PlannerGate)
    calls: int = 0
    """Planning calls begun; the next call's key is numbered by it."""
    handoffs: tuple[str, ...] = ()
    """The paths of the planning calls' handoff files, oldest first, as the owner last listed them."""
    kept: dict[str, Kept] = field(default_factory=dict)
    """The candidates closed workspaces retained that were winner-eligible, by settlement."""
    main: MainLine = field(init=False)
    """The run's main revision, which only a sound merge moves."""
    main_seen: dict[str, int] = field(default_factory=dict)
    """Main's generation when each hypothesis last started: behind it, the hypothesis rebases."""
    outline: tuple[Step, ...] | None = None
    """The steps of the plan the run follows: the task's own, or the outline call's; None
    while a run that follows a plan has yet to outline it, and in a run that explores."""
    submitted: dict[str, Submission] = field(default_factory=dict)
    """Running workstreams that submitted their candidate for its trusted measurement, by key:
    their agents are done, so they no longer hold an agent slot."""
    planned_by: dict[str, str] = field(default_factory=dict)
    """The agent whose reply planned each plan, by work id: the planner, or the outliner for a
    step it outlined; it starts the plan's workstreams (`WorkstreamStart.started_by`)."""
    evaluations: EvaluationQueue = field(init=False)
    """The submissions waiting for an evaluation slot, and the measurements holding one."""
    input_overdue: InputOverdue | None = None
    """The first report that a workstream waited too long for the input's measurement."""

    def __post_init__(self) -> None:
        """Main starts at the run's input; the task's steps, when declared, are the outline.

        The evaluations are queued, and the input's measurement starts with the run, so it holds
        the first evaluation slot.
        """
        self.main = MainLine(revision=self.base, baseline=self.baseline)
        plan = self.config.plan
        if self.config.follows_plan and plan is not None and plan.steps:
            self.outline = plan.steps
        config = self.config
        self.evaluations = EvaluationQueue(slots=config.slots, policy=config.pick)
        if self.baseline is None:
            self.evaluations.hold(INPUT_KEY)

    # -- resuming after a code change ------------------------------------------------------------

    def state(self) -> RecordState:
        """What the record knows, to resume it from (`resumed`)."""
        return RecordState(
            baseline=self.baseline,
            hypotheses=tuple(self.hypotheses.items()),
            finished_work=tuple(self.finished_work),
            running=tuple(self.running.values()),
            accepted=tuple(self.accepted),
            parents=self.parents,
            withheld=self.withheld,
            refunded=self.refunded,
            unreachable=tuple(self.unreachable),
            gate_failed=self.gate.failed,
            gate_blocked_at_done=self.gate.blocked_at_done,
            gate_last_error=self.gate.last_error,
            calls=self.calls,
            handoffs=self.handoffs,
            kept=tuple(self.kept.items()),
            main=self.main.state(),
            main_seen=tuple(self.main_seen.items()),
            outline=self.outline,
            submitted=tuple(self.submitted.values()),
            evaluations=self.evaluations.state(),
            input_overdue=self.input_overdue,
            planned_by=tuple(self.planned_by.items()),
        )

    @classmethod
    def resumed(
        cls, config: DynamicConfig, base: RevisionRef, terms: TaskTerms, state: RecordState
    ) -> "RunRecord":
        """The record ``state`` describes, in the run ``config``, ``base`` and ``terms`` fix."""
        record = cls(
            config,
            base,
            state.baseline,
            terms,
            hypotheses=dict(state.hypotheses),
            finished_work=list(state.finished_work),
            running={start.key: start for start in state.running},
            accepted=list(state.accepted),
            parents=state.parents,
            withheld=state.withheld,
            refunded=state.refunded,
            unreachable=list(state.unreachable),
            gate=PlannerGate(
                failed=state.gate_failed,
                blocked_at_done=state.gate_blocked_at_done,
                last_error=state.gate_last_error,
            ),
            calls=state.calls,
            handoffs=state.handoffs,
            kept=dict(state.kept),
            main_seen=dict(state.main_seen),
            submitted={item.key: item for item in state.submitted},
            input_overdue=state.input_overdue,
            planned_by=dict(state.planned_by),
        )
        record.main = MainLine.resumed(state.baseline, state.main)
        record.outline = state.outline
        record.evaluations = EvaluationQueue.resumed(config.slots, config.pick, state.evaluations)
        return record

    def decided_verdicts(self) -> list[tuple[str, SlotVerdict]]:
        """The verdict already decided for each running workstream that submitted.

        A restart empties every latch, so the resumed root hands these again: a resumed
        workstream that waits for its verdict reads it, and one past it never reads it.
        """
        queue = self.evaluations
        dropped = {entry.submission.key: verdict for entry, verdict in queue.dropped}
        decided: list[tuple[str, SlotVerdict]] = []
        for key in self.running:
            if key in queue.holding:
                decided.append((key, Bound()))
            elif key in dropped:
                decided.append((key, dropped[key]))
        return decided

    # -- the plan the run follows --------------------------------------------------------------

    @property
    def needs_outline(self) -> bool:
        """Whether the run follows a plan whose steps are still to be outlined."""
        return self.config.follows_plan and self.outline is None

    def step_states(self) -> dict[str, StepState]:
        """Each step's state, by id; empty while the run explores or has no outline yet."""
        if self.outline is None:
            return {}
        running = {start.plan.work_id for start in self.running.values()}
        return step_states(self.outline, self.finished_work, self.hypotheses, running)

    def following(self) -> bool:
        """Whether the plan decides what runs: the run follows one and a step is not done."""
        if not self.config.follows_plan:
            return False
        states = self.step_states()
        return not states or any(state not in DONE for state in states.values())

    def repairable(self) -> frozenset[str] | None:
        """While the plan decides, the failed steps the planner may continue; else None (any)."""
        if not self.following():
            return None
        states = self.step_states()
        return frozenset(key for key, state in states.items() if state is StepState.FAILED)

    def _due_steps(self, room: int) -> list[WorkPlan]:
        """Plans for the ready steps, in declared order, that fit in ``room`` slots."""
        if self.outline is None or room <= 0:
            return []
        queued = {plan.work_id for plan in self.accepted}
        ready = [s for s in ready_steps(self.outline, self.step_states()) if s.id not in queued]
        return [step_plan(step) for step in ready[:room]]

    # -- what the run has left -----------------------------------------------------------------

    @property
    def started(self) -> int:
        """Workstreams ever scheduled: the finished ones and the ones still running."""
        return len(self.finished_work) + len(self.running)

    @property
    def parked(self) -> frozenset[str]:
        """Running workstreams that wait on their trusted measurement without an agent slot.

        These are the earliest submitted, up to the run's backlog; any later one keeps its slot.
        """
        return frozenset(list(self.submitted)[: self.config.backlog])

    @property
    def working(self) -> tuple[WorkstreamStart, ...]:
        """Running workstreams that hold an agent slot: not parked on their measurement."""
        parked = self.parked
        return tuple(start for key, start in self.running.items() if key not in parked)

    def free_agents(self) -> int:
        """Agent slots no workstream works in."""
        return max(self.config.max_in_flight - len(self.working), 0)

    def capacity(self) -> int:
        """Free slots the next plan may fill, bounded by the remaining budget."""
        left = remaining(self.config, started=self.started, refunded=self.refunded)
        return free_slots(self.config, active=len(self.working), left=left)

    def may_plan(self, *, stopping: bool) -> bool:
        """Whether a planning call should start: a slot is free and planning is not held off."""
        gate = self.gate
        if stopping or gate.failed or not self.capacity():
            return False
        if self.following() and not self.needs_outline and not self.repairable():
            return False  # the plan says what runs; only a failed step needs the planner
        return gate.blocked_at_done is None or len(self.finished_work) > gate.blocked_at_done

    def over(self, *, planning: bool, measuring: bool, stopping: bool) -> bool:
        """Whether nothing is in flight and nothing more will be scheduled."""
        if self.running or planning or measuring or self.accepted:
            return False
        left = remaining(self.config, started=self.started, refunded=self.refunded)
        return stopping or self.gate.failed or left == 0

    # -- planning ------------------------------------------------------------------------------

    def next_call(self) -> str:
        """Begin a planning call; its key."""
        self.calls += 1
        self.gate.blocked_at_done = None
        return planning_key(self.calls)

    def offered(self) -> tuple[ParentOption, ...]:
        """The parents the planner may choose from now."""
        return offered_parents(self.parents, self.withheld)

    # -- the input's measurement ---------------------------------------------------------------

    def measured(self, baseline: candidates.Baseline) -> None:
        """Fold the input's measurement, which planning did not wait for.

        An input that failed its measurement drops the plans accepted meanwhile and not yet
        started, since they were made without knowing what the benchmark requires: the planner is
        asked again with the failure in view (`planned` drops a blind plan that arrives later).
        Workstreams already running go on: their work is paid for, and their own measurement
        shows them the failure. Either way a call that ended empty is asked again.
        """
        self.baseline = baseline
        self.gate.blocked_at_done = None
        self.evaluations.release(INPUT_KEY)
        if not candidates.trusted(baseline):
            self.accepted = []
        self.main.baseline = baseline

    def _merge_ids(self) -> frozenset[str]:
        """The IDs merges were scheduled under, finished or running."""
        work = (*self.finished_work, *self.running.values())
        return frozenset(item.plan.work_id for item in work if item.plan.kind is WorkKind.MERGE)

    # -- the trusted evaluations ---------------------------------------------------------------

    def submit(self, submission: Submission) -> None:
        """A workstream's candidate waits for its trusted measurement.

        Within the backlog its workstream is parked, and its agent slot is free.

        A key submits one candidate per run. Its verdict is set on a latch that is set once per
        run (``VERDICTS.of(key)``). A workstream resumed after a code change submits the same
        candidate again (the report it sent may have been lost with the restart): that is the
        submission already held, and the resumed root hands its verdict again
        (`decided_verdicts`). Another candidate under the same key is a bug.
        """
        known = self.submitted.get(submission.key)
        if known is not None:
            if known.model_copy(update={"at": submission.at}) == submission:
                return
            msg = f"workstream {submission.key!r} submitted twice: its verdict latch is already set"
            raise RuntimeError(msg)
        self.submitted[submission.key] = submission
        self.evaluations.submit(submission)
        if submission.key in self.parked:
            self.gate.blocked_at_done = None  # its agent slot is free: planning may fill it

    def released(self, key: str) -> None:
        """The trusted measurement of ``key`` no longer holds its slot."""
        self.evaluations.release(key)

    def verdicts(self, *, stopping: bool) -> list[tuple[str, SlotVerdict]]:
        """Bind the waiting candidates to the free slots: the verdicts for their workstreams.

        After a stop nothing is bound: every waiting candidate is dropped as stopped, so its
        workstream keeps its reviewed code as work in progress.
        """
        if stopping:
            self.evaluations.stop()
        else:
            self.evaluations.bind()
        return self.evaluations.verdicts()

    def overdue(self, report: InputOverdue) -> None:
        """A workstream waited too long for the input: the orchestrator is told on its next turn."""
        if self.baseline is None and self.input_overdue is None:
            self.input_overdue = report
            self.gate.blocked_at_done = None

    def world(self, now: float | None = None) -> PlanWorld:
        """What a planning call reads of the run at ``now``, on the run clock.

        Without ``now`` the request leaves out how long each candidate has waited.
        """
        offered = self.offered()
        return PlanWorld(
            config=self.config,
            base=self.base,
            baseline=self.baseline,
            hypotheses=tuple(self.hypotheses.values()),
            running=tuple(self.running.values()),
            finished=tuple(self.finished_work),
            refunded=self.refunded,
            offered=offered,
            handoffs=tuple(reversed(self.handoffs)),
            main=self.main.revision,
            main_measurement=self.main.measurement,
            merges=tuple(self.main.merges),
            merge_options=self.main.options(offered, self.terms, self._merge_ids()),
            offer=ProfileOffer(
                fields=tuple(ProfileField(item) for item in self.terms.profile_fields)
            )
            if self.config.profiling
            else None,
            repairable=self.repairable(),
            parked=self.parked,
            evaluations=self._queue_view(now),
            input_overdue=self._overdue_view(now),
            space=candidates.space_of(self.terms),
        )

    def _queue_view(self, now: float | None) -> EvaluationQueueView | None:
        """The evaluation queue as the orchestrator is shown it; None before anything queued."""
        queue = self.evaluations
        if not queue.received:
            return None

        def waited(at: float) -> int | None:
            return None if now is None else max(int(now - at), 0)

        return EvaluationQueueView(
            pick=queue.policy,
            slots=queue.slots,
            input_holds_slot=INPUT_KEY in queue.holding,
            measuring=tuple(
                self._queued_view(self.submitted[key], None)
                for key in queue.holding
                if key in self.submitted
            ),
            waiting=tuple(
                self._queued_view(item.submission, waited(item.submission.at))
                for item in queue.in_order()
            ),
            dropped=tuple(
                DroppedEvaluation(
                    work_id=item.submission.work_id,
                    revision=item.submission.candidate,
                    reason=verdict.reason,
                    superseded_by=verdict.by,
                )
                for item, verdict in queue.dropped[-_DROPPED_SHOWN:]
            ),
            ranking=queue.ranking,
        )

    @staticmethod
    def _queued_view(submission: Submission, waited: int | None) -> QueuedEvaluation:
        return QueuedEvaluation(
            work_id=submission.work_id,
            kind=submission.kind,
            revision=submission.candidate,
            waited_seconds=waited,
        )

    def _overdue_view(self, now: float | None) -> InputOverdueView | None:
        report = self.input_overdue
        if report is None or self.baseline is not None:
            return None
        return InputOverdueView(
            waited_seconds=None if now is None else max(int(now - report.since), 0),
            limit_seconds=int(report.limit),
        )

    def planned(self, outcome: PlanOutcome) -> None:
        """Fold a finished planning call: park or abandon what it decided, queue what it accepted.

        A plan made before the input's measurement landed is dropped when the input turned out
        untrusted (see `measured`); planning is asked again.
        """
        match outcome:
            case Scheduled(blind=True, handoff=handoff) if self._input_failed():
                if handoff is not None:
                    self.handoffs = handoff.listed
                self.gate.blocked_at_done = None
            case Outlined(steps=steps, outliner=outliner):
                self.outline = steps
                self.gate.last_error = None
                if outliner is not None:
                    self.planned_by.update((step.id, outliner) for step in steps)
            case Scheduled() as scheduled:
                self._scheduled(scheduled)
            case NoPlan(problem=problem, stuck=stuck, blocked_at_done=at_done):
                self.gate = PlannerGate(failed=stuck, blocked_at_done=at_done, last_error=problem)

    def _scheduled(self, scheduled: Scheduled) -> None:
        """Fold a planning call that scheduled a plan: queue what it accepted, and who planned it."""
        check = scheduled.check
        if scheduled.handoff is not None:
            self.handoffs = scheduled.handoff.listed
        if scheduled.planner is not None:
            self.planned_by.update((plan.work_id, scheduled.planner) for plan in check.accepted)
        for change in check.updates:
            known = self.hypotheses[change.hypothesis_id]
            self.hypotheses[change.hypothesis_id] = _hypotheses.with_disposition(known, change)
        self.gate.last_error = None
        self.gate.blocked_at_done = None
        self.accepted.extend(self._as_steps(check.accepted))
        if check.ranking is not None:
            self.evaluations.rank(check.ranking)

    def _as_steps(self, plans: tuple[WorkPlan, ...]) -> list[WorkPlan]:
        """The planner's plans, each repair of a plan step carrying the step it repairs."""
        steps = {step.id: step for step in self.outline or ()}
        return [
            plan.model_copy(update={"step": steps[plan.work_id]})
            if plan.work_id in steps and plan.step is None
            else plan
            for plan in plans
        ]

    def _input_failed(self) -> bool:
        return self.baseline is not None and not candidates.trusted(self.baseline)

    # -- workstreams ---------------------------------------------------------------------------

    def parent_of(self, plan: WorkPlan) -> tuple[RevisionRef, str | None]:
        """The revision a plan builds on and the hypothesis it descends from, frozen at scheduling.

        A hypothesis that goes on builds on its own last candidate, else on the parent it had; any
        other parent is resolved only from what was offered, and falls back to the run's main.
        """
        continued = self._own_last_round(plan)
        if continued is not None:
            return continued
        if plan.step is not None and not plan.continue_hypothesis:
            found_step = built_on(plan.step, self.finished_work, self.hypotheses)
            return found_step if found_step is not None else (self.base, None)
        if plan.parent_hypothesis_id is not None:
            found = resolve(self.parents, plan.parent_hypothesis_id, plan.parent_revision)
            if found is not None:
                return found.revision, plan.parent_hypothesis_id
        return self.main.revision, None

    def _own_last_round(self, plan: WorkPlan) -> tuple[RevisionRef, str | None] | None:
        """What a continuation builds on: its last round's candidate.

        A hypothesis whose candidate waits for its evaluation is still in flight, so it is never
        continued before that round is recorded (`PlanLedger.submitted`).
        """
        prior = self.hypotheses.get(plan.work_id)
        if plan.kind is not WorkKind.IMPLEMENT or not plan.continue_hypothesis or prior is None:
            return None
        last = prior.rounds[-1] if prior.rounds else None
        if last is not None and last.candidate is not None:
            return last.candidate, prior.lineage_parent_id
        earlier = [item for item in self.finished_work if item.plan.work_id == plan.work_id]
        return (earlier[-1].parent, earlier[-1].parent_hypothesis_id) if earlier else None

    def ready_to_start(self, free: int) -> list[WorkstreamStart]:
        """A workstream for each accepted plan that fits in ``free`` slots, now running.

        A workstream may start before the input is measured; what in it compares a candidate
        with the input waits for the measurement (`_measure.the_input`).
        """
        starts = []
        if self.following():
            room = min(free, self.capacity()) - len(self.accepted)
            self.accepted = [*self._due_steps(room), *self.accepted]
        ready, self.accepted = self.accepted[:free], self.accepted[free:]
        for plan in ready:
            sequence = self.started + 1
            parent, lineage = self.parent_of(plan)
            record = self.hypotheses.get(plan.work_id)
            prior = record.rounds[-1] if record is not None and record.rounds else None
            rebase = self._rebase_onto(plan)
            parts = self._parts(plan)
            if plan.kind is WorkKind.IMPLEMENT:
                self.hypotheses[plan.work_id] = _hypotheses.scheduled(record, plan, sequence, parts)
                self.main_seen[plan.work_id] = self.main.generation
            start = WorkstreamStart(
                key=workstream_key(sequence, plan.work_id),
                sequence=sequence,
                plan=plan,
                parent=parent,
                parent_hypothesis_id=lineage,
                prior=prior,
                config=self.config,
                baseline=self.baseline,
                merge=self._sources(plan),
                combine=parts,
                rebase_onto=rebase,
                started_by=_started_by(self.planned_by.get(plan.work_id)),
            )
            self.running[start.key] = start
            starts.append(start)
        return starts

    def _rebase_onto(self, plan: WorkPlan) -> RevisionRef | None:
        """Main, when a hypothesis that goes on last started before main moved; else None."""
        if self._own_last_round(plan) is None:
            return None
        seen = self.main_seen.get(plan.work_id, 0)
        return self.main.revision if self.main.generation > seen else None

    def _sources(self, plan: WorkPlan) -> tuple[MergeSource, ...]:
        """A merge's candidates as the exact revisions offered now, in the planner's order."""
        found = (resolve(self.parents, item, None) for item in plan.candidates)
        return tuple(
            MergeSource(hypothesis_id=item, revision=option.revision)
            for item, option in zip(plan.candidates, found, strict=True)
            if option is not None
        )

    def _parts(self, plan: WorkPlan) -> tuple[CombineSource, ...]:
        """A combine's parts as the exact revisions offered now, each with its own change.

        A part whose revision is no longer offered (its verification failed since) is left out,
        and the implementer is told so. A part's change starts where its hypothesis started:
        the parent of the hypothesis's first workstream.
        """
        offered = {
            (item.snapshot.hypothesis_id, item.snapshot.revision.revision_id.root): item.snapshot
            for item in self.offered()
        }
        parts: list[CombineSource] = []
        for pick in plan.combine:
            found = offered.get((pick.hypothesis_id, pick.revision))
            if found is None:
                continue
            first = next(
                (item for item in self.finished_work if item.plan.work_id == pick.hypothesis_id),
                None,
            )
            parts.append(
                CombineSource(
                    hypothesis_id=pick.hypothesis_id,
                    revision=found.revision,
                    built_on=self.base if first is None else first.parent,
                    metric=None if found.benchmark is None else found.benchmark.headline(),
                )
            )
        return tuple(parts)

    def finished(self, done: WorkstreamDone) -> None:
        """Keep what a finished workstream leaves behind: its record, round and parent."""
        start = done.start
        del self.running[start.key]
        self.submitted.pop(start.key, None)
        self.evaluations.forget(start.key)
        profile = start.plan.kind is WorkKind.PROFILE
        self.finished_work.append(
            FinishedWork(
                sequence=start.sequence,
                plan=start.plan,
                parent=start.parent,
                parent_hypothesis_id=start.parent_hypothesis_id,
                failure=done.failure,
                readings=done.evidence.readings if profile else (),
                proof=done.evidence.proof if profile else None,
            )
        )
        self.refunded += int(done.refunded)
        if done.withheld is not None:
            root = done.withheld.revision_id.root
            if root not in self.withheld:
                self.withheld = (*self.withheld, root)
            self.gate.blocked_at_done = None
        if done.unreachable is not None:
            self.unreachable.append(done.unreachable)
        current = self.hypotheses.get(start.plan.work_id)
        if current is not None and not profile and done.settlement is not None:
            self.hypotheses[start.plan.work_id] = _hypotheses.with_round(current, done)
        if start.plan.kind is WorkKind.MERGE:
            self.main.finished(done, self.terms)
        self._offer_as_parent(done)
        self._keep(done)

    def _offer_as_parent(self, done: WorkstreamDone) -> None:
        snapshot = snapshot_of(done)
        if snapshot is None:
            return
        try:
            self.parents = ingest(self.parents, snapshot)
        except ParentConflictError:
            return  # a conflicting receipt is never offered

    def _keep(self, done: WorkstreamDone) -> None:
        settlement = done.settlement
        if settlement is None or not settlement.eligible:
            return
        retained = Kept.of(settlement)
        if retained is not None:
            self.kept[settlement.settlement_id.root] = retained

    def stopped_before_start(self, key: str) -> None:
        """A task the stop ended before it had anything to settle.

        That is a workstream before it had a workspace, a planning call, or the input's measurement.
        A planning call leaves nothing to record: after a stop nothing it planned would start.
        The plans accepted and not yet started are dropped with the input's measurement.
        """
        if key == INPUT_KEY:
            self.accepted = []
            self.evaluations.release(INPUT_KEY)
        elif key in self.running:
            self.finished(WorkstreamDone.stopped(self.running[key]))

    # -- the winner ----------------------------------------------------------------------------

    def eligible_rounds(self) -> tuple[candidates.EligibleRound, ...]:
        """Every winner-eligible round of the run, merged mains included."""
        merged = tuple(item for item in self.main.rounds if item.settlement.root in self.kept)
        return merged + tuple(
            candidates.EligibleRound(
                hypothesis_id=hypothesis.hypothesis_id,
                sequence=row.sequence,
                settlement=row.settlement,
                candidate=row.candidate,
                metrics=row.metrics,
            )
            for hypothesis in self.hypotheses.values()
            for row in hypothesis.rounds
            if row.eligible
            and row.candidate is not None
            and row.settlement is not None
            and row.settlement.root in self.kept
        )

    def winner(self) -> candidates.Winner:
        """The candidate that leads now, or the trusted input."""
        return candidates.choose_winner(
            self.eligible_rounds(),
            space=candidates.space_of(self.terms),
            baseline=self.base,
            when_unranked=candidates.Unranked.LATEST,
        )

    def leader(self) -> Kept | Input:
        """The candidate that leads now, else the input: what the run would adopt if it ended."""
        return self.leading()[0]

    def leading(self) -> tuple[Kept | Input, Label]:
        """The leader, with the label of the span that adopts it (``ctx.choose``'s arguments)."""
        winner = self.winner()
        base = candidates.input_value(self.baseline)
        found = None if winner.settlement is None else self.kept.get(winner.settlement.root)
        if found is None:
            return INPUT, adoption_label(INPUT, base, base)
        return found, adoption_label(found, winner.value, base)

    def ending(self) -> NoWinner | None:
        """None when the run ends with a winner or with the input standing; else why it has none."""
        if self.eligible_rounds() or self.baseline is None:
            return None  # a stop before the input was measured leaves the input standing
        trusted = candidates.trusted(self.baseline)
        if self.gate.failed:
            return NoWinner(reason=self._no_winner_reason(trusted=trusted))
        return None if trusted else NoWinner(reason=self._no_winner_reason(trusted=False))

    def _no_winner_reason(self, *, trusted: bool) -> str:
        problem = self.gate.last_error
        asked = problem.kind.value if problem is not None else ""
        if trusted or self.baseline is None:
            return asked or "no candidate could be adopted"
        why = candidates.failure(self.baseline)
        detail = why.kind.value if why is not None else "the input did not pass its measurement"
        if asked:
            detail = f"{detail}; the planner's last reply: {asked}"
        return candidates.ending_reason(RunEnding.NO_TRUSTED_RESULT, detail)

    # -- the record ----------------------------------------------------------------------------

    def snapshot(self) -> RunSnapshot:
        """The record as it is published."""
        return RunSnapshot(
            baseline=self.baseline,
            hypotheses=tuple(self.hypotheses.values()),
            finished=tuple(self.finished_work),
            running=tuple(
                Running(sequence=r.sequence, plan=r.plan, parent=r.parent)
                for r in self.running.values()
            ),
            parents=self.parents,
            unreachable=tuple(self.unreachable),
            leading=self.winner().hypothesis_id,
            main=self.main.revision,
            main_measurement=self.main.measurement,
            merges=tuple(self.main.merges),
            steps=self.outline or (),
            step_states=tuple(self.step_states().items()),
            evaluations=self.evaluations.snapshot(),
            input_overdue=self.input_overdue,
        )


def _started_by(planner: str | None) -> tuple[str, ...]:
    """The agents that start a plan's workstream: the one that planned it, when known."""
    return () if planner is None else (planner,)
