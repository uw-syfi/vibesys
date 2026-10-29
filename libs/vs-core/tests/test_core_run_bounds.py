"""What core's own runs cost an attempt is bounded by constants, not by how many inputs it measures.

Core runs the base, and without a declared floor the candidate's input, for an agent's
comparisons. Its runs never hold the agent's turn, so an agent that makes many small edits
(each a new candidate input, each its own comparison) could make core's runs grow with them:

- a base that keeps dying was retried up to five times for each comparison, so six inputs
  cost thirty base runs, and the scope's submission budget held more receipts than its limit,
  a state that could not be read back;
- each input's close call kept one run of its own in flight, so ten inputs meant ten of
  core's runs at once;
- a comparison was reported final while a base run another candidate's close call asked for
  was still under way, though that run would change it.

These tests drive the kernel through its public events along production paths (`Lab`).
"""

from __future__ import annotations

from typing import Literal

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_sim.api.testing import examples

from .test_comparison_liveness import Bench, Outcome, base_runs, compared, end, running, seeded
from .test_measurement_comparison import FLOOR, Lab

Side = Literal["base", "candidate"]


def side_of(run: core.SubmitMeasurement) -> Side:
    return "base" if run.plan.purpose == "baseline" else "candidate"


def core_in_flight(lab: Lab) -> dict[tuple[str, Side], int]:
    """How many of core's own runs of each side have not ended, per attempt."""
    own = {run.request_id for run in lab.state.evaluation.core_runs}
    counts: dict[tuple[str, Side], int] = {}
    for run in running(lab):
        if run.request_id in own:
            key = (run.scope.owner.root, side_of(run))
            counts[key] = counts.get(key, 0) + 1
    return counts


def readable(lab: Lab) -> None:
    """The state reads back as it was written: every budget holds no more receipts than its limit."""
    assert type(lab.state).model_validate_json(lab.state.model_dump_json()) == lab.state


def out_of_queue(lab: Lab, run: core.SubmitMeasurement) -> None:
    """Core ends ``run`` because its job waited in the queue for its whole queue budget.

    Recovery blocks the record with ``QUEUE_BUDGET_SPENT`` and cancels its job
    (`vs_core._request_recovery`); the job's cancellation then reaches core like any other.
    """
    assert run.request_id is not None
    rows = tuple(
        row.model_copy(
            update={
                "failure": core.Failed(
                    reason=core.FailureReason.QUEUE_BUDGET_SPENT, limit_at=lab.state.run.now_at
                )
            }
        )
        if row.request_id == run.request_id
        else row
        for row in lab.state.records.records
    )
    lab.state = lab.state.model_copy(
        update={"records": lab.state.records.model_copy(update={"records": rows})}
    )
    end(lab, run, "cancelled")


# Finding 1: a base that keeps dying costs a constant number of runs, however many inputs.


@pytest.mark.regression
@settings(max_examples=examples(pr=30, nightly=300))
@given(
    floor=st.sampled_from((FLOOR, 0.0)),
    inputs=st.integers(1, 8),
    deaths=st.lists(st.sampled_from(("cancelled", "infrastructure", "refused")), min_size=1),
)
@example(floor=FLOOR, inputs=6, deaths=["infrastructure"])
@example(floor=FLOOR, inputs=6, deaths=["cancelled"])
def test_a_base_that_keeps_dying_costs_a_bounded_number_of_runs_whatever_the_inputs(
    floor: float, inputs: int, deaths: list[Outcome]
) -> None:
    """Every base run dies; the agent measures ``inputs`` candidate inputs one after another.

    Before, each comparison was charged only its own runs of the base, so the base was run five
    times for each input (thirty for six), past its budget's limit. Now core stops after
    ``MAX_SUBMISSIONS_PER_MEASUREMENT`` of its runs of the base in a row give no sample.
    """
    lab = Lab.of("a", floor=floor)
    ended = 0
    for n in range(inputs):
        (mine, *_) = lab.call("a", f"c{n}", f"input-{n}")
        end(lab, mine, "sample", 120.0)
        while live := [r for r in running(lab) if side_of(r) == "base"]:
            end(lab, live[0], deaths[ended % len(deaths)])
            ended += 1
        readable(lab)
    assert len(base_runs(lab)) == min(core.MAX_SUBMISSIONS_PER_MEASUREMENT, core.MAX_CORE_BASE_RUNS)


