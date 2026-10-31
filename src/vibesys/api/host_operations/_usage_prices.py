"""Save the prices a person set. The same request id runs once.

A replay returns the stored result. A refusal (a book that repeats a key or loops) is stored too.
An unknown outcome stays unconfirmed and is not tried again, as for every admitted change; the
save is also idempotent by content, so asking again with the same book is safe in any case.
"""

from collections.abc import Callable

from vs_project.api import AdmissionBusyError, AdmissionConflictError, AdmissionStore
from vs_proto.api import (
    FailureCategory,
    MutateRequest,
    MutateResponse,
    Oneof,
    OperationFailure,
    ValidationError,
    admission,
    host_operations,
    validate,
)
from vs_proto.api import host_payloads as pb

_MAX_BYTES = 1 << 20
_MAX_DIAGNOSTIC = 65536
_SLOT = "usage_prices_save"
_TARGET = "usage-prices"
"""What the claim names: a host keeps one price book, so one save runs at a time."""


class PricesRefusedError(Exception):
    """The book was refused before it changed the host; ``message`` names the offending key."""

    def __init__(self, message: str) -> None:
        """Keep the words for the stored refusal."""
        super().__init__(message)
        self.message = message


class PricesUnknownError(Exception):
    """The save returned without a result the receipt can store."""


def serve_usage_prices(
    payload: bytes,
    *,
    store: AdmissionStore,
    host_user: str,
    apply: Callable[[pb.UsagePricesSaveRequest], pb.UsagePricesSaveResponse],
) -> bytes:
    """Validate one save and return the envelope bytes.

    ``apply`` receives the decoded request and returns the response. It runs only when this
    request id has no outcome.
    """
    if len(payload) > _MAX_BYTES:
        return _failure(FailureCategory.INVALID_INPUT, "HostMutate request exceeds 1 MiB")
    try:
        envelope = MutateRequest.from_binary(payload)
        validate(envelope)
    except (ValidationError, ValueError) as exc:
        return _failure(FailureCategory.INVALID_INPUT, str(exc))
    method = envelope.method
    if (
        method is None
        or method.field != _SLOT
        or not isinstance(method.value, pb.UsagePricesSaveRequest)
    ):
        return _failure(FailureCategory.INVALID_INPUT, "HostMutate.method is required")
    try:
        return _admit(method.value, store=store, host_user=host_user, apply=apply)
    except AdmissionBusyError:
        return _failure(FailureCategory.CONFLICT, "admission is busy")
    except AdmissionConflictError as exc:
        return _failure(FailureCategory.CONFLICT, str(exc))


def _admit(
    request: pb.UsagePricesSaveRequest,
    *,
    store: AdmissionStore,
    host_user: str,
    apply: Callable[[pb.UsagePricesSaveRequest], pb.UsagePricesSaveResponse],
) -> bytes:
    identity = admission.AdmissionIdentity(
        operation_id=host_operations.UsagePricesSave.operation_id,
        request_id=request.request_id,
        host_user_target=host_user,
    )
    accepted = admission.AcceptedOperation(request=Oneof(_SLOT, request))
    validate(identity)
    validate(accepted)
    with store.claim(identity, (_TARGET,), accepted) as claim:
        observed = claim.observe()
        stored = _stored(observed)
        if stored is not None:
            return stored
        if observed.intent is not None:
            return _failure(FailureCategory.ADMISSION_UNCONFIRMED, "price save is unconfirmed")
        claim.record_intent(
            admission.PublicationEvidence(effect_id=request.request_id, destination=_TARGET)
        )
        try:
            response = apply(request)
        except PricesRefusedError as refused:
            failure = _operation_failure(FailureCategory.INVALID_INPUT, refused.message)
            claim.settle(
                admission.AdmissionStatus.REFUSED,
                admission.OperationResult(result=Oneof("failure", failure)),
            )
            return MutateResponse(result=Oneof("failure", failure)).to_binary()
        except PricesUnknownError:
            return _failure(FailureCategory.ADMISSION_UNCONFIRMED, "price save is unconfirmed")
        validate(response)
        result = Oneof(_SLOT, response)
        claim.settle(admission.AdmissionStatus.CONFIRMED, admission.OperationResult(result=result))
        return MutateResponse(result=result).to_binary()


def _stored(observed: admission.AdmissionReceipt) -> bytes | None:
    result = observed.result
    if result is None or result.result is None:
        return None
    body = result.result
    if body.field == "failure" and isinstance(body.value, OperationFailure):
        return MutateResponse(result=Oneof("failure", body.value)).to_binary()
    if body.field == _SLOT and isinstance(body.value, pb.UsagePricesSaveResponse):
        validate(body.value)
        return MutateResponse(result=Oneof(_SLOT, body.value)).to_binary()
    return None


def _operation_failure(category: FailureCategory, message: str) -> OperationFailure:
    text = message.strip() or "price save failed"
    encoded = text.encode()
    if len(encoded) > _MAX_DIAGNOSTIC:
        text = encoded[:_MAX_DIAGNOSTIC].decode(errors="ignore") or "price save failed"
    return OperationFailure(category=category, message=text)


def _failure(category: FailureCategory, message: str) -> bytes:
    return MutateResponse(
        result=Oneof("failure", _operation_failure(category, message))
    ).to_binary()
