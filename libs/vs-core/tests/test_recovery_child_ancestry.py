"""Descendant ancestry and cleanup guards through the public kernel step."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core

from .restart import unaddressed
from .test_request_recovery import (
    observed,
    pending_record,
    recovering_state,
    reload,
    with_sibling_history,
)


@given(st.text(alphabet="abc123", min_size=1, max_size=20))
def test_recovery_rejects_a_resource_discovering_itself(identity: str) -> None:
    original = pending_record()
    resource = core.ResourceId(root=identity)
    original = original.model_copy(
        update={"observation": observed(original, resource_id=resource, children=(resource,))}
    )
    state = recovering_state(original)
    before = state.model_dump_json()
    with pytest.raises(core.ContractError, match="resource cannot be its own descendant"):
        core.step(reload(state), core.RecoveryStarted(epoch=1, now_at=11.0))
    assert state.model_dump_json() == before


@given(st.integers(min_value=2, max_value=8))
def test_recovery_rejects_discovery_of_a_transitive_ancestor(depth: int) -> None:
    original = pending_record()
    resources = tuple(core.ResourceId(root=f"resource-{index}") for index in range(depth))
    original = original.model_copy(
        update={
            "observation": observed(original, resource_id=resources[0], children=(resources[-1],))
        }
    )
    children = tuple(
        core.ChildLease(
            resource_id=resource,
            scope=original.request.scope,
            source_requests=(original.request_id,),
            parent_resources=(resources[index + 1],),
        )
        for index, resource in enumerate(resources[:-1])
    )
    state = recovering_state(original)
    state = state.model_copy(
        update={"records": state.records.model_copy(update={"children": children})}
    )
    before = state.model_dump_json()
    with pytest.raises(core.ContractError, match="cyclic child ownership ancestry"):
        core.step(reload(state), core.RecoveryStarted(epoch=1, now_at=11.0))
    assert state.model_dump_json() == before


@given(st.integers(min_value=1, max_value=10))
def test_recovery_rejects_shared_child_discovery_in_another_scope(generation: int) -> None:
    original = pending_record(identity="original")
    foreign = pending_record(identity="foreign")
    foreign = foreign.model_copy(
        update={
            "request": foreign.request.model_copy(
                update={
                    "scope": foreign.request.scope.model_copy(update={"generation": generation})
                }
            )
        }
    )
    resource = core.ResourceId(root="shared-child")
    original = original.model_copy(update={"observation": observed(original, children=(resource,))})
    child = core.ChildLease(
        resource_id=resource,
        scope=foreign.request.scope,
        source_requests=(foreign.request_id,),
    )
    state = recovering_state(original, foreign)
    state = state.model_copy(
        update={"records": state.records.model_copy(update={"children": (child,)})}
    )
    before = state.model_dump_json()
    with pytest.raises(core.ContractError, match="conflicting child ownership scope"):
        core.step(reload(state), core.RecoveryStarted(epoch=1, now_at=11.0))
    assert state.model_dump_json() == before


@given(st.integers(min_value=1, max_value=5))
def test_prepared_source_with_descendants_cannot_be_safe_to_dispatch(count: int) -> None:
    original = pending_record(phase=core.RequestPhase.PREPARED)
    children = tuple(
        core.ChildLease(
            resource_id=core.ResourceId(root=f"child-{index}"),
            scope=original.request.scope,
            source_requests=(original.request_id,),
        )
        for index in range(count)
    )
    state = recovering_state(original)
    state = state.model_copy(
        update={"records": state.records.model_copy(update={"children": children})}
    )
    event = core.RecoveryStarted(epoch=1, now_at=11.0)
    result = core.step(reload(state), event)
    assert result.state.records.children == children
    assert unaddressed(result.state.records.records[0]) == original
    assert result.state.records.recovery.phase == core.RecoveryPhase.RECOVERING
    assert result.state.records.recovery.checks[0].resolution == "pending"
    assert len(result.requests) == count + 1
    assert all(isinstance(request, core.InspectRequest) for request in result.requests)
    assert {
        request.resource_id
        for request in result.requests
        if isinstance(request, core.InspectRequest)
    } == {
        None,
        *(child.resource_id for child in children),
    }
    replay = core.step(reload(result.state), event)
    assert replay.requests == ()
    assert replay.state.records == result.state.records


@given(st.text(alphabet="abc123", min_size=1, max_size=20))
def test_unrelated_invocation_does_not_prove_a_session_lease(identity: str) -> None:
    original = pending_record(identity=identity)
    original = original.model_copy(
        update={
            "observation": observed(
                original,
                accepted=True,
                status=core.ObservationStatus.PENDING,
                resource_id=core.ResourceId(root="unowned-conversation"),
                children_complete=True,
            )
        }
    )
    state = with_sibling_history(recovering_state(original), (2, 1))
    result = core.step(reload(state), core.RecoveryStarted(epoch=1, now_at=11.0))
    assert result.state.records.recovery.checks[0].resolution == "pending"
    assert len(result.requests) == 1
    assert isinstance(result.requests[0], core.InspectRequest)
    assert result.state.sessions == state.sessions
    assert result.state.attempts == state.attempts


@given(pending=st.booleans())
def test_workspace_reattachment_requires_the_owners_pending_request(*, pending: bool) -> None:
    original = pending_record()
    state = with_sibling_history(recovering_state(original), (2, 1))
    owner = state.attempts.attempts[0]
    owner = owner.model_copy(update={"pending_records": (original.request_id,) if pending else ()})
    request = core.EnsureWorkspace(
        request_id=original.request_id,
        scope=core.Scope(owner=owner.attempt_id, generation=owner.generation),
        admission_id=owner.admission_id,
        attempt=core.AttemptRef(attempt_id=owner.attempt_id, generation=owner.generation),
        plan=owner.workspace,
    )
    original = original.model_copy(update={"request": request})
    original = original.model_copy(
        update={
            "observation": observed(
                original,
                accepted=True,
                status=core.ObservationStatus.PENDING,
                resource_id=core.ResourceId(root="workspace"),
                children_complete=True,
                admission_id=owner.admission_id,
            )
        }
    )
    state = state.model_copy(
        update={
            "attempts": core.AttemptsState(attempts=(owner,)),
            "records": state.records.model_copy(
                update={"records": (original, pending_record(identity="anchor"))}
            ),
        }
    )
    result = core.step(reload(state), core.RecoveryStarted(epoch=1, now_at=11.0))
    assert result.state.records.recovery.checks[0].resolution == (
        "reattached" if pending else "pending"
    )
    assert len(result.requests) == (1 if pending else 2)
    assert result.state.attempts == state.attempts
    assert result.state.sessions == state.sessions
    assert unaddressed(result.state.records.records[0]) == original
