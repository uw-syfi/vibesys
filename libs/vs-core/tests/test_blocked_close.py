"""A closing run never waits on a blocked record that nothing can end.

A request whose executor answered without a conclusion is blocked at its reconciliation
bound. The strategy hears ``RequestBlocked`` and may act on it, but once the run closes the
close itself waits on the record. Core ends such a record with a typed failure unless a live
cancellation or inspection still addresses it (``BlockedRecordStalled``), so a close always
reaches a terminal status: for any record kind, answer, host restart and lateness.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

import pytest
from hypothesis import strategies as st

import vs_core.api as core

from .test_silent_executor_deadline import _silent

if TYPE_CHECKING:
    from collections.abc import Iterable

_STOP = core.RunControlEvent(
    control=core.ControlInput(
        control_id=core.ControlId(root="stop"), action="stop", mode=core.StopMode.DRAIN
    ),
    result=core.RunResultProposal(outcome="cancelled", reason="operator stop"),
    now_at=0.5,
)

_SILENT_ANSWERS = [
    (core.RequestPhase.RECONCILING, core.ObservationStatus.UNKNOWN),
    (core.RequestPhase.DISPATCHED, core.ObservationStatus.FAILED),
]

# The lifecycles whose closure hold ends with their record: nothing is owned beyond it.
_ENDS_WITH_RECORD = (core.LifecycleClass.IDEMPOTENT_WRITE, core.LifecycleClass.QUERY)


def _record(
    lifecycle: core.LifecycleClass,
    answer: tuple[core.RequestPhase, core.ObservationStatus],
    *,
    accepted: bool,
    named: bool,
    answered: bool = True,
) -> core.CoreState:
    """A run with one open record whose executor answered without a conclusion and went silent.

    Unless ``answered`` is false: then the executor never answered at all (a slow submission).
    """
    phase, status = answer
    state = _silent(phase, status, terminal=False)
    [record] = state.records.records
    assert record.observation is not None
    observation = record.observation.model_copy(
        update={
            "accepted": accepted,
            "resource_id": core.ResourceId(root="held") if named else None,
        }
    )
    held = record.model_copy(
        update={
            "lifecycle": lifecycle,
            "observation": observation if answered else None,
            "phase": phase if answered else core.RequestPhase.DISPATCHED,
        }
    )
    return state.model_copy(
        update={"records": state.records.model_copy(update={"records": (held,)})}
    )


def _original(state: core.CoreState) -> core.RequestRecord:
    """The silent record, not the block or cancellation requests core sent about it."""
    return next(row for row in state.records.records if row.request_id.root == "setup")


def _answer(request: core.Request, now_at: float) -> core.Observation:
    """What a well-behaved executor says to the requests core itself sends while closing."""
    cancelled = isinstance(request, core.CancelOwnedResource)
    return core.Observation(
        event_id=core.EventId(
            root=f"answer:{request.request_id.root if request.request_id else ''}"
        ),
        request_id=request.request_id or core.RequestId(root="unnamed"),
        scope=request.scope,
        sequence=0,
        observed_at=now_at,
        status=core.ObservationStatus.CANCELLED if cancelled else core.ObservationStatus.SUCCEEDED,
        resource_id=request.resource_id if cancelled else None,
        accepted=True,
        terminal=True,
        released=True,
        children_complete=True,
        admission_id=request.admission_id,
    )


def _settle(
    state: core.CoreState, requests: Iterable[core.Request], now_at: float
) -> core.CoreState:
    """Authorize and answer every request core sends, until it sends no more."""
    pending = list(requests)
    for _ in range(20):
        if not pending:
            break
        request = pending.pop(0)
        if request.request_id is None:
            continue
        state = core.step(state, core.DispatchAuthorized(request_id=request.request_id)).state
        result = core.step(state, core.RequestObserved(observation=_answer(request, now_at)))
        state = result.state
        pending.extend(result.requests)
    return state


class _Case(NamedTuple):
    """One generated world: a record kind and answer, a host restart, and how late it is."""

    lifecycle: core.LifecycleClass
    answer: tuple[core.RequestPhase, core.ObservationStatus]
    accepted: bool
    named: bool
    restart: bool
    stop_first: bool
    late_by: float


_CASES = st.builds(
    _Case,
    lifecycle=st.sampled_from(list(core.LifecycleClass)),
    answer=st.sampled_from(_SILENT_ANSWERS),
    accepted=st.booleans(),
    named=st.booleans(),
    restart=st.booleans(),
    stop_first=st.booleans(),
    late_by=st.floats(min_value=0.0, max_value=500.0),
)


@pytest.mark.parametrize("kind", list(core.WaitKind))
def test_every_wait_kind_has_a_resolution(kind: core.WaitKind) -> None:
    assert kind in core.RESOLUTIONS


_DID_NOT_SUCCEED = {
    core.ObservationStatus.FAILED,
    core.ObservationStatus.REJECTED,
    core.ObservationStatus.CANCELLED,
}


class _Later(NamedTuple):
    """What a later host's executor says about a record the close already ended."""

    status: core.ObservationStatus
    accepted: bool
    released: bool
    children_complete: bool
    terminal: bool


_LATER_ANSWERS = st.builds(
    _Later,
    status=st.sampled_from(list(core.ObservationStatus)),
    accepted=st.booleans(),
    released=st.booleans(),
    children_complete=st.booleans(),
    terminal=st.booleans(),
)


def _judged(run: core.CoreState, answer: core.Observation) -> tuple[object, ...]:
    """What core makes of ``answer``: the typed contradiction, a refusal, or the record it leaves.

    The record is read without the answer's own name and number, which differ by construction.
    """
    try:
        after = core.step(run, core.RequestObserved(observation=answer)).state
    except core.ClosedRecordContradictionError:
        return ("contradiction",)
    except core.ContractError as error:
        return ("refused", error.path, error.detail)
    record = _original(after)
    seen = record.observation
    assert seen is not None
    return (
        record.phase,
        seen.status,
        seen.accepted,
        seen.resource_id,
        seen.released,
        seen.children_complete,
        seen.diagnostic,
    )
