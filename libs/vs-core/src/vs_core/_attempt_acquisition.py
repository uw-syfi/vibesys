"""Attempt acquisition lifecycle implementation owned by its wave-1 slice."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._evaluation_history import produce_history
from ._registry import ContractError
from .types.attempts import AttemptEvaluationHistoryUpdated
from .types.common import Area, KernelNotImplementedError, Scope
from .types.kernel import AreaChange

if TYPE_CHECKING:
    from .types.attempts import AttemptsEvent, AttemptsState
    from .types.kernel import AttemptsContext


def advance(
    state: AttemptsState, context: AttemptsContext, event: AttemptsEvent
) -> AreaChange[AttemptsState]:
    """Consume only wrapper-routed events, preserving sibling-owned state fields.

    Behavior remains explicitly unavailable until this lifecycle slice moves.
    """
    if isinstance(event, AttemptEvaluationHistoryUpdated):
        scope = Scope(owner=event.attempt.attempt_id, generation=event.attempt.generation)
        owner = next(
            (
                row
                for row in state.attempts
                if row.attempt_id == scope.owner and row.generation == scope.generation
            ),
            None,
        )
        if owner is None or event.history != produce_history(
            scope, context.evaluation, context.intents, owner, context.run
        ):
            raise ContractError(("history",), "requires canonical measurement coverage")
        if any(record not in event.history.records for record in owner.evaluation_history.records):
            raise ContractError(("history",), "cannot replace durable terminal measurement facts")
        updated = owner.model_copy(update={"evaluation_history": event.history})
        return AreaChange(
            state=state.model_copy(
                update={
                    "attempts": tuple(updated if row == owner else row for row in state.attempts)
                }
            )
        )
    del state, context
    raise KernelNotImplementedError(Area.ATTEMPTS, event.kind, subarea="_attempt_acquisition")
