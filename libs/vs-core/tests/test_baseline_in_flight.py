"""A base the strategy is already measuring is awaited, never measured a second time (#3047).

The strategy measures the run's input before any agent works. That run names no measured
input, so when an agent's evaluation needs the same revision as its base while the strategy's
run is still going, core used to start a second run of it: a wasted job on a contended cluster.
Core now claims the running one: its result becomes the base's samples when it ends, and the
candidates compared with it follow. These tests drive the kernel through its public events.
"""

from __future__ import annotations

from typing import Literal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_sim.api.testing import examples

from .test_measurement_comparison import FLOOR, Lab, ended_with
from .test_measurement_reuse import submissions
from .test_measurements import plan, requested

BASE = core.initial_state().run.facts.baseline
DECISIVE = (0.5, 1.4, 1.6, 2.0)
"""The ratio each measured input has over the base: far enough from one that no close call earns
the base another run (an input's every run reads the same ratio, so they aggregate to it)."""


def strategy_baseline(lab: Lab) -> core.SubmitMeasurement:
    """The strategy's own measurement of the run's input: a baseline plan naming no input."""
    asked = requested(
        lab.state,
        identity="baseline",
        measurement=plan(purpose="baseline", dedicated_measurement_resources=True),
    )
    lab.state = asked.state
    (run,) = [r for r in asked.requests if isinstance(r, core.SubmitMeasurement)]
    return run


def end(lab: Lab, run: core.SubmitMeasurement, value: float | None) -> None:
    """End ``run`` passed, its benchmark record reading ``value``."""
    assert run.request_id is not None
    lab.ended.add(run.request_id.root)
    lab.state = ended_with(lab.state, run, value)


def base_runs(lab: Lab) -> list[core.SubmitMeasurement]:
    """Every run of the base revision, whoever asked for it."""
    return [r for r in submissions(lab.state) if r.plan.purpose == "baseline"]


def running(lab: Lab) -> list[core.SubmitMeasurement]:
    return [
        r
        for r in submissions(lab.state)
        if r.request_id is not None and r.request_id.root not in lab.ended
    ]


def running_bases(lab: Lab) -> list[core.SubmitMeasurement]:
    """The runs of the base under way; without a floor, core also runs the candidate again."""
    return [r for r in running(lab) if r.plan.purpose == "baseline"]


def test_an_agent_evaluating_while_the_strategys_baseline_runs_starts_no_second_base_run() -> None:
    lab = Lab.of("a")
    own = strategy_baseline(lab)
    asked = lab.call("a", "c1", "input-1")
    assert [r.plan.purpose for r in asked] == ["local-validation"]
    assert base_runs(lab) == [own]


def test_the_candidate_is_compared_when_the_strategys_baseline_ends_after_it() -> None:
    lab = Lab.of("a")
    own = strategy_baseline(lab)
    (candidate,) = lab.call("a", "c1", "input-1")
    end(lab, candidate, 130.0)
    assert lab.comparisons() == (), "no base sample yet, so nothing to compare with"
    end(lab, own, 100.0)
    (held,) = lab.comparisons()
    assert held.ratio == 1.3
    assert (held.base, held.base_input, held.base_samples) == (BASE, "base", 1)
    assert [s.run for s in lab.state.evaluation.samples if s.measured_input == "base"] == [
        own.request_id
    ]


def test_the_candidate_is_compared_when_the_strategys_baseline_ended_before_the_call() -> None:
    lab = Lab.of("a")
    own = strategy_baseline(lab)
    end(lab, own, 100.0)
    asked = lab.call("a", "c1", "input-1")
    assert [r.plan.purpose for r in asked] == ["local-validation"]
    end(lab, asked[0], 130.0)
    (held,) = lab.comparisons()
    assert (held.ratio, held.base_samples) == (1.3, 1)
    assert base_runs(lab) == [own]


