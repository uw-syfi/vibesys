"""The official record core's ledger keeps states the settled comparison's candidate value.

The official measurement of an input is answered by every sample the attempt holds of it, and
its ledger record states their mean: the figure every reader ranks by. A close call earns the
candidate more runs (`remeasures_candidate`), and they land after the answer was stored. The
record then has to state the mean they make too, or the round's headline (the settled
comparison) and the ledger (`measurements_of`, resume receipts) state two values for one
measurement. These tests drive the kernel through its public events (`Lab`) and put the
official request at every point among core's runs and their ends.
"""

from __future__ import annotations

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_sim.api.testing import examples

from .test_comparison_liveness import Bench, running, seeded
from .test_measurement_comparison import Lab, ended_with, final_check, official_reading
from .test_measurement_reuse import revision

INPUT = "input-1"


def end_run(lab: Lab, run: core.SubmitMeasurement, bench: Bench) -> None:
    """End ``run``: a new run reads the bench, a reusing one re-reads the run it reuses."""
    assert run.request_id is not None
    assert run.plan.measured_input is not None
    lab.ended.add(run.request_id.root)
    if run.reuses is None:
        lab.state = ended_with(lab.state, run, bench.read(run.plan.measured_input))
        return
    source = next(s for s in lab.state.evaluation.samples if s.run == run.reuses)
    lab.state = ended_with(lab.state, run, source.reading.value, reused=run.reuses)


@pytest.mark.regression
@settings(max_examples=examples(pr=40, nightly=400))
@given(
    spread=st.sampled_from((0.0, 0.01, 0.05)),
    steps=st.lists(st.integers(0, 7), max_size=10),
    at=st.integers(0, 10),
    seed=st.integers(0, 2**16),
)
@example(spread=0.01, steps=[0, 0], at=2, seed=0)
def test_the_official_record_states_the_settled_comparison_s_value(
    spread: float, steps: list[int], at: int, seed: int
) -> None:
    """Once every run is in, the record and the comparison state one value for the input.

    The check comes after ``at`` of the run ends (in any order), whenever core's runs are.
    Before, the record kept the mean held when the check was answered, and a close call's runs
    that landed after it moved only the comparison.
    """
    lab = Lab.of("a", floor=0.0)
    bench = Bench({INPUT: 100.4, "base": 100.0}, spread, seeded(f"answer:{seed}"))
    lab.call("a", "c1", INPUT)
    check: core.SubmitMeasurement | None = None
    for n, which in enumerate([*steps, 0, 0, 0]):
        if n == at and check is None:
            check = final_check(lab, "a", "c1", INPUT)
        if live := running(lab):
            end_run(lab, live[which % len(live)], bench)
    if check is None:
        check = final_check(lab, "a", "c1", INPUT)
    while live := running(lab):
        end_run(lab, live[0], bench)
    settled = [c for c in lab.comparisons() if c.candidate_input == INPUT][-1]
    stated = official_reading(lab, check)
    assert stated.value == settled.candidate_value
    ranked = [
        item.reading
        for item in lab.state.evaluation.measurements_of(revision("c1"))
        if item.purpose == "official" and item.kind is core.EvidenceKind.BENCHMARK
    ]
    assert ranked
    assert all(reading == stated for reading in ranked), "core's re-runs state the same answer"
