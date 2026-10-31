"""Typed read-operation facade behind existing host adapters.

The generated registry owns routing, validation, budgets and correlation. The
adapter supplies host facts through ReadSource; no server module enters here.
"""

from typing import Any, cast

from vibesys.api.host_operations._candidate import inspect_candidate
from vibesys.api.host_operations._compute import (
    ClusterRefusedError,
    ClusterUnknownError,
    serve_cluster,
)
from vibesys.api.host_operations._handlers import handlers
from vibesys.api.host_operations._login import LoginRefusedFact, LoginStartedFact, serve_login
from vibesys.api.host_operations._project import (
    ProjectRefusedError,
    ProjectUnknownError,
    serve_project,
)
from vibesys.api.host_operations._reads import ReadSource
from vibesys.api.host_operations._saved_runs import (
    host_watch_failure,
    saved_run_frames,
    serve_saved_runs,
)
from vibesys.api.host_operations._start import StartRefusedError, StartUnknownError, serve_start
from vibesys.api.host_operations._usage_prices import (
    PricesRefusedError,
    PricesUnknownError,
    serve_usage_prices,
)
from vs_proto.api import (
    AcceptedRequest,
    FailureCategory,
    Oneof,
    OperationFailure,
    ReadRequest,
    ReadResponse,
    ValidationError,
    host_operations,
    validate,
)

__all__ = [
    "ClusterRefusedError",
    "ClusterUnknownError",
    "LoginRefusedFact",
    "LoginStartedFact",
    "PricesRefusedError",
    "PricesUnknownError",
    "ProjectRefusedError",
    "ProjectUnknownError",
    "ReadSource",
    "StartRefusedError",
    "StartUnknownError",
    "execute_host_read",
    "execute_inspect_candidate",
    "execute_read",
    "host_read_failure",
    "host_watch_failure",
    "saved_run_frames",
    "serve_cluster",
    "serve_login",
    "serve_project",
    "serve_saved_runs",
    "serve_start",
    "serve_usage_prices",
]

_READ_COMMANDS = {item.slot: item.cli for item in host_operations.READ_CALLS}

_MAX_BYTES = 1 << 20
_MAX_DIAGNOSTIC = 65536


def execute_read(request: AcceptedRequest, source: ReadSource) -> bytes:
    """Execute one immutable accepted request with complete typed handler wiring."""
    return handlers(source).dispatch(request)


def host_read_failure(category: FailureCategory, message: str) -> bytes:
    """A closed failure envelope. Callers branch on category, not on the text."""
    text = message.strip() or "host read failed"
    encoded = text.encode()
    if len(encoded) > _MAX_DIAGNOSTIC:
        text = encoded[:_MAX_DIAGNOSTIC].decode(errors="ignore") or "host read failed"
    failure = OperationFailure(category=category, message=text)
    return ReadResponse(result=Oneof("failure", failure)).to_binary()


def execute_host_read(payload: bytes, source: ReadSource, *, cli: str) -> bytes:
    """Validate one HostRead envelope and return the typed response bytes.

    ``cli`` is the generated command the gateway selected. A mismatch is invalid
    input, not a second route.
    """
    if len(payload) > _MAX_BYTES:
        return host_read_failure(FailureCategory.INVALID_INPUT, "HostRead request exceeds 1 MiB")
    try:
        request = ReadRequest.from_binary(payload)
        validate(request)
    except (ValidationError, ValueError) as exc:
        return host_read_failure(FailureCategory.INVALID_INPUT, str(exc))
    method = request.method
    if method is None or method.field not in host_operations.READS:
        return host_read_failure(FailureCategory.INVALID_INPUT, "HostRead.method is required")
    operation = host_operations.READS[method.field]
    expected = _READ_COMMANDS.get(method.field)
    if expected is None or expected != cli:
        return host_read_failure(
            FailureCategory.INVALID_INPUT, "read command does not match the envelope"
        )
    try:
        accepted = operation.accept(method.value)
        encoded = handlers(source).dispatch(accepted)
        response = operation.response.from_binary(encoded)
        validate(response)
    except (ValidationError, ValueError, TypeError) as exc:
        return host_read_failure(FailureCategory.INVALID_INPUT, str(exc))
    # The slot and the decoded response are the same registry pair. The constructor
    # wants each pair spelled out, which a dynamic slot cannot name one by one.
    return ReadResponse(result=cast("Any", Oneof(method.field, response))).to_binary()


def execute_inspect_candidate(payload: bytes) -> bytes:
    """Inspect the candidate named by one HostRead envelope.

    A missing path is ``NOT_FOUND`` state, not a failure. Permission and an
    unusable path are typed failures. The schema hash is this build's hash;
    the gateway refuses a different one.
    """
    if len(payload) > _MAX_BYTES:
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
