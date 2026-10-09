"""Pure assessment, retention and cleanup-gated settlement lifecycle."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from .types.attempts import AttemptPhase, RetentionRequired
from .types.common import (
    AssessmentKind,
    CompletionStatus,
    ContractValidationError,
    DependencyRef,
    EvidenceId,
    EvidenceKey,
    ObservationStatus,
    RejectionCode,
    Scope,
    SettlementId,
    WorkspaceMode,
    WorkspaceRef,
)
from .types.kernel import AreaChange, DecisionCompleted
from .types.sessions import DispatchTurn, ResumeSessionTurn, SessionPhase
from .types.settlement import (
    AssessmentSubmitted,
    AttemptSettled,
    OwnershipSettled,
    Settlement,
    SettlementState,
)
from .types.strategy import Accepted, Rejected, Settle, Withdraw

if TYPE_CHECKING:
    from .types.attempts import AttemptView
    from .types.common import AttemptRef, DecisionId, InvocationRef, RevisionRef
    from .types.evaluation import EvidenceRef
    from .types.intents import Intent
    from .types.kernel import DecisionReceipt, SettlementContext
    from .types.sessions import Invocation
    from .types.settlement import AssessmentProposal, SettlementEvent


def _owner(context: SettlementContext, attempt: AttemptRef) -> AttemptView | None:
    return next(
        (
            owner
            for owner in context.attempts.attempts
            if owner.attempt_id == attempt.attempt_id and owner.generation == attempt.generation
        ),
        None,
    )


def _evidence_valid(
    evidence: EvidenceRef, candidate: RevisionRef, scope: Scope, context: SettlementContext
) -> bool:
    facts = context.run.facts
    if (
        evidence.scope != scope
        or evidence.candidate != candidate
        or evidence.status != ObservationStatus.SUCCEEDED
        or (evidence.evaluator_digest, evidence.workload_digest, evidence.environment_digest)
        != (facts.evaluator_digest, facts.workload_digest, facts.environment_digest)
    ):
        return False
    # Built-in plans carry the authoritative measurement identity. Generic job
    # evidence has no plan identity in the frozen contract and cannot prove it.
    return any(
        job.scope == scope
        and job.submission_id == evidence.source_request
        and job.terminal
        and job.status == ObservationStatus.SUCCEEDED
        and job.observation is not None
        and job.observation.terminal
        and job.observation.accepted
        and job.observation.status == ObservationStatus.SUCCEEDED
        and job.observation.scope == scope
        and job.observation.request_id == evidence.source_request
        and job.observation.resource_id == job.resource_id
        and job.observation.sequence >= evidence.observation_sequence
        and evidence in job.evidence
        and job.plan.candidate == candidate
        and job.plan.purpose == evidence.purpose
        and (job.plan.evaluator_digest, job.plan.workload_digest, job.plan.environment_digest)
        == (evidence.evaluator_digest, evidence.workload_digest, evidence.environment_digest)
        for job in context.evaluation.jobs
    )


def _turn_request_valid(intent: Intent, invocation: Invocation) -> bool:
    request = intent.request
    # SettlementContext has no registry descriptors. Receipt/wire agreement
    # cannot establish registered operation authority without that declaration.
    return (
        isinstance(request, DispatchTurn | ResumeSessionTurn)
        and invocation.registered_operation is None
        and request.turn == invocation.turn
    )


def _invocation_valid(
    source: InvocationRef,
    assessment: AssessmentProposal,
    owner: AttemptView,
    context: SettlementContext,
) -> bool:
    invocation = next(
        (item for item in context.sessions.invocations if item.invocation == source), None
    )
    scope = Scope(owner=owner.attempt_id, generation=owner.generation)
    if (
        invocation is None
        or invocation.scope != scope
        or source.generation != scope.generation
        or source.session_id != invocation.turn.session.session_id
        or source.invocation_id != invocation.turn.invocation_id
    ):
        return False
    observation = invocation.observation
    if (
        invocation.phase != SessionPhase.TERMINAL
        or observation is None
        or observation.scope != scope
        or not observation.accepted
        or not observation.terminal
        or observation.status != ObservationStatus.SUCCEEDED
        or invocation.output_schema != invocation.turn.output_schema
        or invocation.output_json is None
    ):
        return False
    if not any(
        intent.request_id == observation.request_id
        and intent.request.request_id == observation.request_id
        and intent.request.scope == scope
        and _turn_request_valid(intent, invocation)
        for intent in context.intents.intents
    ):
        return False
    # Sessions owns schema validation before accepting final output. Settlement
    # additionally rejects malformed persisted JSON, without owning role schemas.
    try:
        json.loads(invocation.output_json)
    except (ValueError, TypeError):
        return False
    authorized = any(
        authority.kind == assessment.kind
        and authority.role_id == invocation.turn.session.role_id
        and authority.output_schema == invocation.output_schema
        and authority.output_schema.version == assessment.schema_version
        for authority in context.run.requirements.assessment_authorities
    )
    workspace = invocation.turn.workspace
    attributed = any(
        checkpoint.invocation == source and checkpoint.revision == assessment.candidate
        for checkpoint in owner.checkpoints
    ) or (
        isinstance(workspace, WorkspaceRef)
        and workspace.scope == scope
        and workspace.mode == WorkspaceMode.READ_ONLY_REVISION
        and workspace.revision == assessment.candidate
    )
    return authorized and attributed


def _assessment_valid(
    assessment: AssessmentProposal,
    settlement: Settlement,
    owner: AttemptView,
    context: SettlementContext,
) -> bool:
    if (
        assessment.candidate != settlement.candidate
        or not assessment.sources
        or len(set(assessment.sources)) != len(assessment.sources)
    ):
        return False
    scope = Scope(owner=owner.attempt_id, generation=owner.generation)
    for source in assessment.sources:
        if isinstance(source, EvidenceId | EvidenceKey):
            matches = tuple(
                item
                for item in context.evaluation.evidence
                if (item.key if isinstance(source, EvidenceKey) else item.evidence_id) == source
            )
            evidence = matches[0] if len(matches) == 1 else None
            if (
                evidence is None
                or settlement.candidate is None
                or AssessmentKind(evidence.kind.value) != assessment.kind
                or not _evidence_valid(evidence, settlement.candidate, scope, context)
            ):
                return False
        elif not _invocation_valid(source, assessment, owner, context):
            return False
    return True


def _eligible(settlement: Settlement, owner: AttemptView, context: SettlementContext) -> bool:
    if (
        not settlement.eligible
        or settlement.outcome != "succeeded"
        or settlement.retention != "candidate"
        or settlement.candidate is None
    ):
        return False
    requirements = context.run.requirements
    if not all(
        _assessment_valid(item, settlement, owner, context) for item in settlement.assessments
    ):
        return False
    if not all(
        any(item.kind == kind for item in settlement.assessments)
        and all(item.verdict == "satisfied" for item in settlement.assessments if item.kind == kind)
        for kind in requirements.required_assessments
    ):
        return False
    scope = Scope(owner=owner.attempt_id, generation=owner.generation)
    # Requirements constrain authority; an empty universal check provides none.
    has_authority = any(item.verdict == "satisfied" for item in settlement.assessments) or any(
        _evidence_valid(evidence, settlement.candidate, scope, context)
        for evidence in context.evaluation.evidence
    )
    return has_authority and all(
        any(
            evidence.kind == required.kind
            and evidence.provenance == required.provenance
            and (required.purpose is None or evidence.purpose == required.purpose)
            and _evidence_valid(evidence, settlement.candidate, scope, context)
            for evidence in context.evaluation.evidence
        )
        for required in requirements.required_evidence
    )


def _normalize_choice(settlement: Settlement, owner: AttemptView | None) -> Settlement:
    """Preserve requested eligibility until finalization, except cancellation."""
    if settlement.outcome == "cancelled" or (
        owner is not None and owner.closure is not None and owner.closure.disposition == "cancel"
    ):
        return settlement.model_copy(
            update={"outcome": "cancelled", "eligible": False, "retention": "discard"}
        )
    return settlement


def _released(owner: AttemptView, settlement: Settlement) -> bool:
    if (
        owner.phase != AttemptPhase.TERMINAL
        or owner.closure is None
        or owner.closure.disposition not in ("settle", "cancel")
        or owner.closure.admission_id != owner.admission_id
        or owner.release_dependencies
        or owner.pending_intents
    ):
        return False
    return settlement.retention == "discard" or any(
        checkpoint.revision == settlement.candidate and checkpoint.retention == settlement.retention
        for checkpoint in owner.checkpoints
    )


def _canonical_settlement_receipt(
    context: SettlementContext, settlement: Settlement
) -> DecisionReceipt | None:
    """Match frozen settlement:<decision_id> authority, including failed commands.

    A rejected command remains canonical even when its decision was discarded.
    It must never become standalone settlement authority after recovery.
    """
    receipt = next(
        (
            item
            for item in context.run.receipts
            if settlement.settlement_id == SettlementId(root=f"settlement:{item.decision_id.root}")
        ),
        None,
    )
    if receipt is None or receipt.decision is None:
        return receipt
    decision = receipt.decision
    if not isinstance(decision, Withdraw) or not isinstance(decision.disposition, Settle):
        raise ContractValidationError("settlement_id", "does not identify a settlement decision")
    if decision.decision_id != receipt.decision_id or decision.target != settlement.attempt:
        raise ContractValidationError("attempt", "differs from canonical settlement decision")
    canonical = _accepted_settlement(settlement, receipt)
    normalized = _normalize_choice(canonical, _owner(context, settlement.attempt))
    if settlement not in (canonical, normalized):
        expected = normalized if settlement.outcome == "cancelled" else canonical
        for field in ("candidate", "assessments", "outcome", "retention", "eligible"):
            if getattr(expected, field) != getattr(settlement, field):
                raise ContractValidationError(field, "differs from canonical settlement decision")
    return receipt


def _accepted_settlement(settlement: Settlement, receipt: DecisionReceipt | None) -> Settlement:
    """Project the accepted disposition as the sole source of requested facts."""
    if receipt is None or not isinstance(receipt.decision, Withdraw):
        return settlement
    disposition = receipt.decision.disposition
    if not isinstance(disposition, Settle):
        return settlement
    return settlement.model_copy(
        update={
            field: getattr(disposition, field)
            for field in ("candidate", "assessments", "outcome", "retention", "eligible")
        }
    )


def _receipt_active(receipt: DecisionReceipt) -> bool:
    return (
        isinstance(receipt.feedback, Accepted)
        and receipt.feedback.decision_id == receipt.decision_id
        and receipt.completion is None
        and isinstance(receipt.decision, Withdraw)
        and isinstance(receipt.decision.disposition, Settle)
    )


def _settlement_receipt(
    context: SettlementContext, settlement: Settlement
) -> DecisionReceipt | None:
    """Find the active owning command by exact frozen routing correspondence."""
    receipt = _canonical_settlement_receipt(context, settlement)
    return receipt if receipt is not None and _receipt_active(receipt) else None


def _unfinished_dependency(
    context: SettlementContext, receipt: DecisionReceipt | None
) -> DecisionId | None:
    if receipt is None or receipt.decision is None:
        return None
    completed = {
        item.decision_id
        for item in context.run.receipts
        if isinstance(item.feedback, Accepted)
        and item.feedback.decision_id == item.decision_id
        and item.completion == CompletionStatus.SUCCEEDED
    }
    return next(
        (dependency for dependency in receipt.decision.depends_on if dependency not in completed),
        None,
    )


def _refuse_proposal(
    state: SettlementState,
    context: SettlementContext,
    proposal: Settlement,
    code: RejectionCode,
    detail: str,
) -> AreaChange[SettlementState]:
    receipt = _settlement_receipt(context, proposal)
    return AreaChange[SettlementState](
        state=state,
        events=()
        if receipt is None
        else (
            Rejected(decision_id=receipt.decision_id, code=code, path=("target",), detail=detail),
        ),
    )


def _finalize(
    state: SettlementState, context: SettlementContext, pending: Settlement
) -> AreaChange[SettlementState]:
    owner = _owner(context, pending.attempt)
    if owner is None:
        return AreaChange[SettlementState](state=state)
    receipt = _canonical_settlement_receipt(context, pending)
    if receipt is not None and not _receipt_active(receipt):
        return AreaChange[SettlementState](state=state)
    final = _normalize_choice(_accepted_settlement(pending, receipt), owner)
    final = final.model_copy(update={"eligible": _eligible(final, owner, context)})
    if not _released(owner, final):
        return AreaChange[SettlementState](state=state)
    if _unfinished_dependency(context, receipt) is not None:
        return AreaChange[SettlementState](state=state)
    return AreaChange[SettlementState](
        state=state.model_copy(
            update={
                "pending": tuple(item for item in state.pending if item.attempt != final.attempt),
                "settlements": (*state.settlements, final),
            }
        ),
        signals=()
        if receipt is None
        else (
            DecisionCompleted(decision_id=receipt.decision_id, status=CompletionStatus.SUCCEEDED),
        ),
        events=(AttemptSettled(settlement=final),),
    )


def _submit(
    state: SettlementState, context: SettlementContext, proposal: Settlement
) -> AreaChange[SettlementState]:
    existing = (*state.settlements, *state.pending)
    previous = next((item for item in existing if item.attempt == proposal.attempt), None)
    if previous is not None:
        if previous.settlement_id == proposal.settlement_id:
            return AreaChange[SettlementState](state=state)
        return _refuse_proposal(
            state,
            context,
            proposal,
            RejectionCode.ALREADY_SETTLED,
            "attempt already has a committed settlement choice",
        )
    if any(item.settlement_id == proposal.settlement_id for item in existing):
        raise ContractValidationError("settlement_id", "already belongs to another attempt")
    owner = _owner(context, proposal.attempt)
    if owner is None:
        return _refuse_proposal(
            state,
            context,
            proposal,
            RejectionCode.OWNERSHIP,
            "attempt does not have current ownership",
        )
    if owner.closure is not None and owner.closure.disposition == "park":
        return _refuse_proposal(
            state,
            context,
            proposal,
            RejectionCode.CLOSED_SCOPE,
            "parked attempt does not accept settlement completion",
        )
    return _accept_proposal(state, context, proposal, owner)


def _accept_proposal(
    state: SettlementState, context: SettlementContext, proposal: Settlement, owner: AttemptView
) -> AreaChange[SettlementState]:
    receipt = _canonical_settlement_receipt(context, proposal)
    if receipt is not None and not _receipt_active(receipt):
        return AreaChange[SettlementState](state=state)
    dependency = _unfinished_dependency(context, receipt)
    if receipt is not None and dependency is not None:
        # Frozen dependency notifications wake intents only. Reject retriably
        # instead of accepting a request-free settlement that cannot wake up.
        return AreaChange[SettlementState](
            state=state,
            events=(
                Rejected(
                    decision_id=receipt.decision_id,
                    code=RejectionCode.DEPENDENCY,
                    path=("depends_on",),
                    detail="settlement dependency has not completed successfully",
                    retry_after=DependencyRef(decision_id=dependency),
                ),
            ),
        )
    settlement = _normalize_choice(_accepted_settlement(proposal, receipt), owner)
    if settlement.retention != "discard" and settlement.candidate is None:
        raise ContractValidationError(
            "candidate", "retained settlement requires an explicit revision"
        )
    if _released(owner, settlement):
        return _finalize(state, context, settlement)
    return AreaChange[SettlementState](
        state=state.model_copy(update={"pending": (*state.pending, settlement)}),
        signals=(
            RetentionRequired(
                attempt=settlement.attempt,
                retention=settlement.retention,
                revision=settlement.candidate,
            ),
        ),
    )


def _cancelled_choice(
    state: SettlementState, context: SettlementContext, attempt: AttemptRef
) -> Settlement | None:
    """The result of an attempt cancelled before it submitted any assessment.

    A cancelled attempt still ends with one ineligible, discarded settlement, so
    the strategy sees every attempt finish exactly once.
    """
    owner = _owner(context, attempt)
    if (
        owner is None
        or owner.closure is None
        or owner.closure.disposition != "cancel"
        or any(item.attempt == attempt for item in (*state.settlements, *state.pending))
    ):
        return None
    return Settlement(
        settlement_id=SettlementId(root=f"settlement:{owner.closure.authority.root}"),
        attempt=attempt,
        candidate=None,
        assessments=(),
        eligible=False,
        retention="discard",
        outcome="cancelled",
    )


def advance(
    state: SettlementState, context: SettlementContext, event: SettlementEvent
) -> AreaChange[SettlementState]:
    """Commit one result per attempt, publishing only after owner release proof.

    Retention is an attempts-owned handshake, never direct workspace I/O. A
    committed withdrawal wins over late completion; pending completion fences
    later withdrawal through the attempts context. Adoption is sibling-owned.
    """
    match event:
        case AssessmentSubmitted(settlement=proposal):
            return _submit(state, context, proposal)
        case OwnershipSettled(attempt=attempt, released=True, blocked=False):
            pending = next((item for item in state.pending if item.attempt == attempt), None)
            if pending is None:
                pending = _cancelled_choice(state, context, attempt)
            return (
                AreaChange[SettlementState](state=state)
                if pending is None
                else _finalize(state, context, pending)
            )
        case OwnershipSettled():
            return AreaChange[SettlementState](state=state)
        case AttemptSettled(settlement=proposal):
            pending = next((item for item in state.pending if item == proposal), None)
            return (
                AreaChange[SettlementState](state=state)
                if pending is None
                else _finalize(state, context, pending)
            )
        case _:
            raise ContractValidationError("event.kind", "event belongs to settlement adoption")
