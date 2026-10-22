"""Admit one start, then launch it once.

Intent is durable before the launch. A caller that disconnects does not undo
that intent: the same request id returns the stored result, or an unconfirmed
admission when the launch's outcome is still unknown. It does not launch again.
"""

from collections.abc import Callable

from vs_project.api import (
    AdmissionBusyError,
    AdmissionConflictError,
    AdmissionStore,
)
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
_OPERATION = host_operations.StartRun.operation_id


class StartUnknownError(Exception):
    """The launch returned without a result the admission can store."""


class StartRefusedError(Exception):
    """The start was refused before a run existed."""

    def __init__(self, message: str) -> None:
        """Keep the host's words for the stored refusal."""
        super().__init__(message)
        self.message = message


def serve_start(
    payload: bytes,
    *,
    store: AdmissionStore,
    launch: Callable[[pb.StartRunRequest], tuple[str, str]],
    host_user: str,
) -> bytes:
    """Validate one HostMutate start and return the envelope bytes.

    ``launch`` runs only after intent is stored, and only when this request id
    has no outcome yet. It returns ``(run_id, instance_id)``.
    """
    if len(payload) > _MAX_BYTES:
        return _failure(FailureCategory.INVALID_INPUT, "HostMutate request exceeds 1 MiB")
    start = _request(payload)
    if isinstance(start, bytes):
        return start
    try:
        return _admit(start, store=store, launch=launch, host_user=host_user)
    except AdmissionBusyError:
        return _failure(FailureCategory.CONFLICT, "admission is busy")
    except AdmissionConflictError as exc:
        return _failure(FailureCategory.CONFLICT, str(exc))


def _request(payload: bytes) -> pb.StartRunRequest | bytes:
    try:
        request = MutateRequest.from_binary(payload)
        validate(request)
    except (ValidationError, ValueError) as exc:
        return _failure(FailureCategory.INVALID_INPUT, str(exc))
    method = request.method
    if (
        method is None
        or method.field != "start_run"
        or not isinstance(method.value, pb.StartRunRequest)
    ):
        return _failure(FailureCategory.INVALID_INPUT, "HostMutate.method is required")
    return method.value


def _admit(
    start: pb.StartRunRequest,
    *,
    store: AdmissionStore,
    launch: Callable[[pb.StartRunRequest], tuple[str, str]],
    host_user: str,
) -> bytes:
    identity = admission.AdmissionIdentity(
        operation_id=_OPERATION, request_id=start.request_id, host_user_target=host_user
    )
    accepted = admission.AcceptedOperation(request=Oneof("start_run", start))
    validate(identity)
    validate(accepted)
    with store.claim(identity, (start.project,), accepted) as claim:
        observed = claim.observe()
        stored = _stored(observed)
        if stored is not None:
            return stored
        if observed.intent is not None:
            return _failure(
                FailureCategory.ADMISSION_UNCONFIRMED,
                "start admission is unconfirmed",
            )
        claim.record_intent(
            admission.PublicationEvidence(effect_id=start.request_id, destination=start.project)
        )
        try:
            run_id, instance_id = launch(start)
        except StartRefusedError as refused:
            failure = _operation_failure(FailureCategory.CONFLICT, refused.message)
            claim.settle(
                admission.AdmissionStatus.REFUSED,
                admission.OperationResult(result=Oneof("failure", failure)),
            )
            return MutateResponse(result=Oneof("failure", failure)).to_binary()
        except StartUnknownError:
            return _failure(
                FailureCategory.ADMISSION_UNCONFIRMED,
                "start admission is unconfirmed",
            )
        started = pb.StartRunResponse(
            request_id=start.request_id, run_id=run_id, instance_id=instance_id
        )
        validate(started)
        claim.settle(
            admission.AdmissionStatus.CONFIRMED,
            admission.OperationResult(result=Oneof("start_run", started)),
        )
        return MutateResponse(result=Oneof("start_run", started)).to_binary()


def _stored(observed: admission.AdmissionReceipt) -> bytes | None:
    result = observed.result
    if result is None or result.result is None:
        return None
    body = result.result
    if body.field == "start_run" and isinstance(body.value, pb.StartRunResponse):
        return MutateResponse(result=Oneof("start_run", body.value)).to_binary()
    if body.field == "failure" and isinstance(body.value, OperationFailure):
        return MutateResponse(result=Oneof("failure", body.value)).to_binary()
    return None


def _operation_failure(category: FailureCategory, message: str) -> OperationFailure:
    text = message.strip() or "host start failed"
    encoded = text.encode()
    if len(encoded) > _MAX_DIAGNOSTIC:
        text = encoded[:_MAX_DIAGNOSTIC].decode(errors="ignore") or "host start failed"
    return OperationFailure(category=category, message=text)


def _failure(category: FailureCategory, message: str) -> bytes:
    return MutateResponse(
        result=Oneof("failure", _operation_failure(category, message))
    ).to_binary()
