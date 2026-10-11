"""What a strategy must satisfy to run on the vs-core lifecycle (design note section 9.2).

A `Subject` describes one strategy to the suite: how to build a fresh instance, the harness
that hosts it, and a generator of scripted worlds (what the planner, agents and evaluators
answer). The properties in this module (`check_*`) are written against that description
only, so a ported strategy registers one `Subject` in `subjects.py` and inherits every
property. Nothing here names a concrete strategy.

The pure tier uses no Fakes, clocks or asyncio: `Tape` records every `decide` and `on_event`
call of a run on the production shell's scripted executors, and the properties are
evaluated over the recorded (state, view) pairs. The crash tier lives in `_crash.py`.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from vs_core.api import (
    ENVELOPE_SCHEMA_VERSION,
    AdoptRevision,
    ControlId,
    ControlInput,
    DispatchTurn,
    EventCursor,
    HostFence,
    HostId,
    MeasurementFailure,
    ObserveOwnedJob,
    OwnedJob,
    Proposal,
    ProposeWinner,
    Rejected,
    RejectionCode,
    ResourceId,
    ResumeSessionTurn,
    RetainedCandidate,
    RunControlEvent,
    RunEnvelope,
    RunResultProposal,
    SubmitMeasurement,
    TrustedBaseline,
    VerifyAdoption,
    project,
)
from vs_core.testing.drive import Failed, Faults, Retryable, Succeeded, Unknown, drive
from vs_core.testing.liveness import Budget, End, Journal, assert_live
from vs_runtime.api.testing import HostOptions, Run, drive_shell

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from hypothesis.strategies import SearchStrategy
    from pydantic import BaseModel

    from vs_core.api import (
        CoreState,
        Decision,
        Request,
        RevisionRef,
        RunView,
        SchemaRef,
        Strategy,
        StrategyEvent,
        StrategyState,
    )
    from vs_core.testing.drive import Answer, Harness
    from vs_runtime.api.core import CoreRuntime


class World(Protocol):
    """The far side of a run: executors that answer every request, and what they measured."""

    def __call__(self, request: Request, core: CoreState) -> Answer:
        """What the executors report for ``request``."""
        ...

    def headline(self, commit: str) -> float | None:
        """The trusted headline metric (larger is better) of a candidate commit, if measured."""
        ...


@dataclass(frozen=True)
class Subject:
    """One strategy as the conformance suite sees it."""

    name: str
    new_strategy: Callable[[], Strategy[Any]]
    """A fresh strategy instance over its initial state; every call builds an equal one."""
    harness: Harness
    """Registry, run facts, limits, envelope type and evidence requirements of the run."""
    schemas: Mapping[SchemaRef, type[BaseModel]]
    """The reply types the run's turns answer."""
    worlds: SearchStrategy[Callable[[], World]]
    """Factories of fresh scripted worlds: agents reply, measurements succeed or fail."""
    default_world: Callable[[], World]
    """A fixed world in which the strategy runs its full course and adopts a candidate."""
    smallest_world: Callable[[], World]
    """The shortest full run (fewest durable commits), which the exhaustive crash sweep uses."""
    issued: frozenset[str]
    """Request kinds the strategy's own decisions cause in a run that has no fault."""
    sweeps_crashes: bool = True
    """Whether the exhaustive crash sweep runs on this subject (one run per durable boundary)."""
    ci_stride: int = 1
    """Every n-th boundary of a subject with `sweeps_crashes` False is swept in CI by
    `test_conformance_crash_sampled.py`; the slow sweep there covers every boundary. A subject
    whose run has so many commits that the exhaustive sweep would not fit CI's budget uses it."""


# Kinds core issues only to recover from a fault, to cancel, or to resume a suspended turn.
RECOVERY_ONLY = frozenset(
    {
        "inspect_request",
        "inspect_turn",
        "inspect_owned_job",
        "cancel_turn",
        "cancel_owned_job",
        "cancel_owned_resource",
        "block_intent",
        "resume_session_turn",
        "collect_evidence",
        "snapshot_and_retain_run",
        "restore_revision",
    }
)


# -- the tape -----------------------------------------------------------------------------


@dataclass
class Tape:
    """Every call a run made on its strategy, with the state and view it was made on."""

    decided: list[tuple[StrategyState, RunView, Proposal[Any]]] = field(default_factory=list)
    folded: list[tuple[StrategyState, RunView, StrategyEvent, StrategyState]] = field(
        default_factory=list
    )


