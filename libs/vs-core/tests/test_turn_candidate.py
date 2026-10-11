"""A strategy resolves only the current invocation's durable checkpoint outcome."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from vs_core.api import (
    AttemptBudget,
    AttemptCheckpoint,
    AttemptId,
    AttemptPhase,
    AttemptView,
    CheckpointDecline,
    CheckpointDeclined,
    InvocationId,
    InvocationRef,
    ItemId,
    RequestId,
    RevisionId,
    RevisionRef,
    RunView,
    SessionId,
    TurnCandidateFailed,
    TurnCandidatePending,
    TurnCandidateRetained,
    TurnChain,
    Value,
    WorkspaceMode,
    WorkspacePlan,
    initial_state,
    project,
    turn_candidate,
)

ATTEMPT = AttemptId(root="attempt")


def _invocation(name: str) -> InvocationRef:
    return InvocationRef(
        session_id=SessionId(root="session"), invocation_id=InvocationId(root=name), generation=0
    )


def _revision(name: str) -> RevisionRef:
    return RevisionRef(revision_id=RevisionId(root=name), digest=f"digest-{name}")


def _checkpoint(invocation: InvocationRef, name: str) -> AttemptCheckpoint:
    return AttemptCheckpoint(
        invocation=invocation,
        request_id=RequestId(root=f"retain-{name}"),
        revision=_revision(name),
        retention="candidate",
    )


def _decline(invocation: InvocationRef, reason: CheckpointDecline) -> CheckpointDeclined:
    return CheckpointDeclined(
        invocation=invocation,
        authority=RequestId(root=f"decline-{invocation.invocation_id.root}-{reason.value}"),
        reason=reason,
    )


def _view(
    *,
    checkpoints: tuple[AttemptCheckpoint, ...] = (),
    declines: tuple[CheckpointDeclined, ...] = (),
) -> RunView:
    attempt = AttemptView(
        attempt_id=ATTEMPT,
        item_id=ItemId(root="item"),
        generation=0,
        phase=AttemptPhase.ACTIVE,
        workspace=WorkspacePlan(mode=WorkspaceMode.EXCLUSIVE_ROOT, base=_revision("base")),
        budget=AttemptBudget(),
        checkpoints=checkpoints,
        checkpoint_declines=declines,
    )
    return project(initial_state()).model_copy(update={"attempts": (attempt,)})


@given(order=st.permutations(("oldest", "newest")))
def test_older_outcomes_never_resolve_a_pending_current_invocation(
    order: list[str],
) -> None:
    current = _invocation("current")
    invocations = {name: _invocation(name) for name in order}
    checkpoints = tuple(_checkpoint(invocations[name], name) for name in order)
    declines = tuple(
        _decline(invocations[name], CheckpointDecline.SNAPSHOT_FAILED) for name in reversed(order)
    )

    candidate = TurnChain[Value]().candidate_outcome(
        _view(checkpoints=checkpoints, declines=declines), ATTEMPT, current
    )

    assert isinstance(candidate, TurnCandidatePending)


@given(
    reason=st.sampled_from(tuple(CheckpointDecline)),
    order=st.permutations(("oldest", "newest")),
)
def test_every_current_decline_is_a_typed_failure_independent_of_older_event_order(
    reason: CheckpointDecline,
    order: list[str],
) -> None:
    current = _invocation("current")
    invocations = {name: _invocation(name) for name in order}
    checkpoints = tuple(_checkpoint(invocations[name], name) for name in order)
    declines = (
        *tuple(_decline(invocations[name], CheckpointDecline.SNAPSHOT_FAILED) for name in order),
        _decline(current, reason),
    )

    candidate = TurnChain[Value]().candidate_outcome(
        _view(checkpoints=checkpoints, declines=declines), ATTEMPT, current
    )

    assert isinstance(candidate, TurnCandidateFailed)
    assert candidate.reason is reason


@given(
    checkpoint_order=st.permutations(("older", "current")),
    decline_order=st.permutations(("older", "current")),
)
def test_a_current_checkpoint_wins_over_declines_and_older_records_in_any_order(
    checkpoint_order: list[str],
    decline_order: list[str],
) -> None:
    current = _invocation("current")
    older = _invocation("older")
    invocations = {"current": current, "older": older}
    checkpoints = tuple(
        _checkpoint(invocations[name], f"{name}-revision") for name in checkpoint_order
    )
    declines = tuple(
        _decline(invocations[name], CheckpointDecline.SNAPSHOT_FAILED) for name in decline_order
    )

    candidate = TurnChain[Value]().candidate_outcome(
        _view(checkpoints=checkpoints, declines=declines), ATTEMPT, current
    )

    assert isinstance(candidate, TurnCandidateRetained)
    assert candidate.revision == _revision("current-revision")


def test_an_unchanged_current_snapshot_is_still_a_retained_current_checkpoint() -> None:
    older = _invocation("older")
    current = _invocation("current")
    unchanged = _revision("same")
    checkpoints = (
        _checkpoint(older, "same"),
        _checkpoint(current, "same"),
    )

    candidate = TurnChain[Value]().candidate_outcome(
        _view(checkpoints=checkpoints), ATTEMPT, current
    )

    assert isinstance(candidate, TurnCandidateRetained)
    assert candidate.revision == unchanged


def test_a_changed_current_snapshot_uses_its_new_revision() -> None:
    older = _invocation("older")
    current = _invocation("current")
    candidate = TurnChain[Value]().candidate_outcome(
        _view(checkpoints=(_checkpoint(older, "old"), _checkpoint(current, "new"))),
        ATTEMPT,
        current,
    )

    assert isinstance(candidate, TurnCandidateRetained)
    assert candidate.revision == _revision("new")


def test_published_candidate_apis_keep_v1_shape_without_predecessor_fallback() -> None:
    older = _invocation("older")
    current = _invocation("current")
    view = _view(checkpoints=(_checkpoint(older, "old"),))
    chain = TurnChain[Value](lineage=(older,))

    assert turn_candidate(view, ATTEMPT, current, predecessors=(older,)) is None
    assert chain.candidate(view, ATTEMPT, current) is None


def test_persisted_v1_turn_chain_accepts_inert_lineage() -> None:
    older = _invocation("older")
    restored = TurnChain[Value].model_validate_json(
        TurnChain[Value](lineage=(older,)).model_dump_json()
    )

    assert restored.lineage == (older,)
