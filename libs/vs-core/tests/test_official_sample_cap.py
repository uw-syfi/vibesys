"""The strategy's official check never takes a candidate past the sample cap.

Without a declared floor core runs the candidate's input again itself, one run at a time, up to
``MIN_SAMPLES_WITHOUT_FLOOR`` samples. The official check of the same input adds a run only when
the samples held fall short of settling the comparison. When core's last re-run was still under
way, the check counted only the samples held, added its own run, and both landed: the candidate
ended with one sample more than the cap. These tests drive the kernel through its public events
(`Lab`) and put the official request at every point among core's runs and their ends.
"""

from __future__ import annotations

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_sim.api.testing import examples

from .test_comparison_liveness import Bench, end, running, seeded
from .test_measurement_comparison import Lab, final_check

INPUT = "input-1"


def candidate_samples(lab: Lab) -> int:
    return sum(s.measured_input == INPUT for s in lab.state.evaluation.samples)


def settle(lab: Lab, bench: Bench) -> None:
    while live := running(lab):
        run = live[0]
        assert run.plan.measured_input is not None
        end(lab, run, "sample", bench.read(run.plan.measured_input))


@pytest.mark.regression
def test_an_official_check_beside_core_s_last_rerun_does_not_add_a_run_past_the_cap() -> None:
    """The agent's run and one re-run are held, the second re-run is under way, then the check.

    Before, the check ran afresh and landed beside the re-run: four samples of one input.
    """
    lab = Lab.of("a", floor=0.0)
    mine, base = lab.call("a", "c1", INPUT)
    end(lab, mine, "sample", 101.0)
    end(lab, base, "sample", 100.0)
    (rerun,) = [r for r in running(lab) if r.plan.measured_input == INPUT]
    end(lab, rerun, "sample", 101.0)
    assert [r.plan.measured_input for r in running(lab) if r.plan.measured_input == INPUT]
    check = final_check(lab, "a", "c1", INPUT)
    assert check.reuses is not None, "answered by the samples held, not by a run of its own"
    settle(lab, Bench({INPUT: 101.0, "base": 100.0}, 0.0, seeded("regression")))
    assert candidate_samples(lab) == core.MIN_SAMPLES_WITHOUT_FLOOR


@settings(max_examples=examples(pr=60, nightly=600))
@given(
    spread=st.sampled_from((0.0, 0.01, 0.05)),
    steps=st.lists(st.integers(0, 7), max_size=10),
    at=st.integers(0, 10),
    seed=st.integers(0, 2**16),
)
@example(spread=0.0, steps=[0, 0, 0], at=2, seed=0)
def test_an_official_check_at_any_point_leaves_the_candidate_within_the_cap_and_is_answered(
    spread: int, steps: list[int], at: int, seed: int
) -> None:
    """The check comes after ``at`` of the run ends (in any order), whenever core's runs are.

    Afterwards the candidate holds at most ``MIN_SAMPLES_WITHOUT_FLOOR`` samples, and the check
    was answered: it reuses the samples held, or its own run ended with a sample.
    """
    lab = Lab.of("a", floor=0.0)
    bench = Bench({INPUT: 100.4, "base": 100.0}, spread, seeded(f"cap:{seed}"))
    lab.call("a", "c1", INPUT)
    check: core.SubmitMeasurement | None = None
    for n, which in enumerate(steps + [0] * 3):
        if n == at and check is None:
            check = final_check(lab, "a", "c1", INPUT)
        if live := running(lab):
            run = live[which % len(live)]
            assert run.plan.measured_input is not None
            end(lab, run, "sample", bench.read(run.plan.measured_input))
    if check is None:
        check = final_check(lab, "a", "c1", INPUT)
    settle(lab, bench)
    assert candidate_samples(lab) <= core.MIN_SAMPLES_WITHOUT_FLOOR
    assert check.request_id is not None
    answered = check.reuses is not None or any(
        s.run == check.request_id for s in lab.state.evaluation.samples
    )
    assert answered