class Taped[S: StrategyState]:
    """A strategy that records its calls on a `Tape` and otherwise behaves as its inner one."""

    def __init__(self, inner: Strategy[S], tape: Tape) -> None:
        """Wrap ``inner``; every binding shares one tape."""
        self._inner = inner
        self._tape = tape
        self.declaration = inner.declaration

    @property
    def state(self) -> S:
        """The wrapped strategy's state."""
        return self._inner.state

    def bind(self, state: S) -> Taped[S]:
        """The same strategy over another state."""
        return Taped(self._inner.bind(state), self._tape)

    def decide(self, view: RunView) -> Proposal[S]:
        """The inner proposal, recorded with the state and view it was made on."""
        proposal = self._inner.decide(view)
        self._tape.decided.append((self._inner.state, view, proposal))
        return proposal

    def on_event(self, view: RunView, event: StrategyEvent) -> S:
        """The inner fold, recorded."""
        folded = self._inner.on_event(view, event)
        self._tape.folded.append((self._inner.state, view, event, folded))
        return folded


# -- generated faults over any world --------------------------------------------------------


@dataclass(frozen=True)
class Outcomes:
    """How each measurement submission and agent turn of a run goes, in order of occurrence.

    The last outcome of a list repeats; an empty list means every one succeeds.
    """

    measurements: tuple[str, ...] = ()
    turns: tuple[str, ...] = ()
    adoption_fails: bool = False


MEASUREMENT_OUTCOMES = ("succeeded", "workload_failure", "infrastructure_failure")
TURN_OUTCOMES = ("replies", "unparsable")
LOST_MEASUREMENT = "lost"
LOST_TURN = "lost"
RETRYABLE_TURN = "retryable_failure"


def _nth(outcomes: tuple[str, ...], position: int, default: str) -> str:
    return default if not outcomes else outcomes[min(position, len(outcomes) - 1)]


class Faulted:
    """A world whose measurements and turns go the way `Outcomes` says, over a base world.

    With ``stop_at`` the operator's stop arrives while the ``stop_at``-th request executes,
    the way a control arrives mid-run in production (the shell admits it between steps).
    """

    def attach(self, shell: CoreRuntime[Any], clock: object) -> None:
        """Hear the shell once it exists: the operator's stop is submitted to it, as a control is."""
        del clock
        self._shell = shell

    def __init__(self, base: World, outcomes: Outcomes, *, stop_at: int | None = None) -> None:
        """Layer ``outcomes`` over ``base``."""
        self._base = base
        self._outcomes = outcomes
        self._stop_at = stop_at
        self._shell: CoreRuntime[Any] | None = None
        self._requests = 0
        self._submitted = 0
        self._asked = 0
        self._infrastructure: set[str] = set()
        self.stopped = False
        self.executed: Counter[str] = Counter()

    def headline(self, commit: str) -> float | None:
        """The base world's headline."""
        return self._base.headline(commit)

    def __call__(self, request: Request, core: CoreState) -> Answer:
        """Answer from the generated outcome when there is one, else from the base world."""
        if request.request_id is not None:
            self.executed[request.request_id.root] += 1
        self._maybe_stop(core)
        faulted = (
            self._measurement(request)
            or self._job(request, core)
            or self._turn(request)
            or self._adoption(request)
        )
        return self._base(request, core) if faulted is None else faulted

    def _maybe_stop(self, core: CoreState) -> None:
        position, self._requests = self._requests, self._requests + 1
        if self._stop_at != position or self._shell is None:
            return
        self.stopped = True
        self._shell.submit(
            RunControlEvent(
                control=ControlInput(control_id=ControlId(root="stop"), action="stop"),
                now_at=core.run.now_at,
                result=RunResultProposal(outcome="cancelled", reason="operator stop"),
            ),
            now_at=core.run.now_at,
        )

    def _measurement(self, request: Request) -> Answer | None:
        if not isinstance(request, SubmitMeasurement):
            return None
        outcome = _nth(self._outcomes.measurements, self._submitted, "succeeded")
        self._submitted += 1
        if outcome == "infrastructure_failure":
            self._infrastructure.add(request.plan.model_dump_json())
        return {
            "workload_failure": Failed(MeasurementFailure.WORKLOAD),
            LOST_MEASUREMENT: Unknown(),
        }.get(outcome)

    def _job(self, request: Request, core: CoreState) -> Answer | None:
        if not isinstance(request, ObserveOwnedJob):
            return None
        job = next(
            row
            for row in core.evaluation.jobs
            if isinstance(row, OwnedJob) and row.resource_id == request.resource_id
        )
        return Failed(accepted=True) if job.plan.model_dump_json() in self._infrastructure else None

    def _turn(self, request: Request) -> Answer | None:
        if not isinstance(request, DispatchTurn | ResumeSessionTurn):
            return None
        outcome = _nth(self._outcomes.turns, self._asked, "replies")
        self._asked += 1
        lease = ResourceId(root=f"lease:{request.turn.session.session_id.root}")
        return {
            "unparsable": Succeeded(output_json="not json", resource_id=lease),
            RETRYABLE_TURN: Retryable(),
            LOST_TURN: Unknown(),
        }.get(outcome)

    def _adoption(self, request: Request) -> Answer | None:
        if self._outcomes.adoption_fails and isinstance(request, AdoptRevision | VerifyAdoption):
            return Failed()
        return None


