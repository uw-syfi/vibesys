"""The run record keeps the budget, the slots, the planning gate and the leader whatever happens.

A generated run plans, starts and ends workstreams in any order, with every ending the flow can
produce: failed, stopped before it started, never opened (refunded), parent unverifiable
(withheld), closed with a kept candidate that is or is not winner-eligible. The input's measurement
may land at any step, since planning does not wait for it. After every step the record's answers
must agree with the facts: the budget the config states, the slots free, when planning may start
again, when the search is over, that no workstream starts before the input is measured, who leads
and why a search may have no winner. An agent turn may suspend on its evaluations and resume, and
the run may restart after a code change at any step: agents at work never outnumber
``max_in_flight``, and a resume waits only while no slot is free. Core admits a started
workstream's attempt only while fewer than an admission limit are open (any limit from 1 to
``max_in_flight + backlog``, as an operator may set): whatever the limit, the run never reaches a
state where no agent works and nothing can close.
"""

from __future__ import annotations

import copy
import dataclasses
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import pytest
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
    Opened,
    PlanCheck,
    PlanUpdate,
    QueueState,
    RecordState,
    Result,
    Resuming,
    RunRecord,
    RunSnapshot,
    Scheduled,
    Suspended,
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
    *("suspend", "suspend", "resume", "resume", "restart", "admit", "admit"),
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
    idle: dict[str, int] = field(default_factory=dict)
    """Workstreams whose agent turn is suspended, and the wait it is in: no agent works there."""
    authorized: set[str] = field(default_factory=set)
    """The idle workstreams whose resume core authorized."""
    limit: int = 1
    """Core's admission limit: it opens a started workstream's attempt only below it."""
    admitted: set[str] = field(default_factory=set)
    """The workstreams whose attempt core opened; the others queue for admission."""
    seated: set[str] = field(default_factory=set)
    """The workstreams lent their first agent slot: their agents may work."""


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
    # an agent works in every seated workstream whose turn is not suspended
    at_work = [key for key in run.running if key in trace.seated and key not in trace.idle]
    assert len(at_work) <= rules.max_in_flight
    assert len(run.at_work) <= rules.max_in_flight
    assert set(run.unseated) == set(run.running) - trace.seated
    _check_live(run, trace, at_work=at_work)
    assert scheduled - trace.refunded <= rules.start_budget
    # The run may schedule start_budget workstreams; a refund returns its slot.
    left = rules.start_budget - scheduled + trace.refunded
    assert run.capacity() == max(0, min(rules.max_in_flight - len(run.working), left))

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


def _check_live(run: RunRecord, trace: Trace, *, at_work: list[str]) -> None:
    """Something can always go on while a workstream runs, whatever core's admission limit."""
    if not run.running:
        return
    open_attempts = trace.admitted & set(run.running)
    queued = set(run.running) - trace.admitted
    may_admit = bool(queued) and len(open_attempts) < trace.limit
    may_authorize = any(key not in trace.authorized for key in trace.idle)
    may_grant = bool(copy.deepcopy(run).resumes())
    assert at_work or may_admit or may_authorize or may_grant, (
        f"stuck: open={sorted(open_attempts)} queued={sorted(queued)} "
        f"resuming={run.resuming} unseated={list(run.unseated)} limit={trace.limit}"
    )


def _admit(run: RunRecord, trace: Trace, pick: int) -> None:
    queued = [key for key in run.running if key not in trace.admitted]
    if queued and len(trace.admitted & set(run.running)) < trace.limit:
        key = queued[pick % len(queued)]
        trace.admitted.add(key)
        run.opened(Opened(key=key))  # its workspace opened: it waits for its first slot


def _suspend(run: RunRecord, trace: Trace, pick: int) -> None:
    busy = [key for key in run.running if key in trace.seated and key not in trace.idle]
    if busy:
        key = busy[pick % len(busy)]
        trace.idle[key] = pick + 1
        run.suspend(Suspended(key=key, wait=pick + 1))
        assert key in run.parked or len(run.waits) > run.config.backlog


def _resume(run: RunRecord, trace: Trace, pick: int) -> None:
    waiting = [key for key in trace.idle if key not in trace.authorized]
    if waiting:
        key = waiting[pick % len(waiting)]
        trace.authorized.add(key)
        run.resume(Resuming(key=key, wait=trace.idle[key]))


def _grant(run: RunRecord, trace: Trace, _pick: int) -> None:
    for key, wait in run.resumes():
        if key in trace.idle:
            assert key in trace.authorized
            assert trace.idle.pop(key) == wait
            trace.authorized.discard(key)
        else:  # new work's first slot, once core admitted its attempt
            assert (key, wait) == (key, 0)
            assert key in trace.admitted
            assert key not in trace.seated
            trace.seated.add(key)
    # a resume still waits only for a slot: it holds none and every slot works
    assert all(key in run.parked or key in run.unseated for key in run.resuming)
    assert not run.resuming or not run.free_agents()


