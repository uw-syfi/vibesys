"""A query core ended at its limit does not hold a restarted run in recovery.

A live run was resumed after its host died while a poll of an evaluation job ran. Core had
ended that poll (a query) at its limit long before, its job was released, and nothing waited on
it. Recovery still inspected it, the executor found it begun with no result (``unknown``), and
that kept recovery blocked: the run loop ended the resumed run blocked, with the search still
waiting for its first answer. A query owns nothing and starts nothing, so a blocked one has no
answer that could matter.
"""

from __future__ import annotations

from vs_core.api import Failed as FailedUntil
from vs_core.api import (
    FailureReason,
    LifecycleClass,
    ObserveOwnedJob,
    RecoveryPhase,
    RecoveryStarted,
    RequestId,
    RequestPhase,
    RequestRecord,
    ResourceId,
    Scope,
    Until,
    step,
)

from .test_request_recovery import fixture_digest, pending_record, recovering_state


def _blocked_poll() -> RequestRecord:
    base = pending_record()
    request = ObserveOwnedJob(
        request_id=RequestId(root="poll"),
        scope=Scope(owner=base.request.scope.owner, generation=0),
        resource_id=ResourceId(root="job"),
    )
    return base.model_copy(
        update={
            "request_id": request.request_id,
            "request": request,
            "payload_digest": fixture_digest(request),
            "lifecycle": LifecycleClass.QUERY,
            "phase": RequestPhase.BLOCKED,
            "until": Until(at=10.0),
            "failure": FailedUntil(reason=FailureReason.TIMED_OUT, limit_at=10.0),
        }
    )


def test_recovery_opens_over_a_query_core_ended_at_its_limit() -> None:
    """Regression: the poll was inspected, answered unknown, and recovery blocked for good."""
    started = step(recovering_state(_blocked_poll()), RecoveryStarted(epoch=2, now_at=1000.0))
    barrier = started.state.records.recovery
    assert [check.resolution for check in barrier.checks] == ["terminal"]
    assert barrier.phase == RecoveryPhase.READY
    assert started.requests == ()  # nothing is asked of the executor about it


def test_an_open_write_core_cannot_account_for_still_gates_recovery() -> None:
    started = step(recovering_state(pending_record()), RecoveryStarted(epoch=2, now_at=1000.0))
    assert started.state.records.recovery.phase == RecoveryPhase.RECOVERING
    assert started.state.records.recovery.checks[0].resolution == "pending"
