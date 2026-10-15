"""What the dynamic search knows of its run, and the plain decisions that follow from it.

`RunRecord` is the root flow's one piece of mutable state. The flows it starts report back
through `planned` and `finished`, which fold their results in; nothing else writes it. It holds
the facts (hypotheses, finished work, what is running, the parents on offer) and answers the
questions the root flow asks of them: may a plan start, is the search over, which plans become
workstreams, who is ahead. It reads nothing from the run and awaits nothing, so every rule here
is a plain function of its record. It is rebuilt by replay after a restart, so it is never saved.
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
from vibesys.orchestration.dynamic.search._main import MainLine
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
from vibesys.orchestration.dynamic.search._values import (
    FinishedWork,
    HypothesisRecord,
    MainMeasurement,
    MergeRecord,
    MergeSource,
    PlanProblem,
    Unreachable,
    WorkKind,
    WorkPlan,
)
from vs_core.api.search import INPUT, Input, Kept, NoWinner, RevisionRef, TaskTerms, Value


@dataclass(frozen=True)
class Scheduled:
    """A planning call ended with a plan to schedule, and the handoff file it left.

    ``blind`` says the plan answered a prompt asked before the input's measurement landed.
    """

    check: PlanCheck
    blind: bool = False
    handoff: HandoffWritten | None = None
    """The call's handoff file and every one the owner holds; None when its write failed."""


@dataclass(frozen=True)
class NoPlan:
    """A planning call ended with nothing valid.

    ``stuck`` says nothing is in flight to wait for, so planning has failed for good;
    ``blocked_at_done`` is the number of finished workstreams planning waits to exceed.
    """

    problem: PlanProblem | None
    stuck: bool
    blocked_at_done: int


type PlanOutcome = Scheduled | NoPlan


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

    def __post_init__(self) -> None:
        """Main starts at the run's input."""
        self.main = MainLine(revision=self.base, baseline=self.baseline)

    # -- what the run has left -----------------------------------------------------------------

    @property
    def started(self) -> int:
        """Workstreams ever scheduled: the finished ones and the ones still running."""
        return len(self.finished_work) + len(self.running)

    def capacity(self) -> int:
        """Free slots the next plan may fill, bounded by the remaining budget."""
        left = remaining(self.config, started=self.started, refunded=self.refunded)
        return free_slots(self.config, active=len(self.running), left=left)

    def may_plan(self, *, stopping: bool) -> bool:
        """Whether a planning call should start: a slot is free and planning is not held off."""
        gate = self.gate
        if stopping or gate.failed or not self.capacity():
            return False
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
        if not candidates.trusted(baseline):
            self.accepted = []
        self.main.baseline = baseline

    def _merge_ids(self) -> frozenset[str]:
        """The IDs merges were scheduled under, finished or running."""
        work = (*self.finished_work, *self.running.values())
        return frozenset(item.plan.work_id for item in work if item.plan.kind is WorkKind.MERGE)

    def world(self) -> PlanWorld:
        """What a planning call reads of the run now."""
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
            case Scheduled(check=check, handoff=handoff):
                if handoff is not None:
                    self.handoffs = handoff.listed
                for change in check.updates:
                    known = self.hypotheses[change.hypothesis_id]
                    self.hypotheses[change.hypothesis_id] = _hypotheses.with_disposition(
                        known, change
                    )
                self.gate.last_error = None
                self.gate.blocked_at_done = None
                self.accepted.extend(check.accepted)
            case NoPlan(problem=problem, stuck=stuck, blocked_at_done=at_done):
                self.gate = PlannerGate(failed=stuck, blocked_at_done=at_done, last_error=problem)

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
        if plan.parent_hypothesis_id is not None:
            found = resolve(self.parents, plan.parent_hypothesis_id, plan.parent_revision)
            if found is not None:
                return found.revision, plan.parent_hypothesis_id
        return self.main.revision, None

    def _own_last_round(self, plan: WorkPlan) -> tuple[RevisionRef, str | None] | None:
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
        ready, self.accepted = self.accepted[:free], self.accepted[free:]
        for plan in ready:
            sequence = self.started + 1
            parent, lineage = self.parent_of(plan)
            record = self.hypotheses.get(plan.work_id)
            prior = record.rounds[-1] if record is not None and record.rounds else None
            rebase = self._rebase_onto(plan)
            if plan.kind is WorkKind.IMPLEMENT:
                self.hypotheses[plan.work_id] = _hypotheses.scheduled(record, plan, sequence)
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
                rebase_onto=rebase,
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

    def finished(self, done: WorkstreamDone) -> None:
        """Keep what a finished workstream leaves behind: its record, round and parent."""
        start = done.start
        del self.running[start.key]
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
        winner = self.winner()
        if winner.settlement is None:
            return INPUT
        return self.kept.get(winner.settlement.root, INPUT)

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
        )
