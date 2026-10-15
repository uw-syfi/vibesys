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

from .test_proof_ownership_regressions import stopped
from .test_recovery_successor_integrity import persisted_command, state_with_root
from .test_silent_executor_deadline import _silent

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


def _command(kind: str, root: core.RequestRecord, request_id: core.RequestId) -> core.Request:
    scope, target = root.request.scope, root.request_id
    match kind:
        case "inspect":
            return core.InspectRequest(request_id=request_id, scope=scope, target=target)
        case "cancel":
            return core.CancelOwnedResource(
                request_id=request_id,
                scope=scope,
                target=target,
                resource_id=core.ResourceId(root="physical"),
            )
        case _:
            return core.BlockRequest(
                request_id=request_id,
                scope=scope,
                target=target,
                reason=core.FailureReason.TIMED_OUT,
            )


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
    request_id = core.RequestId(root=f"{kind}-of-root")
    request = _command(kind, root, request_id)
    observation = (
        None
        if answer is None
        else core.Observation(
            event_id=core.EventId(root="command-answer"),
            request_id=request_id,
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


def _closing_with(*commands: core.RequestRecord) -> tuple[core.CoreState, core.RequestRecord]:
    """A closing run in epoch 1 holding a blocked record that names *commands*."""
    state = _silent(core.RequestPhase.BLOCKED, core.ObservationStatus.UNKNOWN, terminal=False)
    record = state.records.records[0]
    assert record.observation is not None
    held = record.model_copy(
        update={
            "observation": record.observation.model_copy(
                update={"accepted": True, "resource_id": core.ResourceId(root="lease")}
            ),
            "addressed_by": tuple(
                command.request_id
                for command in commands
                if getattr(command.request, "target", None) == record.request_id
            ),
        }
    )
    records = state.records.model_copy(update={"records": (held, *commands)})
    return stopped(state.model_copy(update={"records": records})), held


def _sent(
    kind: str, target: core.RequestRecord, phase: core.RequestPhase, epoch: int
) -> core.RequestRecord:
    request_id = core.RequestId(root=f"{kind}-of-{target.request_id.root}")
    request = _command(kind, target, request_id)
    return target.model_copy(
        update={
            "request_id": request_id,
            "request": request,
            "payload_digest": request_id.root,
            "phase": phase,
            "observation": None,
            "lifecycle": core.LifecycleClass.IDEMPOTENT_WRITE,
            "until": core.Until(at=10**6),
            "addressed_by": (),
            "dispatched_in": epoch,
        }
    )


def _held_wait_live(state: core.CoreState, held: core.RequestRecord) -> bool:
    [wait] = [
        wait
        for wait in core.waits(state)
        if wait.waiter == core.WaitKind.BLOCKED_RECORD and held.request_id.root in wait.subject
    ]
    return wait.producer is not None


@given(
    kind=st.sampled_from(["inspect", "cancel", "block"]),
    phase=st.sampled_from([core.RequestPhase.DISPATCHED, core.RequestPhase.RECONCILING]),
    epoch=st.integers(min_value=0, max_value=1),
)
def test_a_command_a_dead_host_dispatched_is_not_live_by_itself(
    kind: str, phase: core.RequestPhase, epoch: int
) -> None:
    """REVIEW-8 P2-2: a dead epoch's command no longer holds a closing run's wait silently.

    Sent in this epoch, it is live. Sent by a dead host and not inspected since, no call of
    this epoch is behind it: the wait on it has no producer, so the step ends the blocked
    record or reports the orphan instead of waiting for good.
    """
    state, held = _closing_with()
    command = _sent(kind, held, phase, epoch)
    state, held = _closing_with(command)
    assert _held_wait_live(state, held) == (epoch == 1)


@given(kind=st.sampled_from(["cancel", "block"]), inspected_in=st.integers(0, 1))
def test_an_inspection_of_a_command_carries_the_wait_on_the_command(
    kind: str, inspected_in: int
) -> None:
    """REVIEW-8 part 2 item 4: the wait follows a command to the inspection sent about it."""
    state, held = _closing_with()
    command = _sent(kind, held, core.RequestPhase.DISPATCHED, 0)
    inspection = _sent("inspect", command, core.RequestPhase.DISPATCHED, inspected_in)
    command = command.model_copy(update={"addressed_by": (inspection.request_id,)})
    state, held = _closing_with(command, inspection)
    assert _held_wait_live(state, held) == (inspected_in == 1)