@pytest.mark.regression
def test_a_strategy_baseline_that_ends_without_a_reading_leaves_the_base_to_core() -> None:
    """The claim holds no sample, so core asks for the base itself as the claimed run ends.

    Before, the candidate that claimed it waited until another evaluation asked for the base.
    """
    lab = Lab.of("a")
    own = strategy_baseline(lab)
    lab.call("a", "c1", "input-1")
    end(lab, own, None)
    (replaced,) = base_runs(lab)[1:]
    assert replaced.plan.measured_input == "base"
    asked = lab.call("a", "c2", "input-2")
    assert [r.plan.purpose for r in asked] == ["local-validation"], "one base run serves both"


@pytest.mark.regression
@pytest.mark.parametrize(
    ("floor", "most"),
    [(FLOOR, core.BASE_MAX_SAMPLES), (0.0, core.MIN_SAMPLES_WITHOUT_FLOOR)],
    ids=["declared-floor", "no-floor"],
)
def test_a_close_call_awaits_the_strategys_running_baseline_and_counts_it(
    floor: float, most: int
) -> None:
    """A close call that earns the base its last run claims the strategy's run under way.

    The candidate lands within noise of its base, and the base's close-call runs (core's own)
    end one by one. The strategy starts measuring the same revision before the close call
    that would earn the base its last allowed run. Before, that close call started another
    base run beside the strategy's; now it claims the strategy's run, whose sample is the one
    that reaches the bound (`BASE_MAX_SAMPLES` under a declared floor, three without one).
    """
    lab = Lab.of("a", floor=floor)
    candidate, first = lab.call("a", "c1", "input-1")
    assert first.plan.purpose == "baseline"
    end(lab, first, 100.0)
    own: core.SubmitMeasurement | None = None
    if most == core.BASE_MAX_SAMPLES:
        own = strategy_baseline(lab)
    end(lab, candidate, 100.3)
    while len(base_runs(lab)) < most:
        (extra,) = running_bases(lab)
        own = strategy_baseline(lab)
        end(lab, extra, 100.1)
    assert own is not None
    assert core.close_call(lab.comparisons()[-1])
    assert base_runs(lab)[-1] == own, "the close call awaits the strategy's run"
    assert running_bases(lab) == [own], "and starts no run of the base beside it"
    end(lab, own, 100.2)
    assert running_bases(lab) == [], "the claimed run counts toward the bound"
    assert len(base_runs(lab)) == most
    assert lab.comparisons()[-1].base_samples == most


@settings(max_examples=examples(pr=40, nightly=400))
@given(
    steps=st.lists(
        st.one_of(
            st.tuples(st.just("call"), st.sampled_from(("a", "b")), st.integers(0, 3)),
            st.tuples(st.just("end"), st.integers(0, 5), st.just(0)),
        ),
        min_size=1,
        max_size=10,
    ),
)
def test_the_base_is_measured_exactly_once_however_the_runs_interleave(
    steps: list[tuple[Literal["call", "end"], str | int, int]],
) -> None:
    """With the strategy's baseline issued first, no interleaving of evaluations and run ends
    starts another run of the base, and every candidate that ended has a comparison with it."""
    lab = Lab.of("a", "b")
    own = strategy_baseline(lab)
    values = {f"input-{n}": 100.0 * DECISIVE[n] for n in range(len(DECISIVE))} | {"base": 100.0}
    for kind, who, input_number in steps:
        if kind == "call":
            assert isinstance(who, str)
            key = f"input-{input_number}"
            lab.call(who, f"c{len(submissions(lab.state))}", key)
        else:
            live = running(lab)
            if live:
                chosen = live[int(who) % len(live)]
                measured = chosen.plan.measured_input
                end(lab, chosen, 100.0 if measured is None else values[measured])
    for left in running(lab):
        measured = left.plan.measured_input
        end(lab, left, 100.0 if measured is None else values[measured])
    assert base_runs(lab) == [own]
    for comparison in lab.comparisons():
        assert comparison.base == BASE
        assert comparison.base_samples == 1
    ended = {
        s.measured_input
        for s in lab.state.evaluation.samples
        if s.measured_input != "base" and s.base is not None
    }
    compared = {c.candidate_input for c in lab.comparisons()}
    assert ended == compared
