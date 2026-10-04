"""Declarative kernel routing traces, without implementing owning area policy."""

from vs_core._registry import ContractError
from vs_core._step import _operation_prepared, operation_owner
from vs_core.types.attempts import RevisionOperationRequested
from vs_core.types.common import Area, ExecuteRegisteredOperation
from vs_core.types.evaluation import RegisteredJobRequested
from vs_core.types.intents import RequestPrepared
from vs_core.types.kernel import CoreState
from vs_core.types.sessions import RegisteredTurnRequested
from vs_core.types.strategy import Operation

from .traces import (
    AttemptsChange,
    EvaluationChange,
    IntentsChange,
    ReducerTrace,
    SessionsChange,
    TraceFrame,
)


def operation_trace(state: CoreState, decision: Operation) -> ReducerTrace:
    """Describe passthrough leaf outputs to exercise shared operation routing."""
    prepared = _operation_prepared(state, decision)
    if not isinstance(prepared, RequestPrepared):
        raise ContractError(("normalized_scope_reopen",), "reopening requires owning guard traces")
    request = prepared.request
    if not isinstance(request, ExecuteRegisteredOperation):
        raise ContractError(("request",), "expected registered operation request")
    descriptor = next(item for item in state.registry if item.kind == decision.request.kind)
    owner = operation_owner(state, request)
    if owner == Area.INTENTS:
        return ReducerTrace(
            frames=(
                TraceFrame(
                    signal=prepared, change=IntentsChange(state=state.intents, requests=(request,))
                ),
            )
        )
    if owner == Area.SESSIONS:
        if prepared.normalized_turn is None:
            raise ContractError(("normalized_turn",), "registered turn missing normalization")
        signal = RegisteredTurnRequested(request=request, turn=prepared.normalized_turn)
        change = SessionsChange(state=state.sessions, requests=(request,))
    elif owner == Area.EVALUATION:
        if descriptor.resource_pool is None:
            raise ContractError(("resource_pool",), "owned job missing declared pool")
        signal = RegisteredJobRequested(request=request, resource_pool=descriptor.resource_pool)
        change = EvaluationChange(state=state.evaluation, requests=(request,))
    else:
        signal = RevisionOperationRequested(
            request=request, authority=descriptor.revision_authority
        )
        change = AttemptsChange(state=state.attempts, requests=(request,))
    return ReducerTrace(
        frames=(
            TraceFrame(
                signal=prepared, change=IntentsChange(state=state.intents, signals=(signal,))
            ),
            TraceFrame(signal=signal, change=change),
        )
    )