@pytest.mark.regression
def test_a_base_run_core_ended_out_of_queue_budget_is_not_replaced() -> None:
    """The queue that held the run holds the next: core does not run the base again for it.

    Before, a cancellation for a spent queue budget read like any death, and core started
    another base run at once, and another for every later input.
    """
    lab = Lab.of("a")
    candidate, base = lab.call("a", "c1", "input-1")
    end(lab, candidate, "sample", 120.0)
    out_of_queue(lab, base)
    assert running(lab) == []
    (later,) = lab.call("a", "c2", "input-2")
    end(lab, later, "sample", 130.0)
    assert base_runs(lab) == [base]
    assert running(lab) == []


@pytest.mark.regression
def test_a_base_that_gives_a_sample_again_is_measured_again() -> None:
    """Four of core's base runs die, the fifth gives a sample: the next need is served again.

    The base's budget is opened at ``MAX_CORE_BASE_RUNS``, so its sixth run and later ones fit
    it. Before, they went past the limit of five it was opened at, and the state could not be
    read back.
    """
    lab = Lab.of("a", floor=0.0, staleness=50.0)
    candidate, _ = lab.call("a", "c1", "input-1")
    end(lab, candidate, "sample", 120.0)
    for _ in range(core.MAX_SUBMISSIONS_PER_MEASUREMENT - 1):
        (base,) = (r for r in running(lab) if side_of(r) == "base")
        end(lab, base, "infrastructure")
    (base,) = (r for r in running(lab) if side_of(r) == "base")
    end(lab, base, "sample", 100.0)
    while live := running(lab):
        end(lab, live[0], "sample", 120.0 if side_of(live[0]) == "candidate" else 100.0)
    lab.advance(60.0)
    (later, again) = lab.call("a", "c2", "input-2")
    assert side_of(again) == "base", "the stale base is measured for the next candidate"
    end(lab, later, "sample", 130.0)
    end(lab, again, "sample", 100.0)
    assert compared(lab, "a", "input-2")[-1].base_samples == 1
    readable(lab)


# Finding 2: core keeps one run of each side in flight per scope, and serves every need.


Step = tuple[Literal["call", "end"], int, int]


@pytest.mark.regression
@settings(max_examples=examples(pr=40, nightly=400))
@given(
    floor=st.sampled_from((FLOOR, 0.0)),
    spread=st.sampled_from((0.0, 0.01, 0.05)),
    steps=st.lists(
        st.one_of(
            st.tuples(st.just("call"), st.integers(0, 1), st.integers(0, 9)),
            st.tuples(st.just("end"), st.integers(0, 15), st.just(0)),
        ),
        min_size=1,
        max_size=30,
    ),
    seed=st.integers(0, 2**16),
)
@example(
    floor=0.0,
    spread=0.0,
    steps=[step for n in range(10) for step in (("call", 0, n), ("end", 0, 0), ("end", 1, 0))],
    seed=0,
)
def test_core_keeps_one_run_a_side_in_flight_per_scope_and_serves_every_need(
    floor: float, spread: float, steps: list[Step], seed: int
) -> None:
    """Two attempts' agents evaluate up to ten inputs while runs end in any order.

    After every step, each attempt has at most one of core's base runs and one of its
    candidate runs in flight (before, one per input). Once every run has ended with a sample,
    every comparison has the runs its close call earns, so no need was dropped while it waited
    for the side's place in flight: under no floor a close call holds three runs a side unless
    no further run could settle it, under a floor its base holds ``BASE_MAX_SAMPLES``.
    """
    names = ("a", "b")
    lab = Lab.of(*names, floor=floor)
    bench = Bench(
        {f"input-{n}": 100.0 * (1 + n / 1000) for n in range(10)} | {"base": 100.0},
        spread,
        seeded(f"in-flight:{seed}"),
    )

    def capped() -> None:
        assert all(count <= 1 for count in core_in_flight(lab).values()), core_in_flight(lab)

    def ended(run: core.SubmitMeasurement) -> None:
        assert run.plan.measured_input is not None
        end(lab, run, "sample", bench.read(run.plan.measured_input))

    for kind, who, what in steps:
        if kind == "call":
            lab.call(names[who], f"{names[who]}-{what}", f"input-{what}")
        elif live := running(lab):
            ended(live[who % len(live)])
        capped()
    for _ in range(400):
        if not (live := running(lab)):
            break
        ended(live[0])
        capped()
    assert running(lab) == [], "the runs a comparison takes are bounded"
    readable(lab)
    for name in names:
        served(lab, name, floor=floor)


