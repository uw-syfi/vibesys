"""Every reconciliation command ends, and a cancel only goes where it can be answered.

REVIEW-8 P1-1: a deadline sent ``CancelOwnedResource`` for any resource a record reported.
Only the operations executor answers that command, and it holds receipts for registered
operations only, so a session's lease, a workspace or a job got "unknown" back. REVIEW-8
P2-1: an inspection, cancel or block answered "unknown" (or never answered) stayed open
forever, so every wait on it counted it as live and nothing reported the hang.
"""

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core

from .test_recovery_successor_integrity import persisted_command, state_with_root

_SESSION = core.EnsureSession(
    request_id=core.RequestId(root="root"),
    scope=core.Scope(owner=core.initial_state().run.run_id, generation=0),
    spec=core.SessionSpec(
        session_id=core.SessionId(root="session"),
        role_id=core.RoleId(root="worker"),
        policy="reuse",
        lifetime="owner",
        access=core.Access.WRITE_ARTIFACTS,
    ),
)


def _ready(state: core.CoreState) -> core.CoreState:
    records = state.records.model_copy(
        update={"recovery": core.RecoveryBarrier(epoch=1, phase=core.RecoveryPhase.READY)}
    )
    run = state.run.model_copy(update={"status": core.RunStatus.RUNNING})
    return state.model_copy(update={"records": records, "run": run})


@pytest.mark.parametrize("owner", ["operation", "session"])
def test_a_deadline_cancels_only_a_registered_operations_resource(owner: str) -> None:
    """A session's lease is closed by Sessions, never by a cancel the operations side refuses."""
    state, root = state_with_root()
    if owner == "session":
        root = root.model_copy(update={"request": _SESSION})
        state = state.model_copy(
            update={"records": state.records.model_copy(update={"records": (root,)})}
        )
    result = core.step(
        _ready(state), core.ReconciliationDeadline(request_id=root.request_id, now_at=100.0)
    )
    cancels = [
        request for request in result.requests if isinstance(request, core.CancelOwnedResource)
    ]
    assert [request.resource_id for request in cancels] == (
        [core.ResourceId(root="physical")] if owner == "operation" else []
    )
    [blocked] = [row for row in result.state.records.records if row.request_id == root.request_id]
    assert blocked.phase == core.RequestPhase.BLOCKED


def _command(kind: str, root: core.RequestRecord) -> core.Request:
    common = {
        "request_id": core.RequestId(root=f"{kind}-of-root"),
        "scope": root.request.scope,
        "target": root.request_id,
    }
    match kind:
        case "inspect":
            return core.InspectRequest(**common)
        case "cancel":
            return core.CancelOwnedResource(**common, resource_id=core.ResourceId(root="physical"))
        case _:
            return core.BlockRequest(**common, reason=core.FailureReason.TIMED_OUT)


@given(
    kind=st.sampled_from(["inspect", "cancel", "block"]),
    phase=st.sampled_from(
        [core.RequestPhase.PREPARED, core.RequestPhase.DISPATCHED, core.RequestPhase.RECONCILING]
    ),
    answer=st.sampled_from([None, core.ObservationStatus.UNKNOWN, core.ObservationStatus.FAILED]),
    early=st.floats(min_value=0.0, max_value=49.0),
    late=st.floats(min_value=0.0, max_value=1000.0),
)
def test_an_open_reconciliation_command_ends_at_its_limit_and_not_before(
    kind: str,
    phase: core.RequestPhase,
    answer: core.ObservationStatus | None,
    early: float,
    late: float,
) -> None:
    """An unanswered or inconclusively answered command is live until its limit, then blocked."""
    state, root = state_with_root()
    root = root.model_copy(update={"phase": core.RequestPhase.BLOCKED})
    request = _command(kind, root)
    observation = (
        None
        if answer is None
        else core.Observation(
            event_id=core.EventId(root="command-answer"),
            request_id=request.request_id,
            scope=request.scope,
            sequence=1,
            observed_at=10.0,
            status=answer,
            terminal=False,
        )
    )
    command = persisted_command(root, request).model_copy(
        update={
            "phase": phase,
            "observation": observation,
            "until": core.Until(at=50.0),
            "lifecycle": core.LifecycleClass.IDEMPOTENT_WRITE,
        }
    )
    root = root.model_copy(update={"addressed_by": (command.request_id,)})
    records = state.records.model_copy(update={"records": (root, command)})
    state = _ready(state.model_copy(update={"records": records}))
    assert core.project(state).next_observe_at is not None

    def phase_at(now_at: float) -> core.RequestPhase:
        result = core.step(state, core.ClockAdvanced(now_at=now_at))
        [row] = [
            row for row in result.state.records.records if row.request_id == request.request_id
        ]
        return row.phase

    assert phase_at(early) == phase
    assert phase_at(50.0 + late) == core.RequestPhase.BLOCKED
