"""The liveness checks flag a repeated request exactly when no new observation separates it."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from vs_core.api import (
    CollectEvidence,
    EventId,
    Observation,
    ObservationStatus,
    ObserveOwnedJob,
    RequestId,
    ResourceId,
    RunId,
    Scope,
)

# test-isolation: the liveness checker is test support published at vs_core.testing; vs_core.api carries no test support.
from vs_core.testing.liveness import Invariant, Journal, spin_violations

SCOPE = Scope(owner=RunId(root="run"), generation=0)
JOB = ResourceId(root="job:1")


def _collect(index: int) -> CollectEvidence:
    return CollectEvidence(
        request_id=RequestId(root=f"collect:{index}"), scope=SCOPE, resource_id=JOB, deadline_at=1.0
    )


def _observe(index: int) -> ObserveOwnedJob:
    return ObserveOwnedJob(
        request_id=RequestId(root=f"observe:{index}"), scope=SCOPE, resource_id=JOB, deadline_at=1.0
    )


def _answer(
    request_id: str, *, terminal: bool, status: ObservationStatus, diagnostic: str = ""
) -> Observation:
    return Observation(
        event_id=EventId(root=f"{request_id}:0"),
        request_id=RequestId(root=request_id),
        scope=SCOPE,
        sequence=0,
        observed_at=1.0,
        status=status,
        accepted=True,
        terminal=terminal,
        diagnostic=diagnostic,
    )


def _failed_job(request_id: str) -> Observation:
    return _answer(request_id, terminal=True, status=ObservationStatus.FAILED)


@given(st.integers(min_value=2, max_value=30))
def test_collections_answered_by_the_same_job_observation_are_a_spin(count: int) -> None:
    journal = Journal()
    for index in range(count):
        journal.issue(_collect(index))
        journal.observe(_failed_job(f"collect:{index}"))
    found = spin_violations(journal)
    assert [item.invariant for item in found] == [Invariant.SPIN]
    assert "collect_evidence" in found[0].detail
    assert f"{count - 1} repeats" in found[0].detail


@given(st.integers(min_value=2, max_value=30))
def test_a_repeat_after_new_information_is_not_a_spin(count: int) -> None:
    """Each collection is separated from the next by an observation nobody had seen."""
    journal = Journal()
    for index in range(count):
        journal.issue(_collect(index))
        journal.observe(_failed_job(f"collect:{index}"))
        journal.observe(
            _answer(
                "other", terminal=False, status=ObservationStatus.PENDING, diagnostic=str(index)
            )
        )
    assert spin_violations(journal) == []


@given(st.integers(min_value=2, max_value=30))
def test_polling_a_pending_job_is_not_a_spin(count: int) -> None:
    journal = Journal()
    for index in range(count):
        journal.issue(_observe(index))
        journal.observe(
            _answer(f"observe:{index}", terminal=False, status=ObservationStatus.PENDING)
        )
    assert spin_violations(journal) == []


def test_a_request_with_no_answer_yet_may_be_asked_again() -> None:
    journal = Journal()
    journal.issue(_collect(0))
    journal.issue(_collect(1))
    assert spin_violations(journal) == []