def served(lab: Lab, name: str, *, floor: float) -> None:
    """Every close call of ``name``'s comparisons holds the runs it earns, once all have ended."""
    moment = core.moment_of(lab.state.run)
    held = {c.candidate_input for c in lab.comparisons() if c.scope == lab.scopes[name]}
    for comparison in core.latest_comparisons(lab.state.evaluation, lab.scopes[name], held, moment):
        if not core.close_call(comparison) or core.verdict_unreachable(comparison, moment.policy):
            continue
        if floor:
            assert comparison.base_samples == core.BASE_MAX_SAMPLES, comparison
        else:
            assert (comparison.candidate_samples, comparison.base_samples) == (
                core.MIN_SAMPLES_WITHOUT_FLOOR,
                core.MIN_SAMPLES_WITHOUT_FLOOR,
            ), comparison


# Finding 3: a comparison is settling while any run that feeds it is under way.


def feeds(lab: Lab, comparison: core.Comparison) -> bool:
    """Whether a run still under way will add a sample to ``comparison``: of its base in any
    scope, or of its candidate's input in its own scope."""
    return any(
        run.plan.measured_input == comparison.base_input
        or (run.scope == comparison.scope and run.plan.measured_input == comparison.candidate_input)
        for run in running(lab)
    )


@pytest.mark.regression
@settings(max_examples=examples(pr=40, nightly=400))
@given(
    floor=st.sampled_from((FLOOR, 0.0)),
    steps=st.lists(
        st.one_of(
            st.tuples(st.just("call"), st.integers(0, 1), st.integers(0, 3)),
            st.tuples(st.just("end"), st.integers(0, 7), st.integers(0, 3)),
        ),
        min_size=1,
        max_size=16,
    ),
)
@example(
    floor=FLOOR,
    steps=[("call", 0, 1), ("end", 1, 0), ("end", 0, 1), ("call", 0, 2), ("end", 1, 2)],
)
def test_a_comparison_is_settling_exactly_while_a_run_that_feeds_it_is_under_way(
    floor: float, steps: list[Step]
) -> None:
    """Before, only core's runs taken for that same candidate input counted, so a base run
    another input's close call asked for left this comparison reported final, and it changed."""
    names = ("a", "b")
    lab = Lab.of(*names, floor=floor)
    values = {"base": 100.0, "input-0": 100.0, "input-1": 100.5, "input-2": 100.4, "input-3": 110.0}
    for kind, who, what in steps:
        if kind == "call":
            lab.call(names[who], f"{names[who]}-{what}", f"input-{what}")
        elif live := running(lab):
            run = live[who % len(live)]
            assert run.plan.measured_input is not None
            end(lab, run, "sample", values[run.plan.measured_input] + what * 0.1)
        moment = core.moment_of(lab.state.run)
        for name in names:
            held = {c.candidate_input for c in lab.comparisons() if c.scope == lab.scopes[name]}
            for comparison in core.latest_comparisons(
                lab.state.evaluation, lab.scopes[name], held, moment
            ):
                assert core.comparison_settling(lab.state.evaluation, comparison) == feeds(
                    lab, comparison
                ), comparison
