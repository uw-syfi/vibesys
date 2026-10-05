"""Scope and invocation lookups shared by the Sessions leaves.

One definition of "the owning attempt", "the scope is live" and "the proven
invocation row", so Turns, Inputs and Checkpoints cannot drift apart.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._proofs import Proven, current_admission, invocation_for
from .types.attempts import AttemptPhase
from .types.common import AttemptId, ObservationStatus, RequestId, RunStatus, Scope
from .types.sessions import Access

if TYPE_CHECKING:
    from .types.attempts import AttemptView
    from .types.common import InvocationRef
    from .types.kernel import SessionsContext
    from .types.sessions import Invocation, SessionsState


def encode_identity(namespace: str, *components: str) -> str:
    """Encode supplied identity components injectively, including punctuation."""
    return namespace + ":" + ":".join(f"{len(component)}:{component}" for component in components)


def turn_request_id(ref: InvocationRef, action: str) -> RequestId:
    """The request identity one invocation derives for an *action* on itself."""
    parts = (ref.session_id.root, str(ref.generation), ref.invocation_id.root, action)
    return RequestId(root=encode_identity("invocation", *parts))


def write_turn_authority(ref: InvocationRef) -> RequestId:
    """Authority and request identity of the checkpoint a terminal write turn earns.

    Sessions requests it and Attempts verifies it, both from this one definition.
    """
    return turn_request_id(ref, "retain-write")


def retains_write_turn(invocation: Invocation) -> bool:
    """Whether the invocation is a conclusive write turn whose workspace edits must be kept.

    Core cannot see the tree change; the executor's snapshot of an unchanged tree is
    the existing revision, so requesting it for every such turn is safe.
    """
    observation = invocation.observation
    return (
        invocation.turn.session.access == Access.WRITE_CANDIDATE
        and observation is not None
        and observation.terminal
        and observation.accepted
        and observation.status == ObservationStatus.SUCCEEDED
    )


def attempt_for(context: SessionsContext, scope: Scope) -> AttemptView | None:
    """The attempt row for an attempt scope, matched on id and generation."""
    return next(
        (
            row
            for row in context.attempts.attempts
            if row.attempt_id == scope.owner and row.generation == scope.generation
        ),
        None,
    )


def scope_active(context: SessionsContext, scope: Scope) -> bool:
    """True while the scope may start new work: live admission, or a running run."""
    if isinstance(scope.owner, AttemptId):
        owner = attempt_for(context, scope)
        return (
            owner is not None
            and owner.phase == AttemptPhase.ACTIVE
            and owner.closure is None
            and isinstance(current_admission(owner, scope, owner.admission_id), Proven)
        )
    return (
        scope == Scope(owner=context.run.run_id, generation=context.run.generation)
        and context.run.status == RunStatus.RUNNING
    )


def proven_invocation(state: SessionsState, ref: InvocationRef) -> Invocation | None:
    """The invocation row only when its own identity proof holds."""
    row = next((row for row in state.invocations if row.invocation == ref), None)
    proof = invocation_for(state.invocations, ref, row.scope) if row is not None else None
    return proof.value if isinstance(proof, Proven) else None
