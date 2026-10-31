"""Inspect the candidate directory one HostRead envelope names.

The candidate half of the host operations: ``vibesys host inspect-candidate`` imports it alone,
without the admission and login modules that ``api`` brings.
"""

from vibesys.api.host_operations._candidate import inspect_candidate
from vibesys.api.host_operations.reads import MAX_REQUEST_BYTES, host_read_failure
from vs_proto.api import (
    FailureCategory,
    Oneof,
    ReadRequest,
    ReadResponse,
    ValidationError,
    validate,
)

__all__ = ["execute_inspect_candidate"]


def execute_inspect_candidate(payload: bytes) -> bytes:
    """Inspect the candidate named by one HostRead envelope.

    A missing path is ``NOT_FOUND`` state, not a failure. Permission and an
    unusable path are typed failures. The schema hash is this build's hash;
    the gateway refuses a different one.
    """
    if len(payload) > MAX_REQUEST_BYTES:
        return host_read_failure(FailureCategory.INVALID_INPUT, "HostRead request exceeds 1 MiB")
    try:
        request = ReadRequest.from_binary(payload)
        validate(request)
    except (ValidationError, ValueError) as exc:
        return host_read_failure(FailureCategory.INVALID_INPUT, str(exc))
    method = request.method
    if method is None or method.field != "inspect_candidate":
        return host_read_failure(FailureCategory.INVALID_INPUT, "HostRead.method is required")
    try:
        inspected = inspect_candidate(method.value)
        validate(inspected)
    except PermissionError as exc:
        return host_read_failure(FailureCategory.PERMISSION_DENIED, str(exc) or "permission denied")
    except (ValidationError, ValueError, TypeError) as exc:
        return host_read_failure(FailureCategory.INVALID_INPUT, str(exc))
    return ReadResponse(result=Oneof("inspect_candidate", inspected)).to_binary()
