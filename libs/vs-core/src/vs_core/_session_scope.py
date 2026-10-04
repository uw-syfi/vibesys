"""Scope and invocation lookups shared by the Sessions leaves.

One definition of "the owning attempt", "the scope is live" and "the proven
invocation row", so Turns, Inputs and Checkpoints cannot drift apart.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._proofs import Proven, current_admission, invocation_for
from .types.attempts import AttemptPhase
from .types.common import AttemptId, RunStatus, Scope

if TYPE_CHECKING:
    from .types.attempts import AttemptView
    from .types.common import InvocationRef
    from .types.kernel import SessionsContext
    from .types.sessions import Invocation, SessionsState


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
