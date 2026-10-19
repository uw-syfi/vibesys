"""The run record keeps the budget, the slots, the planning gate and the leader whatever happens.

A generated run plans, starts and ends workstreams in any order, with every ending the flow can
produce: failed, stopped before it started, never opened (refunded), parent unverifiable
(withheld), closed with a kept candidate that is or is not winner-eligible. The input's measurement
may land at any step, since planning does not wait for it. After every step the record's answers
must agree with the facts: the budget the config states, the slots free, when planning may start
again, when the search is over, that no workstream starts before the input is measured, who leads
and why a search may have no winner.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import TypeAdapter
from tests.vibesys.orchestration.dynamic._values import (
    config,
    evidence_of,
    revision,
    run_record,
    settlement_of,
)

from vibesys.candidates.api import (
    Baseline,
    InputFailure,
    InputFailureKind,
    InputReading,
    Measured,
    NotConfigured,
    Unmeasurable,
    trusted,
    uncompared,
)
from vibesys.hypothesis import HypothesisOutcome, HypothesisStrategy
from vibesys.orchestration.dynamic.models import StrategyReason
from vibesys.orchestration.dynamic.search.api import (
    Candidate,
    EvaluationQueue,
    Failure,
    FailureKind,
    HandoffNote,
    MainLine,
    MainState,
    NoPlan,
    PlanCheck,
    PlanUpdate,
    QueueState,
    RecordState,
    Result,
    RunRecord,
    RunSnapshot,
    Scheduled,
    WorkKind,
    WorkPlan,
    WorkstreamDone,
    WorkstreamStart,
)
from vs_core.api import INPUT, Kept, NoWinner, ObservationStatus, Uncompared
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from collections.abc import Callable

    from vibesys.orchestration.dynamic.search.api import DynamicConfig

type Ending = Literal["failed", "stopped", "refunded", "withheld", "ineligible", "eligible"]
ENDINGS: tuple[Ending, ...] = (
    *("failed", "stopped", "refunded", "withheld", "ineligible", "ineligible"),
    *("eligible", "eligible", "eligible"),
)

type Step = tuple[str, int]
WEIGHTED_KINDS = (
    *("plan", "plan", "start", "start", "end", "end", "end"),
    *("continue", "child", "retire", "no-plan", "measured"),
)
STEPS = st.lists(
    st.tuples(
        st.sampled_from(WEIGHTED_KINDS),
        st.integers(0, 9),
    ),
    min_size=10,
    max_size=60,
)


@dataclass
class Trace:
    """What the test itself saw happen, to compare the record with."""

    handed_out: list[WorkstreamStart] = field(default_factory=list)
    refunded: int = 0
    names: int = 0
    withheld: set[str] = field(default_factory=set)
    eligible_candidates: set[str] = field(default_factory=set)


def _fresh(trace: Trace, *, kind: WorkKind = WorkKind.IMPLEMENT, **changes: object) -> WorkPlan:
    trace.names += 1
    prefix = "p" if kind is WorkKind.PROFILE else "h"
    fields: dict[str, object] = {"kind": kind, "work_id": f"{prefix}{trace.names}", **changes}
    return WorkPlan.model_validate(fields)


def _done(run: RunRecord, start: WorkstreamStart, ending: Ending, trace: Trace) -> None:
    candidate = revision(f"c{start.sequence}")
    failure = Failure(kind=FailureKind.OUT_OF_BUDGET)
    match ending:
        case "stopped":
            run.stopped_before_start(start.key)
            return
        case "failed":
            done = WorkstreamDone(start, failure=failure)
        case "refunded":
            done = WorkstreamDone.not_opened(start, failure)
            trace.refunded += 1
        case "withheld":
            done = WorkstreamDone.not_opened(start, failure, withheld=start.parent)
            trace.refunded += 1
            trace.withheld.add(start.parent.revision_id.root)
        case _:
            eligible = ending == "eligible"
            if eligible:
                trace.eligible_candidates.add(candidate.revision_id.root)
            done = WorkstreamDone(
                start,
                failure=None,
                generation=0,
                settlement=settlement_of(
                    eligible=eligible,
                    retention="candidate",
                    candidate=candidate,
                    name=str(start.sequence),
                ),
                result=Result(
                    candidate=Candidate(
                        outcome=HypothesisOutcome.SUPPORTED, summary="done", revision=candidate
                    )
                ),
                evidence=evidence_of(
                    accuracy=True,
                    benchmark=True,
                    throughput=100.0 + start.sequence,
                    candidate=candidate,
                ),
            )
    run.finished(done)


def _check(run: RunRecord, trace: Trace) -> None:
    rules = run.config
    scheduled = len(trace.handed_out)
    assert run.started == scheduled == len(run.finished_work) + len(run.running)
    assert len(run.running) <= rules.max_in_flight
    assert scheduled - trace.refunded <= rules.start_budget
    # The run may schedule start_budget workstreams; a refund returns its slot.
    left = rules.start_budget - scheduled + trace.refunded
    assert run.capacity() == max(0, min(rules.max_in_flight - len(run.running), left))

    gate = run.gate
    assert not run.may_plan(stopping=True)
    if (
        gate.failed
        or run.capacity() == 0
        or (gate.blocked_at_done is not None and len(run.finished_work) <= gate.blocked_at_done)
    ):
        assert not run.may_plan(stopping=False)
    else:
        assert run.may_plan(stopping=False)

    for stopping in (True, False):
        assert not run.over(planning=True, measuring=False, stopping=stopping)
        assert not run.over(planning=False, measuring=True, stopping=stopping)
    if run.running or run.accepted:
        assert not run.over(planning=False, measuring=False, stopping=True)
        assert not run.over(planning=False, measuring=False, stopping=False)
    else:
        assert run.over(planning=False, measuring=False, stopping=True)
        over = run.over(planning=False, measuring=False, stopping=False)
        assert over == (gate.failed or left == 0)

    offered = {item.snapshot.revision.revision_id.root for item in run.offered()}
    assert not offered & trace.withheld
    sequences = [item.sequence for item in trace.handed_out]
    assert sequences == list(range(1, scheduled + 1))
    assert len({item.key for item in trace.handed_out}) == scheduled

    leader = run.leader()
    if leader is not INPUT:
        assert isinstance(leader, Kept)
        assert leader.revision.revision_id.root in trace.eligible_candidates
    assert (leader is INPUT) == (not run.eligible_rounds())
    if run.eligible_rounds():
        compared = run.baseline is None or not uncompared(run.baseline)
        assert run.ending() == (None if compared else Uncompared())
    snapshot = run.snapshot()
    assert RunSnapshot.model_validate_json(snapshot.model_dump_json()) == snapshot
    # resumed after a code change from what it checkpoints, the record is the same record
    state = run.state()
    assert RecordState.model_validate_json(state.model_dump_json()) == state
    resumed = RunRecord.resumed(run.config, run.base, run.terms, state)
    assert resumed.state() == state
    assert resumed.snapshot() == snapshot
    assert [item.plan.work_id for item in snapshot.running] == [
        item.plan.work_id for item in run.running.values()
    ]


def _plan(run: RunRecord, trace: Trace, pick: int) -> None:
    count = 1 + pick % max(1, run.capacity())
    plans = [
        _fresh(trace, kind=WorkKind.PROFILE if index % 4 == 3 else WorkKind.IMPLEMENT)
        for index in range(count)
    ]
    run.planned(Scheduled(PlanCheck(reasoning="r", handoff=HandoffNote(), accepted=tuple(plans))))


def _start(run: RunRecord, trace: Trace, _pick: int) -> None:
    free = run.config.max_in_flight - len(run.running)
    for begun in run.ready_to_start(max(free, 0)):
        trace.handed_out.append(begun)
        assert begun.key in run.running
        assert begun.config == run.config
        built_on = {run.base, *(item.revision for item in run.parents)}
        built_on |= {r.candidate for h in run.hypotheses.values() for r in h.rounds if r.candidate}
        assert begun.parent in built_on  # the input, an offered parent or its own earlier round
        assert begun.parent_hypothesis_id is None or begun.parent != run.base


def _end(run: RunRecord, trace: Trace, pick: int) -> None:
    if run.running:
        start = list(run.running.values())[pick % len(run.running)]
        _done(run, start, ENDINGS[pick % len(ENDINGS)], trace)


def _no_plan(run: RunRecord, _trace: Trace, pick: int) -> None:
    stuck = pick == 0 and not run.running and run.baseline is not None
    run.planned(NoPlan(problem=None, stuck=stuck, blocked_at_done=len(run.finished_work)))


def _continue(run: RunRecord, _trace: Trace, pick: int) -> None:
    busy = {r.plan.work_id for r in run.running.values()}
    again = [h for h in run.hypotheses.values() if h.rounds and h.hypothesis_id not in busy]
    if again:
        chosen = again[pick % len(again)].hypothesis_id
        run.accepted.append(
            WorkPlan(kind=WorkKind.IMPLEMENT, work_id=chosen, continue_hypothesis=True)
        )


def _child(run: RunRecord, trace: Trace, pick: int) -> None:
    parents = [item.snapshot.hypothesis_id for item in run.offered()]
    if parents:
        run.accepted.append(_fresh(trace, parent_hypothesis_id=parents[pick % len(parents)]))


def _retire(run: RunRecord, _trace: Trace, pick: int) -> None:
    done = [item for item in run.hypotheses.values() if item.rounds]
    if not done:
        return
    chosen = done[pick % len(done)]
    update = PlanUpdate(
        hypothesis_id=chosen.hypothesis_id,
        disposition=HypothesisStrategy.PARKED if pick % 2 else HypothesisStrategy.ABANDONED,
        reason="no further progress",
        reason_kind=StrategyReason.BLOCKED,
    )
    run.planned(Scheduled(PlanCheck(reasoning="r", handoff=HandoffNote(), updates=(update,))))
    retired = run.hypotheses[chosen.hypothesis_id]
    assert (retired.strategy, retired.reason_kind) == (update.disposition, update.reason_kind)


def _measured(run: RunRecord, _trace: Trace, pick: int) -> None:
    if run.baseline is None:
        run.measured(INPUTS[sorted(INPUTS)[pick % len(INPUTS)]])
        assert run.gate.blocked_at_done is None  # planning is asked again with the measurement


ACTIONS: dict[str, Callable[[RunRecord, Trace, int], None]] = {
    "plan": _plan,
    "start": _start,
    "end": _end,
    "no-plan": _no_plan,
    "continue": _continue,
    "child": _child,
    "retire": _retire,
    "measured": _measured,
}


def _step(run: RunRecord, trace: Trace, action: Step) -> None:
    kind, pick = action
    if kind in {"plan", "continue", "child"} and (run.accepted or not run.capacity()):
        return  # the flow starts what a plan accepted before it plans again, and plans for free slots
    ACTIONS[kind](run, trace, pick)


def play(
    cfg: DynamicConfig, steps: list[Step], *, input_pending: bool = False
) -> tuple[RunRecord, Trace]:
    run, trace = run_record(cfg), Trace()
    if input_pending:
        run.baseline = None
    for action in steps:
        before = dict(run.running)
        _step(run, trace, action)
        _check(run, trace)
        for key, item in before.items():
            if key in run.running:
                assert run.running[key] == item  # a workstream's parent is frozen at scheduling
    return run, trace


@settings(max_examples=examples(pr=10, nightly=200), deadline=None, derandomize=True, database=None)
@given(
    steps=STEPS,
    rounds=st.integers(1, 3),
    width=st.integers(1, 3),
    input_pending=st.booleans(),
)
def test_the_record_keeps_its_budget_slots_gate_and_leader_whatever_order_things_happen_in(
    steps: list[Step], rounds: int, width: int, *, input_pending: bool
) -> None:
    play(config(max_rounds=rounds, max_in_flight=width), steps, input_pending=input_pending)


# -- how the search ends without a candidate --------------------------------------------------------

MEASURED = Measured(reading=InputReading(accuracy_passed=True, benchmark_passed=True))
INPUTS = {
    "measured": MEASURED,
    "not configured": NotConfigured(),
    "workload failed": Unmeasurable(
        failure=InputFailure(kind=InputFailureKind.BENCHMARK_FAILED, output="crashed"),
        reading=InputReading(accuracy_passed=True, benchmark_passed=False),
    ),
    "no evidence": Unmeasurable(
        failure=InputFailure(
            kind=InputFailureKind.NO_EVIDENCE, status=ObservationStatus.FAILED, output=""
        )
    ),
    "accuracy failed": Unmeasurable(
        failure=InputFailure(
            kind=InputFailureKind.ACCURACY_FAILED, stage="correctness", output="hung"
        ),
        reading=InputReading(accuracy_passed=False),
    ),
    # A checkpoint written before S173 held a failed accuracy check as a measured input.
    "accuracy failed before S173": TypeAdapter(Baseline).validate_json(
        '{"kind":"measured","reading":{"accuracy_passed":false}}'
    ),
}


@settings(max_examples=examples(pr=8, nightly=100))
@given(
    given_input=st.sampled_from(sorted(INPUTS)),
    planner_gave_up=st.booleans(),
    candidates=st.integers(0, 2),
)
def test_a_search_ends_without_a_winner_only_when_no_candidate_stands_and_the_input_is_not_trusted(
    given_input: str, *, planner_gave_up: bool, candidates: int
) -> None:
    baseline = INPUTS[given_input]
    run = run_record(config(max_in_flight=3), baseline=baseline)
    run.accepted.extend(_fresh(Trace()) for _ in range(candidates))
    run.accepted = [
        plan.model_copy(update={"work_id": f"h{index}"}) for index, plan in enumerate(run.accepted)
    ]
    for begun in run.ready_to_start(3):
        _done(run, begun, "eligible", Trace())
    if planner_gave_up:
        run.planned(NoPlan(problem=None, stuck=True, blocked_at_done=len(run.finished_work)))

    ending = run.ending()
    if candidates:
        # a winner stands; one never compared with the input says so
        assert ending == (Uncompared() if uncompared(baseline) else None)
    elif trusted(baseline) and not planner_gave_up:
        assert ending is None  # the trusted input stands
    else:
        assert isinstance(ending, NoWinner)
        if not trusted(baseline):
            assert ending.reason.startswith("no trusted result")


def test_the_record_checkpoints_every_field_it_holds() -> None:
    """A field added to the record or its queue is resumed after a code change, or is the run's.

    What the run fixes (config, input revision, task terms; the queue's slots and policy) is
    given again on resume, and main is compared with the record's own input measurement; verdicts not yet handed over are handed again (`decided_verdicts`).
    """
    record = run_record()
    gate = {f"gate_{item.name}" for item in dataclasses.fields(type(record.gate))}
    held = {item.name for item in dataclasses.fields(RunRecord)} - {"config", "base", "terms"}
    assert (held - {"gate"}) | gate == set(RecordState.model_fields)
    queue = {item.name for item in dataclasses.fields(EvaluationQueue)}
    assert queue - {"slots", "policy", "_verdicts"} == set(QueueState.model_fields)
    main = {item.name for item in dataclasses.fields(MainLine)}
    assert main - {"baseline"} == set(MainState.model_fields)