# -- running a subject ------------------------------------------------------------------------


@dataclass
class Recorded:
    """One finished run and the tape of the strategy calls it made."""

    run: Run
    tape: Tape
    world: World


def run_subject(
    subject: Subject,
    world: World,
    *,
    host: HostOptions | None = None,
    live: bool = True,
) -> Recorded:
    """Run a fresh strategy of ``subject`` to the end of its run on the production shell.

    The run must satisfy the liveness invariants unless ``live`` is False.
    """
    tape = Tape()
    finished = drive_shell(
        Taped(subject.new_strategy(), tape),
        world,
        subject.harness,
        subject.schemas,
        host,
    )
    if live:
        assert_live(
            finished.journal, finished.core, Budget(retries=subject.harness.limits.max_retries)
        )
    if finished.halted is not None:
        raise finished.halted
    return Recorded(finished, tape, world)


# -- properties ---------------------------------------------------------------------------------


def check_decide_is_deterministic(subject: Subject, recorded: Recorded) -> None:
    """C1: the same state and view always propose the same decisions and the same next state.

    A fresh instance is bound to the recorded state, so nothing outside the state and the
    view (a counter, a cache, a clock) can have influenced the proposal.
    """
    for state, view, proposal in recorded.tape.decided:
        again = subject.new_strategy().bind(state).decide(view)
        assert again == proposal, "decide is not a function of (state, view)"


def check_a_replayed_proposal_is_deduplicated(subject: Subject, recorded: Recorded) -> None:
    """C1: re-proposing after the commit repeats ids with the payloads they had.

    After a crash the strategy is rebound to the committed state and asked again on the
    current view. A decision it repeats must carry the identity and payload it had, because
    core deduplicates by both and refuses the same identity with another payload.
    """
    seen: dict[str, str] = {}
    for _, _, proposal in recorded.tape.decided:
        for decision in proposal.decisions:
            payload = decision.model_dump_json()
            first = seen.setdefault(decision.decision_id.root, payload)
            assert first == payload, f"decision {decision.decision_id.root} changed its payload"
    for _, view, proposal in recorded.tape.decided:
        replay = subject.new_strategy().bind(proposal.state).decide(view)
        for decision in replay.decisions:
            known = seen.get(decision.decision_id.root)
            if known is not None:
                assert known == decision.model_dump_json(), (
                    f"replay of {decision.decision_id.root} changed its payload"
                )


def check_states_round_trip_the_codec(subject: Subject, recorded: Recorded) -> None:
    """C3: every state the run reached survives the envelope codec unchanged."""
    harness = subject.harness
    envelope_type = harness.envelope_type
    assert envelope_type is not None
    final = recorded.run.core
    seen: set[str] = set()
    states = [proposal.state for _, _, proposal in recorded.tape.decided]
    states += [folded for _, _, _, folded in recorded.tape.folded]
    for state in states:
        key = state.model_dump_json()
        if key in seen:
            continue
        seen.add(key)
        envelope = _envelope(subject, envelope_type, final, state)
        loaded = harness.registry.decode_envelope(
            envelope_type, harness.registry.encode_envelope(envelope)
        )
        assert loaded.strategy == state


def _envelope(
    subject: Subject,
    envelope_type: type[RunEnvelope[Any]],
    core: CoreState,
    state: StrategyState,
) -> RunEnvelope[Any]:
    declaration = subject.new_strategy().declaration
    return envelope_type(
        schema_version=ENVELOPE_SCHEMA_VERSION,
        fence=HostFence(host_id=HostId(root="conformance"), epoch=1),
        strategy_id=declaration.strategy_id,
        state_schema=declaration.state_schema,
        core=core,
        strategy=state,
        event_cursor=EventCursor(sequence=0),
    )


def check_a_folded_event_twice_changes_nothing(subject: Subject, recorded: Recorded) -> None:
    """C7: folding an event the state already absorbed again leaves the state as it is."""
    for _, view, event, folded in recorded.tape.folded:
        again = subject.new_strategy().bind(folded).on_event(view, event)
        assert again == folded, f"a duplicate {type(event).__name__} changed the state"


