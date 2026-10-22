"""Create a project from an example, or copy one out. The same request id runs once.

A replay returns the stored result. A destination that already holds something
is a stored answer, not a second create. A refusal is stored too.
"""

from collections.abc import Callable
from typing import Literal

from vs_project.api import AdmissionBusyError, AdmissionConflictError, AdmissionStore
from vs_proto.api import (
    FailureCategory,
    HostMutateRequest,
    HostMutateResponse,
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
_SLOTS = {
    "create_example": host_operations.CreateExample,
    "copy_project": host_operations.CopyProject,
}


class ProjectRefusedError(Exception):
    """The project change was refused before it changed the machine."""

    def __init__(self, message: str) -> None:
        """Keep the host's words for the stored refusal."""
        super().__init__(message)
        self.message = message


class ProjectUnknownError(Exception):
    """The project change returned without a result the receipt can store."""


def serve_project(
    payload: bytes,
    *,
    store: AdmissionStore,
    host_user: str,
    apply: Callable[[str, object], object],
) -> bytes:
    """Validate one project mutation and return the envelope bytes.

    ``apply`` receives the slot and the decoded request, and returns that
    slot's response message. It runs only when this request id has no outcome.
    """
    if len(payload) > _MAX_BYTES:
        return _failure(FailureCategory.INVALID_INPUT, "HostMutate request exceeds 1 MiB")
    decoded = _request(payload)
    if isinstance(decoded, bytes):
        return decoded
    slot, request = decoded
    try:
        return _admit(slot, request, store=store, host_user=host_user, apply=apply)
    except AdmissionBusyError:
        return _failure(FailureCategory.CONFLICT, "admission is busy")
    except AdmissionConflictError as exc:
        return _failure(FailureCategory.CONFLICT, str(exc))


def _request(payload: bytes) -> tuple[str, object] | bytes:
    try:
        request = HostMutateRequest.from_binary(payload)
        validate(request)
    except (ValidationError, ValueError) as exc:
        return _failure(FailureCategory.INVALID_INPUT, str(exc))
    method = request.method
    if method is None or method.field not in _SLOTS:
        return _failure(FailureCategory.INVALID_INPUT, "HostMutate.method is required")
    return method.field, method.value


def _admit(
    slot: str,
    request: object,
    *,
    store: AdmissionStore,
    host_user: str,
    apply: Callable[[str, object], object],
) -> bytes:
    operation = _SLOTS[slot]
    request_id, target = _target(request)
    identity = admission.AdmissionIdentity(
        operation_id=operation.operation_id, request_id=request_id, host_user_target=host_user
    )
    accepted = _accepted(request)
    validate(identity)
    validate(accepted)
    with store.claim(identity, (target,), accepted) as claim:
        observed = claim.observe()
        stored = _stored(slot, observed)
        if stored is not None:
            return stored
        if observed.intent is not None:
            return _failure(
                FailureCategory.ADMISSION_UNCONFIRMED, "project admission is unconfirmed"
            )
        claim.record_intent(admission.PublicationEvidence(effect_id=request_id, destination=target))
        try:
            response = apply(slot, request)
        except ProjectRefusedError as refused:
            failure = _operation_failure(FailureCategory.CONFLICT, refused.message)
            claim.settle(
                admission.AdmissionStatus.REFUSED,
                admission.OperationResult(result=Oneof("failure", failure)),
            )
            return HostMutateResponse(result=Oneof("failure", failure)).to_binary()
        except ProjectUnknownError:
            return _failure(
                FailureCategory.ADMISSION_UNCONFIRMED, "project admission is unconfirmed"
            )
        result = _confirmed_result(slot, response)
        claim.settle(admission.AdmissionStatus.CONFIRMED, admission.OperationResult(result=result))
        return HostMutateResponse(result=result).to_binary()


def _target(request: object) -> tuple[str, str]:
    if isinstance(request, pb.CreateExampleRequest | pb.CopyProjectRequest):
        return request.request_id, request.destination
    message = "project request has no destination"
    raise TypeError(message)


def _accepted(request: object) -> admission.AcceptedOperation:
    if isinstance(request, pb.CreateExampleRequest):
        return admission.AcceptedOperation(request=Oneof("create_example", request))
    if isinstance(request, pb.CopyProjectRequest):
        return admission.AcceptedOperation(request=Oneof("copy_project", request))
    message = "project request is not a project mutation"
    raise TypeError(message)


def _confirmed_result(
    slot: str, response: object
) -> (
    Oneof[Literal["create_example"], pb.CreateExampleResponse]
    | Oneof[Literal["copy_project"], pb.CopyProjectResponse]
):
    if slot == "create_example" and isinstance(response, pb.CreateExampleResponse):
        validate(response)
        return Oneof("create_example", response)
    if slot == "copy_project" and isinstance(response, pb.CopyProjectResponse):
        validate(response)
        return Oneof("copy_project", response)
    message = "project response does not match its request"
    raise TypeError(message)


def _stored(slot: str, observed: admission.AdmissionReceipt) -> bytes | None:
    result = observed.result
    if result is None or result.result is None:
        return None
    body = result.result
    if body.field == "failure" and isinstance(body.value, OperationFailure):
        return HostMutateResponse(result=Oneof("failure", body.value)).to_binary()
    if body.field != slot:
        return None
    return HostMutateResponse(result=_confirmed_result(slot, body.value)).to_binary()


def _operation_failure(category: FailureCategory, message: str) -> OperationFailure:
    text = message.strip() or "project change failed"
    encoded = text.encode()
    if len(encoded) > _MAX_DIAGNOSTIC:
        text = encoded[:_MAX_DIAGNOSTIC].decode(errors="ignore") or "project change failed"
    return OperationFailure(category=category, message=text)


def _failure(category: FailureCategory, message: str) -> bytes:
    return HostMutateResponse(
        result=Oneof("failure", _operation_failure(category, message))
    ).to_binary()
