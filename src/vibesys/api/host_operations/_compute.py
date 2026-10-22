"""Put, remove, and test one cluster. The same request id runs once.

A replay returns the stored result. A refusal is stored too. An unknown
outcome stays unconfirmed and is not tried again.
"""

from collections.abc import Callable
from typing import Literal

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
_SLOTS = {
    "put_cluster": host_operations.PutCluster,
    "remove_cluster": host_operations.RemoveCluster,
    "test_cluster": host_operations.TestCluster,
}


class ClusterRefusedError(Exception):
    """The cluster change was refused before it changed the machine."""

    def __init__(self, message: str) -> None:
        """Keep the host's words for the stored refusal."""
        super().__init__(message)
        self.message = message


class ClusterUnknownError(Exception):
    """The cluster change returned without a result the receipt can store."""


def serve_cluster(
    payload: bytes,
    *,
    store: AdmissionStore,
    host_user: str,
    apply: Callable[[str, object], object],
) -> bytes:
    """Validate one cluster mutation and return the envelope bytes.

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
        request = MutateRequest.from_binary(payload)
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
                FailureCategory.ADMISSION_UNCONFIRMED, "cluster admission is unconfirmed"
            )
        claim.record_intent(admission.PublicationEvidence(effect_id=request_id, destination=target))
        try:
            response = apply(slot, request)
        except ClusterRefusedError as refused:
            failure = _operation_failure(FailureCategory.CONFLICT, refused.message)
            claim.settle(
                admission.AdmissionStatus.REFUSED,
                admission.OperationResult(result=Oneof("failure", failure)),
            )
            return MutateResponse(result=Oneof("failure", failure)).to_binary()
        except ClusterUnknownError:
            return _failure(
                FailureCategory.ADMISSION_UNCONFIRMED, "cluster admission is unconfirmed"
            )
        result = _confirmed_result(slot, response)
        claim.settle(admission.AdmissionStatus.CONFIRMED, admission.OperationResult(result=result))
        return MutateResponse(result=result).to_binary()


def _target(request: object) -> tuple[str, str]:
    if isinstance(request, pb.RemoveClusterRequest):
        return request.request_id, request.name
    if isinstance(request, pb.PutClusterRequest | pb.TestClusterRequest):
        cluster = request.cluster
        if cluster is None:
            message = "cluster request has no cluster"
            raise TypeError(message)
        return request.request_id, cluster.name
    message = "cluster request has no target"
    raise TypeError(message)


def _stored(slot: str, observed: admission.AdmissionReceipt) -> bytes | None:
    result = observed.result
    if result is None or result.result is None:
        return None
    body = result.result
    if body.field == "failure" and isinstance(body.value, OperationFailure):
        return MutateResponse(result=Oneof("failure", body.value)).to_binary()
    return _replay(slot, body.field, body.value)


def _operation_failure(category: FailureCategory, message: str) -> OperationFailure:
    text = message.strip() or "cluster change failed"
    encoded = text.encode()
    if len(encoded) > _MAX_DIAGNOSTIC:
        text = encoded[:_MAX_DIAGNOSTIC].decode(errors="ignore") or "cluster change failed"
    return OperationFailure(category=category, message=text)


def _accepted(request: object) -> admission.AcceptedOperation:
    if isinstance(request, pb.PutClusterRequest):
        return admission.AcceptedOperation(request=Oneof("put_cluster", request))
    if isinstance(request, pb.RemoveClusterRequest):
        return admission.AcceptedOperation(request=Oneof("remove_cluster", request))
    if isinstance(request, pb.TestClusterRequest):
        return admission.AcceptedOperation(request=Oneof("test_cluster", request))
    message = "cluster request is not a cluster mutation"
    raise TypeError(message)


def _confirmed_result(
    slot: str, response: object
) -> (
    Oneof[Literal["put_cluster"], pb.PutClusterResponse]
    | Oneof[Literal["remove_cluster"], pb.RemoveClusterResponse]
    | Oneof[Literal["test_cluster"], pb.TestClusterResponse]
):
    if slot == "put_cluster" and isinstance(response, pb.PutClusterResponse):
        validate(response)
        return Oneof("put_cluster", response)
    if slot == "remove_cluster" and isinstance(response, pb.RemoveClusterResponse):
        validate(response)
        return Oneof("remove_cluster", response)
    if slot == "test_cluster" and isinstance(response, pb.TestClusterResponse):
        validate(response)
        return Oneof("test_cluster", response)
    message = "cluster response does not match its request"
    raise TypeError(message)


def _replay(slot: str, field: str, value: object) -> bytes | None:
    if field != slot:
        return None
    return MutateResponse(result=_confirmed_result(slot, value)).to_binary()


def _failure(category: FailureCategory, message: str) -> bytes:
    return MutateResponse(
        result=Oneof("failure", _operation_failure(category, message))
    ).to_binary()
