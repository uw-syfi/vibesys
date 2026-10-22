"""Pure admission transitions shared by file and in-memory stores."""

from __future__ import annotations

import hashlib

from vs_project.api.admissions import AdmissionConflictError, AdmissionUnconfirmedError
from vs_proto.api import (
    FailureCategory,
    Oneof,
    OperationFailure,
    admission,
    host_operations,
    validate,
)

_STATUS = admission.AdmissionStatus


def checked_receipt(payload: bytes) -> admission.AdmissionReceipt:
    """Decode and cross-check the persisted identity, accepted union and exact bytes."""
    if len(payload) > 4 << 20:
        message = "admission receipt exceeds 4 MiB"
        raise ValueError(message)
    receipt = admission.AdmissionReceipt.from_binary(payload)
    validate(receipt)
    accepted = receipt.accepted
    if accepted is None or receipt.identity is None:
        message = "admission receipt is missing accepted input or identity"
        raise ValueError(message)
    check_input(receipt.identity, accepted)
    encoded = accepted.to_binary()
    if receipt.input_bytes != encoded or receipt.input_digest != hashlib.sha256(encoded).digest():
        message = "admission receipt input bytes/digest mismatch"
        raise ValueError(message)
    return receipt


def check_input(
    identity: admission.AdmissionIdentity, accepted: admission.AcceptedOperation
) -> None:
    """Require one registered, validated request and its declared request identity."""
    validate(identity)
    validate(accepted)
    operation = next(
        (op for op in host_operations.OPERATIONS if op.operation_id == identity.operation_id), None
    )
    if operation is None or accepted.request is None:
        message = "admission identity names an unknown operation"
        raise ValueError(message)
    request = accepted.request.value
    if not isinstance(request, operation.request) or request.request_id != identity.request_id:
        message = "admission identity does not match accepted request"
        raise ValueError(message)
    operation.accept(request)


def initial_receipt(
    identity: admission.AdmissionIdentity,
    targets: tuple[str, ...],
    accepted: admission.AcceptedOperation,
) -> admission.AdmissionReceipt:
    """Create the durable pending state before callers can record an effect intent."""
    check_input(identity, accepted)
    encoded = accepted.to_binary()
    receipt = admission.AdmissionReceipt(
        version=1,
        fence=1,
        identity=identity,
        targets=list(targets),
        accepted=accepted,
        input_bytes=encoded,
        input_digest=hashlib.sha256(encoded).digest(),
        status=_STATUS.PENDING,
    )
    validate(receipt)
    return receipt


def compare(receipt: admission.AdmissionReceipt, requested: admission.AdmissionReceipt) -> None:
    """Digest is an index only: exact bytes and the complete identity must agree."""
    if (
        receipt.identity != requested.identity
        or receipt.input_bytes != requested.input_bytes
        or receipt.targets != requested.targets
    ):
        message = "admission identity already holds different request bytes or targets"
        raise AdmissionConflictError(message)


def record_intent(
    receipt: admission.AdmissionReceipt, evidence: admission.PublicationEvidence
) -> admission.AdmissionReceipt:
    """An existing intent is uncertainty, including after caller death or lost ack."""
    validate(evidence)
    if receipt.status != _STATUS.PENDING or receipt.intent is not None:
        message = "admission intent already exists; reconcile the original effect"
        raise AdmissionUnconfirmedError(message)
    result = admission.AdmissionReceipt.from_binary(receipt.to_binary())
    result.intent = admission.PublicationEvidence.from_binary(evidence.to_binary())
    result.intent.fence = receipt.fence
    validate(result)
    return result


def settle(
    receipt: admission.AdmissionReceipt,
    status: admission.AdmissionStatus,
    result: admission.OperationResult,
) -> admission.AdmissionReceipt:
    """Terminal results never change; confirmed/unconfirmed require prior intent."""
    validate(result)
    if status not in (_STATUS.CONFIRMED, _STATUS.REFUSED, _STATUS.UNCONFIRMED):
        message = "admission settlement requires a terminal status"
        raise ValueError(message)
    if receipt.status != _STATUS.PENDING:
        if receipt.status == status and receipt.result == result:
            return receipt
        message = "admission already has a terminal result"
        raise AdmissionConflictError(message)
    if status != _STATUS.REFUSED and receipt.intent is None:
        message = "admission effect requires durable intent before settlement"
        raise ValueError(message)
    response = result.result
    if receipt.identity is None or receipt.accepted is None or receipt.accepted.request is None:
        message = "admission receipt is incomplete"
        raise ValueError(message)
    operation = next(
        op for op in host_operations.OPERATIONS if op.operation_id == receipt.identity.operation_id
    )
    if response is None:
        message = "admission result is required"
        raise ValueError(message)
    if response.field != "failure":
        accepted = operation.accept(receipt.accepted.request.value)
        operation.validate_response(accepted, response.value)
    elif status == _STATUS.CONFIRMED:
        message = "confirmed admission cannot contain a failure"
        raise ValueError(message)
    updated = admission.AdmissionReceipt.from_binary(receipt.to_binary())
    updated.status = status
    updated.result = result
    validate(updated)
    return updated


def reconcile(
    receipt: admission.AdmissionReceipt, observation: admission.PublicationObservation
) -> admission.AdmissionReceipt:
    """Only proven absence invalidates the old fence and permits continuation."""
    validate(observation)
    observed = observation.observation
    if (
        receipt.intent is None
        or observed is None
        or receipt.intent.effect_id != observed.value.effect_id
    ):
        message = "publication observation does not match original intent"
        raise AdmissionConflictError(message)
    if receipt.status not in (_STATUS.PENDING, _STATUS.UNCONFIRMED):
        message = "terminal admission cannot be reconciled again"
        raise AdmissionConflictError(message)
    pending = admission.AdmissionReceipt.from_binary(receipt.to_binary())
    pending.status = _STATUS.PENDING
    pending.clear_field("result")
    match observed:
        case Oneof("published", published):
            if published.result is None:
                message = "published observation requires a typed result"
                raise ValueError(message)
            return settle(pending, _STATUS.CONFIRMED, published.result)
        case Oneof("absent", _):
            pending.clear_field("intent")
            pending.fence += 1
            return pending
        case Oneof("unknown", _):
            return settle(
                pending,
                _STATUS.UNCONFIRMED,
                admission.OperationResult(
                    result=Oneof(
                        "failure",
                        OperationFailure(
                            category=FailureCategory.ADMISSION_UNCONFIRMED,
                            message="original publication remains unconfirmed",
                        ),
                    )
                ),
            )
        case _:
            message = "unknown publication observation"
            raise ValueError(message)
