"""Read-only strategy projection without duplicated lifecycle authority."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._adoption import view as adoption_view
from ._operator_actions import parallel_limit
from ._request_ledger import next_retry_at
from ._request_recovery import next_reconciliation_at
from .types.common import ChargeKind
from .types.kernel import CoreState, CoreView, RunSummary
from .types.records import (
    ExecuteRegisteredOperation,
    OperationView,
    RecordsState,
    Request,
    RequestPhase,
)
from .types.scheduling import SchedulingState, SchedulingView
from .types.sessions import SessionProjection

if TYPE_CHECKING:
    from .types.attempts import AttemptsState, AttemptView
    from .types.evaluation import EvaluationState, EvidenceRef
    from .types.kernel import RunState
    from .types.sessions import SessionsState
    from .types.settlement import Settlement, SettlementState


def scheduling_view(
    state: SchedulingState, run: RunState, attempts: AttemptsState
) -> SchedulingView:
    """Derive admission capacity from authoritative slots, paid bounds and the parallel limit."""
    limits, now_at = run.limits, run.now_at
    receipts = tuple(
        receipt
        for attempt in attempts.attempts
        for receipt in attempt.charges
        if receipt.kind == ChargeKind.ADMISSION
    )
    charged = sum(receipt.charged for receipt in receipts)
    refunded = sum(receipt.refunded for receipt in receipts)
    remaining = max(0, limits.max_attempts - charged + refunded)
    available = min(max(0, parallel_limit(run) - len(state.slots)), remaining)
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
    """The earliest timer a job waits on, so the run loop can sleep until it."""
    due = tuple(
        job.wait.timer
        for job in (*state.jobs, *state.registered_jobs)
        if job.wait is not None and job.wait.timer is not None
    )
    return min(due) if due else None


def _earliest(*times: float | None) -> float | None:
    due = [time for time in times if time is not None]
    return min(due) if due else None


def settlement_view(state: SettlementState) -> tuple[Settlement, ...]:
    """Project final settlements, excluding pending closure."""
    return state.settlements


def pending_requests(state: RecordsState) -> tuple[Request, ...]:
    """The durable outbox; this does not authorize external dispatch."""
    return tuple(
        record.request for record in state.records if record.phase != RequestPhase.COMPLETED
    )


def project(state: CoreState) -> CoreView:
    """Expose only immutable lifecycle facts, never strategy population state."""
    run = state.run
    operations = tuple(
        OperationView(
            operation_id=record.request.operation_id,
            scope=record.request.scope,
            schema_ref=record.request.operation.schema_ref,
            phase=record.phase,
            outcome_schema=record.outcome_schema,
            outcome_json=record.outcome_json,
            outcome=record.outcome,
        )
        for record in state.records.records
        if isinstance(record.request, ExecuteRegisteredOperation)
    )
    return CoreView(
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
        scheduling=scheduling_view(state.scheduling, run, state.attempts),
        attempts=attempt_view(state.attempts),
        sessions=session_view(state.sessions),
        inputs=state.sessions.inputs,
        operations=operations,
        measurements=evidence_view(state.evaluation),
        settlements=settlement_view(state.settlement),
        artifacts=run.artifacts,
        controls=run.controls,
        adoption=adoption_view(state.settlement, state.records),
        next_observe_at=_earliest(
            next_observe_at(state.evaluation),
            next_reconciliation_at(state.records, state.evaluation, run),
            next_retry_at(state.records),
        ),
    )
