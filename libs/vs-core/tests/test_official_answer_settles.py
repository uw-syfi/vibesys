"""The official record core's ledger keeps states the figure the strategy took its verdict on.

The official measurement of an input is answered by every sample the attempt holds of it, and
its ledger record states their mean: the figure every reader ranks by. A close call earns the
candidate more runs (`remeasures_candidate`), and they land after the answer was stored, so
the record is restated as they land (#3187), until the strategy's flow takes its verdict: when
the comparison settles, or at the bound core states for it (``StatedComparison.answer_until``).
From then on it never moves, or the round's headline and the ledger (`measurements_of`, resume
receipts) would state two values for one measurement again (#3208 review LOW-1). These tests
drive the kernel through its public events (`Lab`) and put the official request at every
point among core's runs and their ends.
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


def official_job(lab: Lab, check: core.SubmitMeasurement) -> core.OwnedJob | None:
    return next(
        (job for job in lab.state.evaluation.jobs if job.submission_id == check.request_id), None
    )


def stated_now(lab: Lab) -> core.StatedComparison | None:
    """The view's comparison of the input in attempt ``a``, as the strategy's flow reads it."""
    return next(
        (
            stated
            for stated in core.run_view(lab.state).comparisons
            if stated.comparison.scope == lab.scopes["a"]
            and stated.comparison.candidate_input == INPUT
        ),
        None,
    )


class Answer:
    """Watches the official record across steps: open, it follows the comparison; closed, it
    holds the figure the flow read when it closed, for good."""

    def __init__(self) -> None:
        self.closed: core.Reading | None = None

    def check(self, lab: Lab, check: core.SubmitMeasurement | None) -> None:
        job = None if check is None else official_job(lab, check)
        if check is None or job is None or job.answer_until is None:
            return
        record = official_reading(lab, check)
        stated = stated_now(lab)
        if self.closed is not None:
            assert record == self.closed, "a closed answer never moves"
            return
        if stated is not None:
            assert record.value == stated.comparison.candidate_value
            assert stated.answer_until == job.answer_until
        if lab.state.run.now_at >= job.answer_until:
            assert stated is None or not stated.settling, "closed only once it settled"
            self.closed = record


@pytest.mark.regression
@settings(max_examples=examples(pr=40, nightly=400))
@given(
    spread=st.sampled_from((0.0, 0.01, 0.05)),
    steps=st.lists(st.integers(0, 7), max_size=10),
    at=st.integers(0, 10),
    seed=st.integers(0, 2**16),
)
@example(spread=0.01, steps=[0, 0], at=2, seed=0)
def test_the_official_record_states_the_figure_its_verdict_was_taken_on(
    spread: float, steps: list[int], at: int, seed: int
) -> None:
    """While the answer is open the record states the comparison's value; then it holds.

    The check comes after ``at`` of the run ends (in any order), whenever core's runs are.
    Before #3187 the record kept the mean held when the check was answered, and a close call's
    runs that landed after it moved only the comparison; before LOW-1 a run that landed after
    the flow's verdict moved the record again.
    """
    lab = Lab.of("a", floor=0.0)
    bench = Bench({INPUT: 100.4, "base": 100.0}, spread, seeded(f"answer:{seed}"))
    lab.call("a", "c1", INPUT)
    answer = Answer()
    check: core.SubmitMeasurement | None = None
    for n, which in enumerate([*steps, 0, 0, 0]):
        if n == at and check is None:
            check = final_check(lab, "a", "c1", INPUT)
        if live := running(lab):
            end_run(lab, live[which % len(live)], bench)
            answer.check(lab, check)
    if check is None:
        check = final_check(lab, "a", "c1", INPUT)
    while live := running(lab):
        end_run(lab, live[0], bench)
        answer.check(lab, check)
    assert answer.closed is not None, "every answer closes once its comparison settles"
    stated = official_reading(lab, check)
    ranked = [
        item.reading
        for item in lab.state.evaluation.measurements_of(revision("c1"))
        if item.purpose == "official" and item.kind is core.EvidenceKind.BENCHMARK
    ]
    assert ranked
    assert all(reading == stated for reading in ranked), "core's re-runs state the same answer"


@pytest.mark.regression
def test_a_run_that_lands_after_the_bound_does_not_restate_the_record() -> None:
    """The comparison is still settling when the bound core states for it passes.

    The flow takes its verdict then, on the record as it stands. Before, the close call's run
    that landed later restated the record, and the round's digest and the ledger parted.
    """
    lab = Lab.of("a", floor=0.0)
    bench = Bench({INPUT: 100.4, "base": 100.0}, 0.01, seeded("bound"))
    lab.call("a", "c1", INPUT)
    check = final_check(lab, "a", "c1", INPUT)
    while stated_now(lab) is None:
        end_run(lab, next(run for run in running(lab) if run != check), bench)
    end_run(lab, check, bench)
    stated = stated_now(lab)
    assert stated is not None
    assert stated.settling, "core's runs for the comparison are still under way"
    assert stated.answer_until is not None
    held = official_reading(lab, check)
    lab.advance(stated.answer_until - lab.state.run.now_at)
    while live := running(lab):
        end_run(lab, live[0], bench)
    assert official_reading(lab, check) == held
    ranked = [
        item.reading
        for item in lab.state.evaluation.measurements_of(revision("c1"))
        if item.purpose == "official" and item.kind is core.EvidenceKind.BENCHMARK
    ]
    assert len(ranked) > 1, "core re-ran the official measurement after the bound"
    assert all(reading == held for reading in ranked), "its re-runs state the closed figure"
