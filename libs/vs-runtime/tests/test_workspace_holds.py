"""A run's candidate workspaces outlive a host only while core still references them.

Core references an attempt generation's workspace while the attempt has not ended or a
request scoped to it is still open, and none once the run's result is final. A host's start
binds its workspaces to that reading of core's committed state.
"""

from __future__ import annotations

import hashlib

from hypothesis import given
from hypothesis import strategies as st

from vs_core.api import (
    AttemptBudget,
    AttemptId,
    AttemptPhase,
    AttemptRef,
    AttemptsState,
    AttemptView,
    CoreState,
    DecisionId,
    DiscardWorkspace,
    EventId,
    ItemId,
    LifecycleClass,
    Observation,
    ObservationStatus,
    RecordsState,
    RequestId,
    RequestPhase,
    RequestRecord,
    RunStatus,
    Scope,
    Until,
    WorkspaceMode,
    WorkspacePlan,
    initial_state,
)
from vs_runtime.api.core import (
    revision_ref,
)
from vs_runtime.api.infrastructure import WorkspaceHolds, core_workspace_holds
from vs_runtime.api.testing import counter

_ATTEMPTS = ("a0", "a1", "a2")


def _attempt(name: str, generation: int) -> AttemptRef:
    return AttemptRef(attempt_id=AttemptId(root=name), generation=generation)


def _view(attempt: AttemptRef, phase: AttemptPhase) -> AttemptView:
    return AttemptView(
        attempt_id=attempt.attempt_id,
        item_id=ItemId(root=attempt.attempt_id.root),
        generation=attempt.generation,
        phase=phase,
        workspace=WorkspacePlan(mode=WorkspaceMode.ISOLATED_CHILD, base=revision_ref("0" * 40)),
        budget=AttemptBudget(),
    )


def _discard(index: int, attempt: AttemptRef, phase: RequestPhase) -> RequestRecord:
    request_id = RequestId(root=f"discard-{index}")
    request = DiscardWorkspace(
        request_id=request_id,
        scope=Scope(owner=attempt.attempt_id, generation=attempt.generation),
        admission_id=DecisionId(root="admit"),
        attempt=attempt,
    )
    return RequestRecord(
        request_id=request_id,
        request=request,
        payload_digest=hashlib.sha256(request.model_dump_json().encode()).hexdigest(),
        lifecycle=LifecycleClass.IDEMPOTENT_WRITE,
        phase=phase,
        until=Until(at=100.0),
        addressed_by=(),
    )


def _state(
    views: tuple[AttemptView, ...], records: tuple[RequestRecord, ...], status: RunStatus
) -> CoreState:
    core = initial_state()
    return core.model_copy(
        update={
            "run": core.run.model_copy(
                update={"declaration": counter.declaration, "status": status}
            ),
            "attempts": AttemptsState(attempts=views),
            "records": RecordsState(records=records),
        }
    )


_refs = st.builds(_attempt, st.sampled_from(_ATTEMPTS), st.integers(0, 2))


@given(
    views=st.dictionaries(_refs, st.sampled_from(AttemptPhase), max_size=4),
    records=st.lists(st.tuples(_refs, st.sampled_from(RequestPhase)), max_size=4),
    status=st.sampled_from(RunStatus),
)
def test_core_holds_an_attempt_workspace_until_the_attempt_and_its_requests_end(
    views: dict[AttemptRef, AttemptPhase],
    records: list[tuple[AttemptRef, RequestPhase]],
    status: RunStatus,
) -> None:
    """Held: an attempt not ended, or one an open request is scoped to; none once final."""
    core = _state(
        tuple(_view(attempt, phase) for attempt, phase in views.items()),
        tuple(_discard(index, attempt, phase) for index, (attempt, phase) in enumerate(records)),
        status,
    )
    holds = core_workspace_holds(core)
    assert holds.final == (status == RunStatus.TERMINAL)
    expected = (
        frozenset()
        if holds.final
        else frozenset(
            {attempt for attempt, phase in views.items() if phase != AttemptPhase.TERMINAL}
            | {attempt for attempt, phase in records if phase != RequestPhase.COMPLETED}
        )
    )
    assert holds.attempts == expected
    assert holds.workspace_ids() == WorkspaceHolds(final=False, attempts=expected).workspace_ids()
    assert len(holds.workspace_ids()) == len(expected)


def test_a_blocked_record_that_named_nothing_holds_no_workspace() -> None:
    """It owns nothing to release, so nothing of core's still references the attempt's workspace."""
    attempt = _attempt("a0", 0)
    record = _discard(0, attempt, RequestPhase.BLOCKED)
    unnamed = record.model_copy(
        update={
            "observation": Observation(
                event_id=EventId(root="e"),
                request_id=record.request_id,
                scope=record.request.scope,
                sequence=0,
                observed_at=1.0,
                status=ObservationStatus.UNKNOWN,
                terminal=False,
            )
        }
    )
    core = _state((), (unnamed,), RunStatus.RUNNING)
    assert core_workspace_holds(core).attempts == frozenset()