def _plan(run: RunRecord, trace: Trace, pick: int) -> None:
    count = 1 + pick % max(1, run.capacity())
    plans = [
        _fresh(trace, kind=WorkKind.PROFILE if index % 4 == 3 else WorkKind.IMPLEMENT)
        for index in range(count)
    ]
    run.planned(Scheduled(PlanCheck(reasoning="r", handoff=HandoffNote(), accepted=tuple(plans))))


def _start(run: RunRecord, trace: Trace, _pick: int) -> None:
    _grant(run, trace, _pick)  # as the root does, before new work takes the free slots
    for begun in run.ready_to_start(run.room()):
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
        trace.idle.pop(start.key, None)
        trace.authorized.discard(start.key)
        trace.admitted.discard(start.key)
        trace.seated.discard(start.key)


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
    "suspend": _suspend,
    "resume": _resume,
    "admit": _admit,
}


def _restarted(run: RunRecord, trace: Trace) -> RunRecord:
    """The record a code change resumes: every suspended turn waits for a slot to start again.

    New work not yet lent a slot reopens its admitted workspace and reports it again.
    """
    resumed = RunRecord.resumed(run.config, run.base, run.terms, run.state())
    resumed.held_over()
    held = [key for key in resumed.resuming if key in trace.seated]
    trace.idle = dict.fromkeys(held, 0)
    trace.authorized = set(held)
    for key in resumed.unseated:
        if key in trace.admitted:
            resumed.opened(Opened(key=key))
    return resumed


def _step(run: RunRecord, trace: Trace, action: Step) -> None:
    kind, pick = action
    if kind in {"plan", "continue", "child"} and (run.accepted or not run.capacity()):
        return  # the flow starts what a plan accepted before it plans again, and plans for free slots
    ACTIONS[kind](run, trace, pick)


def play(
    cfg: DynamicConfig, steps: list[Step], *, input_pending: bool = False, limit: int | None = None
) -> tuple[RunRecord, Trace]:
    run = run_record(cfg)
    trace = Trace(limit=cfg.max_in_flight + cfg.backlog if limit is None else limit)
    if input_pending:
        run.baseline = None
    for action in steps:
        before = dict(run.running)
        if action[0] == "restart":
            run = _restarted(run, trace)
        else:
            _step(run, trace, action)
        _check(run, trace)
        _grant(run, trace, 0)  # the root's next pass lends the free slots
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
    data=st.data(),
    input_pending=st.booleans(),
)
def test_the_record_keeps_its_budget_slots_gate_and_leader_whatever_order_things_happen_in(
    steps: list[Step], rounds: int, width: int, data: st.DataObject, *, input_pending: bool
) -> None:
    backlog = data.draw(st.sampled_from([0, 1, 2, 3, None]), label="max_backlog")
    cfg = config(max_rounds=rounds, max_in_flight=width, max_backlog=backlog)
    # any admission limit an operator may set, up to the attempts the run may hold open
    limit = data.draw(st.integers(1, width + cfg.backlog), label="admission limit")
    play(cfg, steps, input_pending=input_pending, limit=limit)


ADMISSION_STORIES = {
    # B1 of the #3328 review: h1 and h2 park, h3 and h4 start into a full admission, h1 and h2
    # are authorized to resume; the resumes must get the slots the queued new work does not use
    "parked turns resume past new work queued for admission": (
        2,
        2,
        [
            *[("plan", 1), ("start", 0), ("admit", 0), ("admit", 0), ("suspend", 0)],
            *[("suspend", 0), ("plan", 1), ("start", 0), ("resume", 0), ("resume", 0)],
        ],
    ),
    # h1 parks, h2 starts, h1 resumes into the free slot; h2 admitted next must wait for one
    "new work admitted while every slot works waits for one": (
        1,
        2,
        [
            *[("plan", 0), ("start", 0), ("admit", 0), ("suspend", 0), ("plan", 0)],
            *[("start", 0), ("resume", 0), ("admit", 0)],
        ],
    ),
}


@pytest.mark.regression
@pytest.mark.parametrize("story", sorted(ADMISSION_STORIES))
def test_an_admission_limit_never_stalls_the_run_nor_lets_agents_outnumber_the_width(
    story: str,
) -> None:
    width, limit, steps = ADMISSION_STORIES[story]
    cfg = config(max_rounds=3, max_in_flight=width, max_backlog=width)
    run, trace = play(cfg, steps, limit=limit)

    assert len(run.running) == 2 * width, run.running  # every step above happened
    assert len(trace.admitted & set(run.running)) == limit, trace  # admission is full


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
