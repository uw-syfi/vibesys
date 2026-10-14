"""What a finished child asserts to core when it closes its workspace."""

from __future__ import annotations

from hypothesis import given, settings
from tests.vibesys.orchestration.evolve._host import TERMS
from tests.vibesys.orchestration.evolve._values import (
    closings,
    passing_closing,
    revision,
    start,
)

from vibesys.orchestration.evolve.search.api import STOPPED, Closing, FailureKind, assertion
from vs_sim.api.testing import examples


@settings(max_examples=examples(pr=8, nightly=100))
@given(closing=closings())
def test_only_a_child_that_passed_everything_is_eligible(closing: Closing) -> None:
    for cold in (True, False):
        settle = assertion(start(cold=cold), closing, TERMS)
        assert not settle.eligible or closing.passed


@settings(max_examples=examples(pr=8, nightly=100))
@given(closing=closings())
def test_a_cold_start_that_failed_after_leaving_work_keeps_it_as_work_in_progress(
    closing: Closing,
) -> None:
    settle = assertion(start(cold=True), closing, TERMS)
    settle_child = assertion(start(cold=False), closing, TERMS)
    assert settle_child.retention != "wip"
    stopped = closing.cause is not None and closing.cause.kind is FailureKind.STOPPED
    kept = closing.revision is not None and not stopped
    if settle_child.retention == "discard" and kept:
        assert (settle.retention, settle.candidate, settle.eligible) == (
            "wip",
            closing.revision,
            False,
        )
    else:
        assert settle == settle_child


def test_a_stopped_cold_start_discards_its_work() -> None:
    closing = Closing(revision=revision("c1")).ended_by(STOPPED)
    assert assertion(start(cold=True), closing, TERMS).retention == "discard"


def test_a_passing_child_is_retained_as_an_eligible_candidate() -> None:
    settle = assertion(start(cold=False), passing_closing("c1", 100.0), TERMS)
    assert (settle.eligible, settle.retention, settle.candidate) == (
        True,
        "candidate",
        revision("c1"),
    )