def check_limits_hold(subject: Subject, recorded: Recorded) -> None:
    """C6: the run never asked for more than its limits, and no decision was refused for budget."""
    limits = subject.harness.limits
    view = project(recorded.run.core)
    assert len(view.attempts) <= limits.max_attempts
    submissions = Counter(
        row.plan.model_dump_json()
        for row in recorded.run.core.evaluation.jobs
        if isinstance(row, OwnedJob)
    )
    assert all(count <= limits.max_measurement_submissions for count in submissions.values())
    refused = [
        event
        for _, _, event, _ in recorded.tape.folded
        if isinstance(event, Rejected) and event.code is RejectionCode.BUDGET
    ]
    assert not refused, f"the strategy asked beyond its budget: {refused[0].detail}"


def check_adoption_is_safe(recorded: Recorded) -> None:
    """C8: a proposed winner is an eligible retained candidate with the best headline.

    The oracle is independent of the strategy: eligibility and retention come from core's
    settlements and the ranking from the world's own measurements. A baseline selection is
    legal only when no candidate settled eligible.
    """
    core = recorded.run.core
    settlements = {row.settlement_id: row for row in core.settlement.settlements}
    eligible = [
        row for row in settlements.values() if row.eligible and row.retention == "candidate"
    ]
    for decision in _decisions(recorded.run):
        if not isinstance(decision, ProposeWinner):
            continue
        selection = decision.selection
        if isinstance(selection, TrustedBaseline):
            assert not eligible, "the baseline was selected while an eligible candidate existed"
            continue
        assert isinstance(selection, RetainedCandidate)
        chosen = settlements[selection.settlement_id]
        assert chosen.eligible, "an ineligible settlement was proposed"
        assert chosen.retention == "candidate", "a settlement that retained nothing was proposed"
        scores = {row.settlement_id: _headline(recorded.world, row.candidate) for row in eligible}
        best = max((score for score in scores.values() if score is not None), default=None)
        assert scores[chosen.settlement_id] == best, "a better eligible candidate existed"


def _headline(world: World, candidate: RevisionRef | None) -> float | None:
    return None if candidate is None else world.headline(candidate.revision_id.root)


def _decisions(run: Run) -> Sequence[Decision]:
    return run.decisions


def request_kinds(recorded: Recorded) -> Counter[str]:
    """How many requests of each kind the run issued, as core's ledger recorded them."""
    return Counter(row.request.kind for row in recorded.run.core.intents.intents)


def run_pure(
    subject: Subject, world: World, faults: Faults, *, end: End = End.CUT_SHORT
) -> list[str]:
    """Drive a fresh strategy on the pure driver under delivery ``faults``; its decision ids.

    The pure driver answers no agent turn (its sessions need the shell's owner events), so
    a world that reaches one is cut short there; the run must still be live up to it.
    """
    trace = drive(subject.new_strategy(), world, subject.harness, faults)
    assert_live(
        Journal.from_log(trace.log),
        trace.core,
        Budget(retries=subject.harness.limits.max_retries),
        end,
    )
    return [decision.decision_id.root for decision in trace.decisions]


# -- request-kind recovery matrix (C9) ----------------------------------------------------------

CONCLUSIVE_AFTER_FAULT = frozenset(
    {
        *(
            (kind, how)
            for kind in ("adopt_revision", "dispatch_turn", "submit_measurement")
            for how in ("unknown_once", "retryable_once", "silent")
        ),
        *(
            (kind, how)
            for kind in ("execute_registered_operation", "verify_adoption")
            for how in ("unknown_once", "retryable_once")
        ),
    }
)
"""Kind and answer pairs after which a run ends live, whatever strategy issues the kind.

For the other pairs core blocks the intent at its bound but routes the block to the strategy
only, so the attempt, session or settlement waiting on it never concludes and the run stays
open. That gap belongs to vs-core (see the dynamic recovery matrix); when it is fixed these
pairs join the set and every strategy is asked to survive them.
"""
KIND_FAULTS = ("unknown_once", "retryable_once", "silent")


class KindFault:
    """A world that answers the requests of one kind a faulty way and the rest as ``base``."""

    def __init__(self, base: World, kind: str, how: str) -> None:
        """Fault ``kind``: once (``unknown_once``, ``retryable_once``) or every time (``silent``)."""
        self._base = base
        self._kind = kind
        self._how = how
        self._seen = 0
        self.executed: Counter[str] = Counter()

    def headline(self, commit: str) -> float | None:
        """The base world's headline."""
        return self._base.headline(commit)

    def __call__(self, request: Request, core: CoreState) -> Answer:
        """The faulty answer for the chosen kind, else the base world's."""
        if request.request_id is not None:
            self.executed[request.request_id.root] += 1
        if request.kind == self._kind:
            position, self._seen = self._seen, self._seen + 1
            if self._how == "silent" or position == 0:
                return Retryable() if self._how == "retryable_once" else Unknown()
        return self._base(request, core)
