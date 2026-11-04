"""Pure admission transitions shared by file and in-memory stores."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from vs_project.api.admissions import (
    AdmissionBusyError,
    AdmissionConflictError,
    AdmissionUnconfirmedError,
)

if TYPE_CHECKING:
    from collections.abc import Iterable
from vs_proto.api import (
    FailureCategory,
    Oneof,
    OperationFailure,
    admission,
    host_operations,
    validate,
    validated_operation_bytes,
)

_STATUS = admission.AdmissionStatus
_OCCUPIED = frozenset({_STATUS.PENDING, _STATUS.UNCONFIRMED})


def checked_receipt(payload: bytes) -> admission.AdmissionReceipt:
    """Decode and cross-check the persisted identity, accepted union and exact bytes."""
    if len(payload) > 4 << 20:
        message = "admission receipt exceeds 4 MiB"
        raise ValueError(message)
    receipt = admission.AdmissionReceipt.from_binary(payload)
    validate(receipt)
    if receipt.to_binary() != receipt.to_binary(write_unknown_fields=False):
        message = "admission receipt contains unknown fields"
        raise ValueError(message)
    accepted = receipt.accepted
    if accepted is None or receipt.identity is None:
        message = "admission receipt is missing accepted input or identity"
        raise ValueError(message)
    check_input(receipt.identity, accepted)
    encoded = validated_operation_bytes(accepted)
    if receipt.input_bytes != encoded or receipt.input_digest != hashlib.sha256(encoded).digest():
        message = "admission receipt input bytes/digest mismatch"
        raise ValueError(message)
    if receipt.intent is not None and receipt.intent.fence != receipt.fence:
        message = "admission intent fence does not match receipt"
        raise ValueError(message)
    if receipt.result is not None:
        check_result(receipt, receipt.status, receipt.result)
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
    encoded = validated_operation_bytes(accepted)
    receipt = admission.AdmissionReceipt(
        version=1,
        fence=1,
        identity=admission.AdmissionIdentity.from_binary(identity.to_binary()),
        targets=list(targets),
        accepted=admission.AcceptedOperation.from_binary(encoded),
        input_bytes=encoded,
        input_digest=hashlib.sha256(encoded).digest(),
        status=_STATUS.PENDING,
    )
    return checked_receipt(receipt.to_binary())


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
    validated_operation_bytes(evidence)
    if evidence.fence not in (0, receipt.fence):
        message = "publication evidence has a stale admission fence"
        raise AdmissionConflictError(message)
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
    validated_operation_bytes(result)
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
    check_result(receipt, status, result)
    updated = admission.AdmissionReceipt.from_binary(receipt.to_binary())
    updated.status = status
    updated.result = admission.OperationResult.from_binary(validated_operation_bytes(result))
    validate(updated)
    return updated


def check_result(
    receipt: admission.AdmissionReceipt,
    status: admission.AdmissionStatus,
    result: admission.OperationResult,
) -> None:
    """Terminal classification and operation identity agree with the typed result."""
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
        if status != _STATUS.CONFIRMED:
            message = "nonconfirmed admission must contain a typed failure"
            raise ValueError(message)
    elif status == _STATUS.CONFIRMED:
        message = "confirmed admission cannot contain a failure"
        raise ValueError(message)
    if response.field == "failure" and (
        (status == _STATUS.UNCONFIRMED)
        != (response.value.category == FailureCategory.ADMISSION_UNCONFIRMED)
    ):
        message = "unconfirmed admission requires ADMISSION_UNCONFIRMED failure"
        raise ValueError(message)


def intent_occupies(receipt: admission.AdmissionReceipt) -> bool:
    """A receipt owns its targets until completion or proven absence."""
    return receipt.intent is not None and receipt.status in _OCCUPIED


def reject_occupied_targets(
    receipts: Iterable[admission.AdmissionReceipt],
    identity: admission.AdmissionIdentity,
    targets: tuple[str, ...],
) -> None:
    """Refuse a claim whose targets still belong to another unresolved intent."""
    wanted = set(targets)
    for receipt in receipts:
        if receipt.identity == identity or not intent_occupies(receipt):
            continue
        if wanted.isdisjoint(receipt.targets):
            continue
        message = "admission target is occupied by an unresolved intent"
        raise AdmissionBusyError(message)


def reconcile(
    receipt: admission.AdmissionReceipt, observation: admission.PublicationObservation
) -> admission.AdmissionReceipt:
    """Only an observation of this exact intent can prove absence or completion."""
    validated_operation_bytes(observation)
    observed = observation.observation
    if receipt.intent is None or observed is None or receipt.intent != observed.value.intent:
        message = "publication observation does not match the fenced intent"
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
