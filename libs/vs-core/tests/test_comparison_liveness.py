"""Every comparison core starts reaches its end: a verdict, a bounded close call, or a known stop.

Two ways a comparison used to wait for good:

- A base run is shared by every candidate compared with it (another attempt's run of the same
  base counts, and so does the strategy's own run it claims). When that run ended without a
  sample (drained with its attempt, out of queue allowance, failed in the machinery, refused at
  submission), nothing asked for another: a new sample was the only trigger, and the scope's
  budget refused a run after one that gave none.
- Under no declared floor a verdict needs three runs a side, and the candidate never got
  there: an agent's call for unchanged content is answered with the evaluation it already made,
  and the official check adds one run. So no verdict was ever reached, every official check ran
  afresh, and the base took its two extra runs for nothing.

These tests drive the kernel through its public events along production paths: one candidate
is one commit of one content, evaluated once by its agent and then by the strategy's official
check, and a repeat call for it is answered with the earlier evaluation.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Literal

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_sim.api.testing import examples

from .test_baseline_in_flight import strategy_baseline
from .test_measurement_comparison import (
    FLOOR,
    STALENESS,
    Lab,
    ended_with,
    final_check,
    reading,
    started_with,
)
from .test_measurement_reuse import record, revision, seen, submissions
from .test_measurements import committed, transition

Death = Literal["cancelled", "infrastructure", "refused", "drained"]
"""How a run ends without a sample, for a reason that says nothing of the next run."""
DEATHS: tuple[Death, ...] = ("cancelled", "infrastructure", "refused", "drained")
Outcome = Literal[
    "sample",
    "no-metric",
    "workload",
    "requirement-not-met",
    "cancelled",
    "infrastructure",
    "refused",
    "unavailable",
]

_PASSED = core.EvaluationTerminalFacts(
    stages=(
        core.EvaluationStageResult(stage_id="accuracy", outcome=core.EvaluationStageOutcome.PASSED),
        core.EvaluationStageResult(
            stage_id="benchmark", outcome=core.EvaluationStageOutcome.PASSED
        ),
    ),
    accuracy_passed=True,
)


def died(state: core.CoreState, request: core.SubmitMeasurement, how: Outcome) -> core.CoreState:
    """End ``request`` without a benchmark reading, ``how``; the observations arrive job first.

    The job's end reaches core before its submission's own observation, as it does in a run
    (`_ended` hands the job's end to the ledger). A refused submission has no job at all.
    """
    return died_steps(state, request, how)[-1].state


def died_steps(
    state: core.CoreState, request: core.SubmitMeasurement, how: Outcome
) -> tuple[core.Transition, ...]:
    """Every transition `died` takes, in order."""
    assert request.request_id is not None
    if how in {"refused", "unavailable"}:
        refused = seen(
            request,
            1,
            status=core.ObservationStatus.REJECTED,
            terminal=True,
            accepted=False,
            resource_id=None,
            observed_at=state.run.now_at,
        )
        state = committed(state, refused)
        return (
            transition(
                state,
                core.MeasurementSubmissionObserved(
                    observation=refused,
                    # The run environment refused it: its workspace root failed its probe.
                    failure=core.MeasurementFailure.RUN_ENVIRONMENT_UNAVAILABLE
                    if how == "unavailable"
                    else core.MeasurementFailure.INFRASTRUCTURE,
                ),
            ),
        )
    started = seen(request, 1)
    state = committed(state, started)
    steps = [transition(state, core.MeasurementSubmissionObserved(observation=started))]
    assert started.resource_id is not None
    steps.append(
        transition(
            steps[-1].state, core.JobObserved(resource_id=started.resource_id, observation=started)
        )
    )
    state = steps[-1].state
    status, failure = {
        "cancelled": (core.ObservationStatus.CANCELLED, None),
        "infrastructure": (core.ObservationStatus.FAILED, core.MeasurementFailure.INFRASTRUCTURE),
        "no-metric": (core.ObservationStatus.SUCCEEDED, None),
        "workload": (core.ObservationStatus.FAILED, core.MeasurementFailure.WORKLOAD),
        "requirement-not-met": (core.ObservationStatus.SUCCEEDED, None),
    }[how]
    finished = seen(
        request,
        2,
        status=status,
        terminal=True,
        released=True,
        children_complete=True,
        observed_at=state.run.now_at,
    )
    records_exist = how in ("no-metric", "requirement-not-met")
    facts = _PASSED if records_exist else None
    records = (
        ()
        if not records_exist
        else tuple(
            core.EvidenceRef(
                evidence_id=core.EvidenceId(root=f"{request.request_id.root}-{stage}"),
                kind=kind,
                purpose=request.plan.purpose,
                scope=request.scope,
                source_request=request.request_id,
                candidate=request.plan.candidate,
                observation_sequence=finished.sequence,
                evaluator_digest=request.plan.evaluator_digest,
                workload_digest=request.plan.workload_digest,
                environment_digest=request.plan.environment_digest,
                provenance="trusted",
                status=(
                    core.ObservationStatus.FAILED
                    if how == "requirement-not-met" and stage == "benchmark"
                    else status
                ),
                unmet_requirement=(
                    "prefix_cache"
                    if how == "requirement-not-met" and stage == "benchmark"
                    else None
                ),
            )
            for stage, kind in (
                ("accuracy", core.EvidenceKind.CORRECTNESS),
                ("benchmark", core.EvidenceKind.BENCHMARK),
            )
            if isinstance(request.plan.candidate, core.RevisionRef)
        )
    )
    state = committed(state, finished, facts)
    steps.append(
        transition(
            state,
            core.JobObserved(
                resource_id=finished.resource_id or started.resource_id,
                observation=finished,
                evidence=records,
                evaluation_result=facts,
                failure=failure,
            ),
        )
    )
    steps.append(
        transition(
            steps[-1].state,
            core.MeasurementSubmissionObserved(observation=finished, failure=failure),
        )
    )
    return tuple(steps)


def closed(lab: Lab, name: str) -> None:
    """The attempt ``name`` is closing: it may start no work, and its jobs are drained."""
    attempts = tuple(
        row.model_copy(update={"phase": core.AttemptPhase.CLOSING})
        if row.attempt_id.root == name
        else row
        for row in lab.state.attempts.attempts
    )
    lab.state = lab.state.model_copy(
        update={"attempts": lab.state.attempts.model_copy(update={"attempts": attempts})}
    )


def running(lab: Lab) -> list[core.SubmitMeasurement]:
    return [
        r
        for r in submissions(lab.state)
        if r.request_id is not None and r.request_id.root not in lab.ended
    ]


def end(lab: Lab, run: core.SubmitMeasurement, outcome: Outcome, value: float = 0.0) -> None:
    """End ``run`` with ``outcome``: a sample reading ``value``, or no sample."""
    assert run.request_id is not None
    lab.ended.add(run.request_id.root)
    if outcome == "sample":
        lab.state = ended_with(lab.state, run, value)
    else:
        lab.state = died(lab.state, run, outcome)


def kill(lab: Lab, run: core.SubmitMeasurement, death: Death) -> None:
    """End ``run`` without a sample by ``death``; a drain closes the run's attempt first."""
    if death == "drained":
        closed(lab, run.scope.owner.root)
        end(lab, run, "cancelled")
    else:
        end(lab, run, death)


def base_runs(lab: Lab) -> list[core.SubmitMeasurement]:
    return [r for r in submissions(lab.state) if r.plan.purpose == "baseline"]


def compared(lab: Lab, name: str, candidate_input: str) -> list[core.Comparison]:
    return [
        c
        for c in lab.comparisons()
        if c.scope == lab.scopes[name] and c.candidate_input == candidate_input
    ]


def repeat(lab: Lab, name: str, commit: str, candidate_input: str) -> core.AgentCall:
    """The agent of ``name`` asks again for the commit it already evaluated: unchanged content."""
    lab.call(name, commit, candidate_input)
    return lab.state.evaluation.agent_calls[-1]


# Finding 2: a base run that dies leaves nobody waiting on it for good.


@pytest.mark.regression
@pytest.mark.parametrize("floor", [FLOOR, 0.0], ids=["floor", "no-floor"])
@pytest.mark.parametrize("death", DEATHS)
def test_another_attempts_base_run_that_dies_is_replaced_for_the_candidate_waiting_on_it(
    death: Death, floor: float
) -> None:
    """Attempt ``b`` skips the base because ``a``'s run of it is under way; that run dies.

    Before, ``b``'s candidate held a sample and no base sample ever came: nothing re-checked
    it, and its comparison never existed. Now the run's end re-checks every candidate that
    waits on the base, and one base run is started for them.
    """
    lab = Lab.of("a", "b", floor=floor)
    _, a_base = lab.call("a", "c1", "input-1")
    (b_run,) = lab.call("b", "c2", "input-2")
    end(lab, b_run, "sample", 300.0)
    kill(lab, a_base, death)
    replaced = base_runs(lab)[1:]
    assert len(replaced) == 1, "one base run replaces the one that died"
    expected = "b" if death == "drained" else "a"
    assert replaced[0].scope == lab.scopes[expected]
    end(lab, replaced[0], "sample", 100.0)
    (held, *_) = compared(lab, "b", "input-2")
    assert held.base_samples == 1
    assert held.ratio == 3.0


@pytest.mark.regression
@pytest.mark.parametrize("death", DEATHS)
def test_a_claimed_strategy_baseline_that_dies_is_replaced_at_once(death: Death) -> None:
    """The candidate claimed the strategy's run of its base; it ends without a sample.

    Before, the candidate waited until another evaluation happened to ask for the base.
    """
    lab = Lab.of("a")
    own = strategy_baseline(lab)
    (candidate,) = lab.call("a", "c1", "input-1")
    end(lab, candidate, "sample", 130.0)
    if death == "drained":
        # The strategy's own run belongs to the run's scope, not to an attempt that closes.
        end(lab, own, "cancelled")
    else:
        end(lab, own, death)
    (replaced,) = base_runs(lab)[1:]
    assert replaced.scope == lab.scopes["a"]
    end(lab, replaced, "sample", 100.0)
    assert compared(lab, "a", "input-1")[0].ratio == 1.3


@pytest.mark.regression
@pytest.mark.parametrize("floor", [FLOOR, 0.0], ids=["floor", "no-floor"])
def test_a_run_core_takes_on_the_end_of_the_strategys_run_proves_its_own_origin(
    floor: float,
) -> None:
    """The strategy's claimed baseline ends and makes a close call, which earns a base run.

    Every request a step emits used to be charged to the decision whose run's end the step
    handled, so core's own base run carried the strategy's baseline decision. Its observations
    then failed to prove their origin (core admitted it for no decision), so its sample was
    never recorded and the close call never narrowed.
    """
    lab = Lab.of("a", floor=floor)
    own = strategy_baseline(lab)
    (candidate,) = lab.call("a", "c1", "input-1")
    end(lab, candidate, "sample", 100.3)
    end(lab, own, "sample", 100.0)
    (extra,) = base_runs(lab)[1:]
    assert extra.decision_id is None, "the request on record is charged to no decision"
    end(lab, extra, "sample", 100.1)
    assert compared(lab, "a", "input-1")[-1].base_samples == 2


@pytest.mark.regression
@pytest.mark.parametrize("death", ["cancelled", "infrastructure", "refused"])
def test_a_base_run_that_died_does_not_block_the_next_one_in_its_scope(death: Death) -> None:
    """Before, a scope's base identity refused any run after one that was not a success with a
    sample, so even the next candidate of the same attempt could not measure its base."""
    lab = Lab.of("a")
    candidate, base = lab.call("a", "c1", "input-1")
    kill(lab, base, death)
    (again,) = base_runs(lab)[1:]
    end(lab, candidate, "sample", 300.0)
    end(lab, again, "sample", 100.0)
    assert compared(lab, "a", "input-1")[-1].base_samples == 1


@pytest.mark.parametrize(
    "ending",
    [
        "no-metric",
        "workload",
        "unavailable",
    ],
)
def test_a_base_run_that_would_end_the_same_way_again_is_not_retried(ending: Outcome) -> None:
    """A benchmark that reads no metric, a base the evaluation rejects, or a run environment
    that can run nothing (its workspace root failed its probe) is one probe."""
    lab = Lab.of("a")
    candidate, base = lab.call("a", "c1", "input-1")
    end(lab, base, ending)
    end(lab, candidate, "sample", 300.0)
    assert base_runs(lab) == [base]
    assert running(lab) == []


def test_a_base_run_that_keeps_dying_is_retried_a_bounded_number_of_times() -> None:
    lab = Lab.of("a")
    candidate, _ = lab.call("a", "c1", "input-1")
    end(lab, candidate, "sample", 300.0)
    for _ in range(3 * core.MAX_SUBMISSIONS_PER_MEASUREMENT):
        live = running(lab)
        if not live:
            break
        (base,) = live
        end(lab, base, "infrastructure")
    assert running(lab) == []
    assert len(base_runs(lab)) == core.MAX_SUBMISSIONS_PER_MEASUREMENT


# Finding 1: under no floor the candidate side reaches the runs a verdict needs.


# Late evidence: a run that ended before its records arrived is not replaced on the way.


def ended_without_records(lab: Lab, run: core.SubmitMeasurement) -> core.RequestId:
    """End ``run`` as succeeded, its records not yet known; the collection core asks for."""
    assert run.request_id is not None
    lab.state = started_with(lab.state, run)
    finished = seen(
        run,
        2,
        status=core.ObservationStatus.SUCCEEDED,
        terminal=True,
        released=True,
        children_complete=True,
        observed_at=lab.state.run.now_at,
    )
    assert finished.resource_id is not None
    lab.state = committed(lab.state, finished, _PASSED)
    lab.state = transition(
        lab.state, core.MeasurementSubmissionObserved(observation=finished)
    ).state
    ended = transition(
        lab.state,
        core.JobObserved(
            resource_id=finished.resource_id, observation=finished, evaluation_result=_PASSED
        ),
    )
    lab.state = ended.state
    lab.ended.add(run.request_id.root)
    (collect,) = (r for r in ended.requests if isinstance(r, core.CollectEvidence))
    assert collect.request_id is not None
    return collect.request_id


def collected(lab: Lab, run: core.SubmitMeasurement, collect: core.RequestId, value: float) -> None:
    """The collection answers with ``run``'s records, the benchmark reading ``value``."""
    answer = seen(
        run,
        3,
        status=core.ObservationStatus.SUCCEEDED,
        terminal=True,
        released=True,
        children_complete=True,
        observed_at=lab.state.run.now_at,
    )
    rows = tuple(
        row.model_copy(update={"phase": core.RequestPhase.COMPLETED})
        if row.request_id == collect
        else row
        for row in lab.state.records.records
    )
    lab.state = lab.state.model_copy(
        update={"records": lab.state.records.model_copy(update={"records": rows})}
    )
    status = core.ObservationStatus.SUCCEEDED
    records = (
        record(run, answer, "accuracy", status),
        record(run, answer, "benchmark", status).model_copy(update={"reading": reading(value)}),
    )
    assert answer.resource_id is not None
    lab.state = transition(
        lab.state,
        core.JobObserved(
            resource_id=answer.resource_id,
            observation=answer,
            evidence=records,
            evaluation_result=_PASSED,
        ),
    ).state


@pytest.mark.regression
@pytest.mark.parametrize("floor", [FLOOR, 0.0])
def test_a_base_run_whose_records_are_still_being_collected_is_not_replaced(floor: float) -> None:
    """The base run ended before its records came; core waits for its collection, not a rerun.

    Before, the end of the job without records read as a run that gave no sample, so core
    started a replacement base run at once, and the records that arrived next made it a
    duplicate.
    """
    lab = Lab.of("a", floor=floor)
    candidate, base = lab.call("a", "c1", "input-1")
    end(lab, candidate, "sample", 130.0)
    collect = ended_without_records(lab, base)
    assert running(lab) == [], "no base run is started while its records are collected"
    collected(lab, base, collect, 100.0)
    (comparison,) = compared(lab, "a", "input-1")
    assert comparison.base_samples == 1, "the collected records are the base's sample"


@pytest.mark.regression
def test_a_base_run_whose_collection_ends_without_records_is_replaced() -> None:
    """A collection that blocks at its bound leaves no sample: the base is measured again."""
    lab = Lab.of("a")
    candidate, base = lab.call("a", "c1", "input-1")
    end(lab, candidate, "sample", 130.0)
    collect = ended_without_records(lab, base)
    row = lab.state.records.record(collect)
    assert row is not None
    lab.state = transition(lab.state, core.ClockAdvanced(now_at=row.until.at + 1.0)).state
    (again,) = running(lab)
    assert again.plan.purpose == "baseline"
    assert again.request_id != base.request_id


@dataclass
class Bench:
    """Readings per measured input, one per run in order: a noisy or a deterministic benchmark."""

    means: dict[str, float]
    spread: float
    draw: random.Random

    def read(self, measured_input: str) -> float:
        mean = self.means[measured_input]
        return mean if self.spread == 0 else mean * (1 + self.draw.gauss(0.0, self.spread))


def settle(lab: Lab, bench: Bench, *, most: int = 40) -> int:
    """End every run, oldest first, until none is left; the number of runs ended."""
    ended = 0
    while (live := running(lab)) and ended < most:
        run = live[0]
        name = run.plan.measured_input
        assert name is not None
        end(lab, run, "sample", bench.read(name))
        ended += 1
    assert not running(lab), "the runs a comparison takes are bounded"
    return ended


def seeded(label: str) -> random.Random:
    # lint-waiver: LW-482913 [S311]; seeded simulation needs reproducible non-cryptographic draws.
    # > System entropy would make the bound nondeterministic; a secure generator adds no test
    # > guarantee and does not provide the Gaussian draws this simulation needs.
    return random.Random(label)  # noqa: S311


@pytest.mark.regression
def test_one_evaluation_without_a_floor_reaches_a_verdict_and_the_official_check_reuses_it() -> (
    None
):
    """One commit, evaluated once, noisy runs: core takes the candidate to three runs itself.

    Before, the candidate held one run (a repeat call is answered with it), the comparison
    stayed ``inconclusive`` for good, and the official check always ran afresh.
    """
    lab = Lab.of("a", floor=0.0)
    lab.call("a", "c1", "input-1")
    settle(lab, Bench({"input-1": 120.0, "base": 100.0}, 0.01, seeded("verdict")))
    again = repeat(lab, "a", "c1", "input-1")
    assert again.repeat, "unchanged content is the evaluation already made"
    last = compared(lab, "a", "input-1")[-1]
    assert (last.candidate_samples, last.base_samples) == (3, 3)
    assert last.verdict is core.ComparisonVerdict.BETTER
    assert final_check(lab, "a", "c1", "input-1").reuses is not None


@pytest.mark.regression
def test_a_deterministic_benchmark_without_a_floor_stops_at_three_runs_a_side() -> None:
    """Identical readings never give a verdict, so nothing more is run for one.

    Before, the base took its two extra runs, the candidate stayed at one, and the official
    check ran afresh. Now each side stops at three runs, and the official check is answered by
    the samples held instead of a fourth run that would read the same.
    """
    lab = Lab.of("a", floor=0.0)
    lab.call("a", "c1", "input-1")
    settle(lab, Bench({"input-1": 100.3, "base": 100.0}, 0.0, seeded("deterministic")))
    last = compared(lab, "a", "input-1")[-1]
    assert (last.candidate_samples, last.base_samples) == (3, 3)
    assert last.verdict is core.ComparisonVerdict.INCONCLUSIVE
    assert core.verdict_unreachable(last, core.moment_of(lab.state.run).policy)
    assert len(base_runs(lab)) == core.MIN_SAMPLES_WITHOUT_FLOOR
    assert final_check(lab, "a", "c1", "input-1").reuses is not None


def test_under_a_declared_floor_core_takes_no_run_of_the_candidate() -> None:
    lab = Lab.of("a", floor=FLOOR)
    lab.call("a", "c1", "input-1")
    settle(lab, Bench({"input-1": 100.3, "base": 100.0}, 0.0, seeded("floor")))
    last = compared(lab, "a", "input-1")[-1]
    assert (last.candidate_samples, last.base_samples) == (1, core.BASE_MAX_SAMPLES)
    assert [r.plan.purpose for r in submissions(lab.state)].count("local-validation") == 1


# Properties over realistic grids.


Step = tuple[Literal["call", "end", "wait"], int, int]


def play(lab: Lab, bench: Bench, steps: list[Step]) -> None:
    """Take ``steps`` in turn: an agent's call, the end of a running run, or the base going stale."""
    names = ("a", "b")
    for kind, who, what in steps:
        if kind == "wait":
            lab.advance(STALENESS + 1.0)
            continue
        if kind == "call":
            name = names[who]
            if not any(
                row.attempt_id.root == name and row.phase is core.AttemptPhase.ACTIVE
                for row in lab.state.attempts.attempts
            ):
                continue
            # One commit per content: an attempt's agent evaluates input-n as commit n-name.
            lab.call(name, f"{name}-{what}", f"input-{what}")
            continue
        live = running(lab)
        if not live:
            continue
        run = live[who % len(live)]
        if what == len(DEATHS):
            name = run.plan.measured_input
            end(lab, run, "sample", bench.read("base" if name is None else name))
        else:
            kill(lab, run, DEATHS[what])


def charged_within_limit(lab: Lab) -> None:
    """No comparison is charged more runs of an identity than its submission limit (`_charged`).

    Core's own runs count toward the comparison they were taken for; any other run counts
    toward every comparison.
    """
    limit = core.MAX_SUBMISSIONS_PER_MEASUREMENT
    served = {run.request_id: run.candidate_input for run in lab.state.evaluation.core_runs}
    for budget in lab.state.evaluation.submission_budgets:
        # A historical receipt names no request, so it counts toward every comparison.
        asked = [
            served.get(receipt.request_id)
            if isinstance(receipt, core.PreparedSubmissionReceipt)
            else None
            for receipt in budget.receipts
        ]
        for candidate_input in set(asked) - {None}:
            charged = sum(1 for one in asked if one in (None, candidate_input))
            assert charged <= min(budget.limit, limit)
        assert asked.count(None) <= min(budget.limit, limit)


@settings(max_examples=examples(pr=60, nightly=600))
@given(
    floor=st.sampled_from((FLOOR, 0.0)),
    spread=st.sampled_from((0.0, 0.01, 0.05)),
    ratio=st.sampled_from((0.5, 1.0, 1.003, 1.05, 2.0)),
    steps=st.lists(
        st.one_of(
            st.tuples(st.just("call"), st.integers(0, 1), st.integers(0, 2)),
            st.tuples(st.just("end"), st.integers(0, 7), st.integers(0, len(DEATHS))),
            st.tuples(st.just("wait"), st.just(0), st.just(0)),
        ),
        min_size=1,
        max_size=16,
    ),
    seed=st.integers(0, 2**16),
)
@example(
    floor=0.0,
    spread=0.0,
    ratio=1.0,
    steps=[("call", 0, 0), ("call", 1, 1), ("end", 1, 3), ("end", 0, 0)],
    seed=0,
)
@example(
    floor=FLOOR,
    spread=0.01,
    ratio=1.003,
    steps=[("call", 0, 0), ("end", 1, 1), ("end", 0, 4), ("end", 0, 2)],
    seed=1,
)
@example(
    floor=0.0,
    spread=0.01,
    ratio=1.003,
    steps=[
        ("call", 0, 0),
        ("end", 0, 4),
        ("end", 0, 4),
        ("wait", 0, 0),
        ("call", 0, 1),
        ("end", 0, 1),
        ("wait", 0, 0),
        ("call", 0, 2),
    ],
    seed=2,
)
def test_every_candidate_with_a_sample_gets_a_comparison_and_the_runs_stay_bounded(
    floor: float, spread: float, ratio: float, steps: list[Step], seed: int
) -> None:
    """Over any interleaving of evaluations by two attempts and run ends, some of them deaths.

    A step ends a running run (chosen by index) with a sample or one of the deaths, a drain
    closing the run's attempt, or lets the base's samples go stale. Afterwards every run ends
    with a sample. Then every candidate that holds a sample in an attempt still open is
    compared with its base, no comparison holds more base samples than a close call may earn,
    each candidate input no more runs than the minimum without a floor (one under a floor) plus
    its agent's, and each comparison is charged no more runs of an identity than its
    submission limit.
    """
    lab = Lab.of("a", "b", floor=floor)
    bench = Bench(
        {f"input-{n}": 100.0 * ratio * (1 + n / 10) for n in range(3)} | {"base": 100.0},
        spread,
        seeded(f"grid:{seed}"),
    )
    play(lab, bench, steps)
    settle(lab, bench, most=80)
    most_base = core.BASE_MAX_SAMPLES if floor else core.MIN_SAMPLES_WITHOUT_FLOOR
    assert all(c.base_samples <= most_base for c in lab.comparisons())
    if not any(kind == "wait" for kind, _, _ in steps):
        base_samples = [s for s in lab.state.evaluation.samples if s.measured_input == "base"]
        assert len(base_samples) <= most_base
    open_scopes = {
        lab.scopes[row.attempt_id.root]
        for row in lab.state.attempts.attempts
        if row.phase is core.AttemptPhase.ACTIVE
    }
    sampled = {
        (s.scope, s.measured_input)
        for s in lab.state.evaluation.samples
        if s.base is not None and s.scope in open_scopes
    }
    assert sampled <= {(c.scope, c.candidate_input) for c in lab.comparisons()}
    for scope, candidate_input in sampled:
        runs = [
            r
            for r in submissions(lab.state)
            if r.scope == scope and r.plan.measured_input == candidate_input
        ]
        assert len(runs) <= core.MAX_SUBMISSIONS_PER_MEASUREMENT
    charged_within_limit(lab)
    assert type(lab.state).model_validate_json(lab.state.model_dump_json()) == lab.state
    if not floor:
        for comparison in lab.comparisons():
            assert comparison.candidate_samples <= core.MIN_SAMPLES_WITHOUT_FLOOR


SIMULATED = 300


@pytest.mark.parametrize("spread", [0.005, 0.02, 0.05])
def test_at_equal_means_one_evaluation_and_its_official_check_rarely_give_a_verdict(
    spread: float,
) -> None:
    """Seeded simulation of the production path at no floor: equal true means, noisy runs.

    Each trial is one commit evaluated once by its agent, everything core takes for its
    comparison, then the official check. Any verdict is false. At most 7% of trials give one
    (Welch's test at 95%, conservative), every trial takes at most two extra runs of the
    candidate and two of the base before the official check, and the official check adds at
    most one run.
    """
    draw = seeded(f"equal-means:{spread}")
    verdicts = 0
    for trial in range(SIMULATED):
        lab = Lab.of("a", floor=0.0)
        bench = Bench({"input-1": 100.0, "base": 100.0}, spread, draw)
        lab.call("a", "c1", "input-1")
        assert settle(lab, bench) <= 2 + 2 * (core.MIN_SAMPLES_WITHOUT_FLOOR - 1)
        check = final_check(lab, "a", "c1", "input-1")
        if check.reuses is None:
            lab.state = ended_with(lab.state, check, bench.read("input-1"))
        assert check.request_id is not None
        lab.ended.add(check.request_id.root)
        assert running(lab) == [], trial
        last = compared(lab, "a", "input-1")[-1]
        assert last.candidate_samples <= core.MIN_SAMPLES_WITHOUT_FLOOR + 1
        assert last.base_samples == core.MIN_SAMPLES_WITHOUT_FLOOR
        if last.verdict in (core.ComparisonVerdict.BETTER, core.ComparisonVerdict.WORSE):
            verdicts += 1
    assert verdicts / SIMULATED <= 0.07, verdicts


@pytest.mark.regression
@pytest.mark.parametrize("gap", [1.05, 1.2])
def test_a_real_difference_without_a_floor_is_usually_found(gap: float) -> None:
    """Seeded simulation: a 5% or 20% true gain at 1% run noise is called ``better`` in most
    trials (before, never)."""
    draw = seeded(f"power:{gap}")
    found = 0
    for _ in range(100):
        lab = Lab.of("a", floor=0.0)
        lab.call("a", "c1", "input-1")
        settle(lab, Bench({"input-1": 100.0 * gap, "base": 100.0}, 0.01, draw))
        if compared(lab, "a", "input-1")[-1].verdict is core.ComparisonVerdict.BETTER:
            found += 1
    assert found >= 80, found


@settings(max_examples=examples(pr=60, nightly=600))
@given(
    mine=st.lists(st.sampled_from((99.0, 100.0, 101.0)), min_size=1, max_size=30),
    theirs=st.lists(st.sampled_from((99.0, 100.0, 101.0)), min_size=1, max_size=30),
    deterministic=st.booleans(),
)
def test_a_verdict_is_unreachable_exactly_when_three_identical_runs_a_side_show_no_noise(
    *, mine: list[float], theirs: list[float], deterministic: bool
) -> None:
    """Over 1 to 30 runs a side: only zero spread at the minimum count, without a floor, is a
    comparison no further run can settle; a declared floor never is."""
    if deterministic:
        mine, theirs = [mine[0]] * len(mine), [theirs[0]] * len(theirs)
    scope = core.Scope(owner=core.AttemptId(root="a"), generation=0)

    def samples(values: list[float], name: str) -> tuple[core.Sample, ...]:
        return tuple(
            core.Sample(
                run=core.RequestId(root=f"{name}-{n}"),
                scope=scope,
                commit=revision(name),
                measured_input=name,
                evaluation="evaluation",
                gates=("benchmark",),
                host="host",
                dedicated_resources=True,
                reading=reading(value),
                observed_at=0.0,
            )
            for n, value in enumerate(values)
        )

    anchor = core.BaseRef(commit=revision("base"), measured_input="base")
    for floor in (0.0, FLOOR):
        policy = core.ComparisonPolicy(noise_floor=floor)
        made = core.compare(
            samples(mine, "input"),
            samples(theirs, "base"),
            scope=scope,
            anchor=anchor,
            policy=policy,
        )
        assert made is not None
        alike = len(set(mine)) == 1 and len(set(theirs)) == 1
        enough = min(len(mine), len(theirs)) >= core.MIN_SAMPLES_WITHOUT_FLOOR
        assert core.verdict_unreachable(made, policy) == (not floor and alike and enough)
