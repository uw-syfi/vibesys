"""A revision that lacks a capability the task requires is measured once, never again.

The evaluator's `error` record can say why a run produced no row. When it says
`requirement_not_met` (the measured revision lacks a capability the task requires, such as a
prefix cache), the run's benchmark evidence names the requirement (`EvidenceRef.unmet_requirement`)
and measuring that revision again cannot help, so core takes no further run of it: not the base
run each attempt's comparison asks for, and not a replacement for the strategy's own measurement
of the input. A run that ends for any other reason keeps its retry (a node fault, a lost job).
These tests drive the kernel through its public events, as `test_comparison_liveness` does.
"""

from __future__ import annotations

from typing import Literal

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from vs_sim.api.testing import examples

from .test_baseline_in_flight import strategy_baseline
from .test_comparison_liveness import Outcome, base_runs, end
from .test_measurement_comparison import Lab

Role = Literal["strategy-input", "another-attempts-base", "later-attempt"]
ROLES: tuple[Role, ...] = ("strategy-input", "another-attempts-base", "later-attempt")


def _lab_where_the_base_run_fails(role: Role, kind: Outcome) -> Lab:
    """The base's run ends ``kind`` without a sample; ``role`` says whose run it was.

    The candidate of attempt ``b`` holds a sample throughout, so whether anything asks for the
    base again is the only thing left to decide.
    """
    lab = Lab.of("a", "b")
    if role == "strategy-input":
        own = strategy_baseline(lab)
        (candidate,) = lab.call("b", "c1", "input-1")
        end(lab, candidate, "sample", 300.0)
        end(lab, own, kind)
        return lab
    _, first = lab.call("a", "c1", "input-1")
    (candidate,) = lab.call("b", "c2", "input-2")
    end(lab, candidate, "sample", 300.0)
    end(lab, first, kind)
    if role == "later-attempt":
        lab.call("b", "c3", "input-3")
    return lab


@pytest.mark.regression
@pytest.mark.parametrize("role", ROLES)
def test_a_base_that_fails_requirement_not_met_is_measured_exactly_once(role: Role) -> None:
    """Before, each attempt's comparison asked for the base again: a deterministic failure repeated.

    The strategy's measurement of the input ended without a sample, and core could not tell it
    would end the same way, so the first comparison that needed the base measured it afresh.
    """
    lab = _lab_where_the_base_run_fails(role, "requirement-not-met")
    assert len(base_runs(lab)) == 1, "the failed run is the only run of the base"


@pytest.mark.parametrize("role", ROLES[:2])
def test_a_base_that_fails_for_another_reason_is_still_measured_again(role: Role) -> None:
    lab = _lab_where_the_base_run_fails(role, "infrastructure")
    assert len(base_runs(lab)) == 2, "the machinery's failure says nothing of the next run"


@pytest.mark.regression
@settings(max_examples=examples(pr=15, nightly=60))
@given(attempts=st.integers(min_value=1, max_value=4), calls=st.integers(min_value=1, max_value=3))
@example(attempts=2, calls=1)
def test_no_candidate_of_any_attempt_earns_a_second_run_of_a_base_that_cannot_be_measured(
    attempts: int, calls: int
) -> None:
    names = [f"attempt-{index}" for index in range(attempts)]
    lab = Lab.of(*names)
    own = strategy_baseline(lab)
    end(lab, own, "requirement-not-met")
    for name in names:
        for call in range(calls):
            for run in lab.call(name, f"{name}-c{call}", f"{name}-input-{call}"):
                end(lab, run, "sample", 300.0 + call)
    assert base_runs(lab) == [own]
