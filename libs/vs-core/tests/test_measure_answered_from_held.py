"""A measurement core answers from what it holds is answered to its decision, after any resume.

The strategy measures the run's input once, and a search that resumes (a code change restarts
its flows from their checkpoints) measures it again. Core holds that measurement, so the repeat
is answered from the run that took it: no second run, and no refusal. The answer needs no
request to carry it, so it is addressed to the decision, and a decision core accepted that way
is complete. A host that saved a run before this (a decision accepted with no request and no
answer) has the answer given again when it resumes. These tests drive the kernel through its
public events and read what the search is told (`translate`), as the flow does.
"""

from __future__ import annotations

from typing import Literal

from hypothesis import example, given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_sim.api.testing import examples

from .test_measurement_comparison import ended_with
from .test_measurement_reuse import revision
from .test_measurements import plan, requested, roundtrip, transition

type Purpose = Literal["baseline", "official"]
type Ending = Literal["passed", "failed", "running"]


def answers(
    state: core.CoreState, step: core.Transition, decision: str
) -> list[core.MeasurementFinished]:
    """What the search is told in ``step`` that answers ``decision`` (an accepted one is not)."""
    told = core.translate(state, step.events).events
    asked = core.DecisionId(root=decision)
    return [
        item
        for item in told
        if isinstance(item, core.MeasurementFinished) and item.decision == asked
    ]


def submissions_of(state: core.CoreState) -> list[core.SubmitMeasurement]:
    return [
        r.request for r in state.records.records if isinstance(r.request, core.SubmitMeasurement)
    ]


def measured_input(*, failed: bool) -> tuple[core.CoreState, core.SubmitMeasurement]:
    """A run whose input was measured by decision ``measure.1``, which ended."""
    asked = requested(identity="measure.1", measurement=plan(purpose="baseline"))
    (run,) = [r for r in asked.requests if isinstance(r, core.SubmitMeasurement)]
    return ended_with(asked.state, run, 100.0, failed=failed), run


@given(failed=st.booleans(), repeats=st.integers(1, 4))
@example(failed=False, repeats=1)
@settings(max_examples=examples(pr=12, nightly=60), deadline=None)
def test_the_input_asked_for_again_is_answered_with_the_run_that_measured_it(
    *, failed: bool, repeats: int
) -> None:
    """Regression: the repeat was refused with an answer addressed to no decision, so the flow
    that asked waited for good (a resumed search measures its input again)."""
    state, first = measured_input(failed=failed)
    held = next(j for j in state.evaluation.jobs if j.submission_id == first.request_id)
    for number in range(2, 2 + repeats):
        decision = f"measure.{number}"
        again = requested(
            state, identity=decision, measurement=plan(purpose="baseline", submitted_at=number)
        )
        (answer,) = answers(again.state, again, decision)
        assert answer.result.source_request == first.request_id
        assert answer.result.evidence == held.evidence
        assert answer.result.status == held.status
        assert not again.requests, "the input is never measured a second time"
        receipt = next(r for r in again.state.run.receipts if r.decision_id.root == decision)
        assert receipt.completion is not None, "an answered decision is complete"
        state = again.state
    assert submissions_of(state) == [first]


@given(
    asks=st.lists(
        st.tuples(
            st.sampled_from(("baseline", "official")),
            st.sampled_from(("c1", "c2")),
            st.sampled_from(("passed", "failed", "running")),
        ),
        min_size=1,
        max_size=6,
    )
)
@example(asks=[("baseline", "c1", "passed"), ("baseline", "c1", "passed")])
@example(asks=[("official", "c1", "passed"), ("official", "c1", "passed")])
@settings(max_examples=examples(pr=25, nightly=200), deadline=None)
def test_a_measurement_core_accepts_has_a_request_or_an_answer_addressed_to_it(
    asks: list[tuple[Purpose, str, Ending]],
) -> None:
    """The class of the bug: whatever core accepts, it either starts a request that will answer
    it or answers it at once, to the decision, never to nobody."""
    state = core.initial_state()
    for number, (purpose, candidate, ending) in enumerate(asks, start=1):
        decision = f"measure.{number}"
        measurement = plan(
            purpose=purpose, candidate=revision(candidate), submitted_at=float(number)
        )
        step = requested(state, identity=decision, measurement=measurement)
        receipt = next(r for r in step.state.run.receipts if r.decision_id.root == decision)
        started = [r for r in step.requests if isinstance(r, core.SubmitMeasurement)]
        told = answers(step.state, step, decision)
        assert bool(started) != bool(told), (
            f"{decision} ({purpose} of {candidate}) has a request or an answer, not both or neither"
        )
        assert (receipt.completion is None) == bool(started), "only a request leaves it open"
        state = step.state
        if started and ending != "running":
            state = ended_with(state, started[0], 100.0, failed=ending == "failed")


def test_a_run_saved_before_the_answer_was_addressed_has_it_given_when_it_resumes() -> None:
    """A decision saved accepted, with no request and no completion, is answered on resume.

    This is the shape of a run a host saved before decisions answered at once were addressed:
    its flow waits on an answer nobody sends. Recovery asks core again, and core answers from
    what it holds; once answered, a later resume gives nothing more.
    """
    state, first = measured_input(failed=False)
    again = requested(state, identity="measure.2", measurement=plan(submitted_at=2.0))
    saved_receipts = tuple(
        r.model_copy(update={"completion": None}) if r.decision_id.root == "measure.2" else r
        for r in again.state.run.receipts
    )
    saved = again.state.model_copy(
        update={"run": again.state.run.model_copy(update={"receipts": saved_receipts})}
    )

    resumed = transition(roundtrip(saved), core.RecoveryStarted(epoch=2, now_at=50.0))
    (answer,) = answers(resumed.state, resumed, "measure.2")
    assert answer.result.source_request == first.request_id
    assert not [r for r in resumed.requests if isinstance(r, core.SubmitMeasurement)]

    later = transition(roundtrip(resumed.state), core.RecoveryStarted(epoch=3, now_at=60.0))
    assert answers(later.state, later, "measure.2") == [], "answered once; it stays answered"
