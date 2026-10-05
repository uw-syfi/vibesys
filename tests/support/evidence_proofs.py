"""Evidence references as core's ledger embeds them in operation requests."""

from typing import ClassVar, Literal

from pydantic import BaseModel

from vs_core.api import (
    EventId,
    EvidenceAcceptanceReceipt,
    EvidenceId,
    EvidenceKind,
    EvidenceRef,
    LifecycleClass,
    Observation,
    ObservationStatus,
    OperationRequest,
    RequestId,
    RevisionRef,
    RunId,
    Scope,
    Value,
)

_DIGEST = "a" * 64


def accepted_accuracy_proof(candidate: RevisionRef) -> EvidenceRef:
    """Successful, trusted correctness evidence of ``candidate`` with its acceptance receipt."""
    scope = Scope(owner=RunId(root="run"), generation=0)
    source = RequestId(root="accuracy")
    return EvidenceRef(
        evidence_id=EvidenceId(root="accuracy-evidence"),
        kind=EvidenceKind.CORRECTNESS,
        purpose="official",
        scope=scope,
        source_request=source,
        candidate=candidate,
        observation_sequence=0,
        evaluator_digest=_DIGEST,
        workload_digest=_DIGEST,
        environment_digest=_DIGEST,
        provenance="trusted",
        status=ObservationStatus.SUCCEEDED,
        acceptance_receipt=EvidenceAcceptanceReceipt(
            observation=Observation(
                event_id=EventId(root="accepted"),
                request_id=source,
                scope=scope,
                sequence=0,
                observed_at=0.0,
                status=ObservationStatus.SUCCEEDED,
                accepted=True,
                terminal=True,
            )
        ),
    )


class RetainWithProof(OperationRequest):
    """A retention request in the shape ``RetainRevisionOwner`` serves."""

    kind: Literal["test.retain-with-proof"] = "test.retain-with-proof"
    lifecycle: Literal[LifecycleClass.IDEMPOTENT_WRITE] = LifecycleClass.IDEMPOTENT_WRITE
    outcome_model: ClassVar[type[BaseModel]] = Value
    revision: RevisionRef
    accuracy_proof: EvidenceRef
