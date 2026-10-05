"""Read-only strategy projection without duplicated lifecycle authority."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .types.common import ChargeKind
from .types.intents import (
    ExecuteRegisteredOperation,
    IntentPhase,
    IntentsState,
    OperationView,
    Request,
)
from .types.kernel import CoreState, RunSummary, RunView
from .types.scheduling import SchedulingState, SchedulingView
from .types.sessions import SessionProjection

if TYPE_CHECKING:
    from .types.attempts import AttemptsState, AttemptView
    from .types.common import Limits
    from .types.evaluation import EvaluationState, EvidenceRef
    from .types.sessions import SessionsState
    from .types.settlement import Settlement, SettlementState


def scheduling_view(
    state: SchedulingState, limits: Limits, attempts: AttemptsState, now_at: float
) -> SchedulingView:
    """Derive admission capacity from authoritative slots and paid bounds."""
    receipts = tuple(
        receipt
        for attempt in attempts.attempts
        for receipt in attempt.charges
        if receipt.kind == ChargeKind.ADMISSION
    )
    charged = sum(receipt.charged for receipt in receipts)
    refunded = sum(receipt.refunded for receipt in receipts)
    remaining = max(0, limits.max_attempts - charged + refunded)
    available = min(max(0, limits.max_parallel - len(state.slots)), remaining)
    return SchedulingView(
        queue=state.queue,
        slots=state.slots,
        available_tokens=0 if state.admission_closed else available,
        charged=charged,
        refunded=refunded,
        slot_seconds=state.released_slot_seconds
        + sum(
            max(0.0, slot.charge_ended_at - slot.admitted_at)
            for slot in state.slots
            if slot.charge_ended_at is not None
        ),
        active_slot_seconds=sum(
            max(0.0, now_at - slot.admitted_at)
            for slot in state.slots
            if slot.charge_ended_at is None
        ),
        admission_closed=state.admission_closed,
    )


def attempt_view(state: AttemptsState) -> tuple[AttemptView, ...]:
    """Project immutable attempt facts."""
    return state.attempts


def session_view(state: SessionsState) -> tuple[SessionProjection, ...]:
    """Project reserved artifacts from the sole input-occurrence ledger."""
    inputs = sorted(
        state.inputs, key=lambda record: (record.input.sequence, record.input.input_id.root)
    )
    return tuple(
        SessionProjection(
            **session.model_dump(),
            reserved_inputs=tuple(
                record.input.artifact
                for record in inputs
                if record.receipt is None
                and record.reserved_to is not None
                and record.reserved_to.session_id == session.spec.session_id
                and record.reserved_to.generation == session.generation
            ),
        )
        for session in state.sessions
    )


def evidence_view(state: EvaluationState) -> tuple[EvidenceRef, ...]:
    """Preserve original evidence identities and provenance."""
    return state.evidence


def next_observe_at(state: EvaluationState) -> float | None:
    """The earliest scheduled poll time, so the run loop can sleep until it."""
    due = tuple(
        job.pacing.next_at
        for job in (*state.jobs, *state.registered_jobs)
        if job.pacing.next_at is not None
    )
    return min(due) if due else None


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
        scheduling=scheduling_view(state.scheduling, run.limits, state.attempts, run.now_at),
        attempts=attempt_view(state.attempts),
        sessions=session_view(state.sessions),
        inputs=state.sessions.inputs,
        operations=operations,
        measurements=evidence_view(state.evaluation),
        settlements=settlement_view(state.settlement),
        artifacts=run.artifacts,
        controls=run.controls,
        next_observe_at=next_observe_at(state.evaluation),
    )
