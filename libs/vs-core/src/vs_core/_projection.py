"""Read-only strategy projection without duplicated lifecycle authority."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .types.intents import (
    ExecuteRegisteredOperation,
    IntentPhase,
    IntentsState,
    OperationView,
    Request,
)
from .types.kernel import CoreState, RunSummary, RunView
from .types.scheduling import SchedulingState, SchedulingView

if TYPE_CHECKING:
    from .types.attempts import AttemptsState, AttemptView
    from .types.common import Limits
    from .types.evaluation import EvaluationState, EvidenceRef
    from .types.sessions import SessionsState, SessionView
    from .types.settlement import Settlement, SettlementState


def scheduling_view(state: SchedulingState, limits: Limits) -> SchedulingView:
    """Derive admission capacity from authoritative slots and paid bounds."""
    remaining = max(0, limits.max_attempts - state.charged + state.refunded)
    available = min(max(0, limits.max_parallel - len(state.slots)), remaining)
    return SchedulingView(
        queue=state.queue,
        slots=state.slots,
        available_tokens=0 if state.admission_closed else available,
        charged=state.charged,
        refunded=state.refunded,
        admission_closed=state.admission_closed,
    )


def attempt_view(state: AttemptsState) -> tuple[AttemptView, ...]:
    """Project immutable attempt facts."""
    return state.attempts


def session_view(state: SessionsState) -> tuple[SessionView, ...]:
    """Project immutable session facts."""
    return state.sessions


def evidence_view(state: EvaluationState) -> tuple[EvidenceRef, ...]:
    """Preserve original evidence identities and provenance."""
    return state.evidence


def settlement_view(state: SettlementState) -> tuple[Settlement, ...]:
    """Project final settlements, excluding pending closure."""
    return state.settlements


def pending_requests(state: IntentsState) -> tuple[Request, ...]:
    """The durable outbox; this does not authorize external dispatch."""
    return tuple(
        intent.request for intent in state.intents if intent.phase != IntentPhase.COMPLETED
    )


def project(state: CoreState) -> RunView:
    """Expose only immutable lifecycle facts, never strategy population state."""
    run = state.run
    operations = tuple(
        OperationView(
            operation_id=intent.request.operation_id,
            scope=intent.request.scope,
            schema_ref=intent.request.operation.schema_ref,
            phase=intent.phase,
            outcome_schema=intent.outcome_schema,
            outcome_json=intent.outcome_json,
            outcome=intent.outcome,
        )
        for intent in state.intents.intents
        if isinstance(intent.request, ExecuteRegisteredOperation)
    )
    return RunView(
        revision=state.revision,
        run=RunSummary(
            run_id=run.run_id,
            generation=run.generation,
            status=run.status,
            now_at=run.now_at,
            deadline_at=run.deadline_at,
            result=run.result,
        ),
        facts=run.facts,
        capabilities=run.capabilities,
        limits=run.limits,
        scheduling=scheduling_view(state.scheduling, run.limits),
        attempts=attempt_view(state.attempts),
        sessions=session_view(state.sessions),
        operations=operations,
        measurements=evidence_view(state.evaluation),
        settlements=settlement_view(state.settlement),
        artifacts=run.artifacts,
        controls=run.controls,
    )
