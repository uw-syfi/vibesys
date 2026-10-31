"""Typed host-operation facade: reads, candidate inspection and the admitted workflows.

The read half lives in ``reads``, which a process that only answers a registered read
imports alone; this module re-exports it with the admission, login and candidate halves.
The generated registry owns routing, validation, budgets and correlation. The adapter
supplies host facts through ReadSource; no server module enters here.
"""

from vibesys.api.host_operations._candidate import inspect_candidate
from vibesys.api.host_operations._compute import (
    ClusterRefusedError,
    ClusterUnknownError,
    serve_cluster,
)
from vibesys.api.host_operations._login import LoginRefusedFact, LoginStartedFact, serve_login
from vibesys.api.host_operations._project import (
    ProjectRefusedError,
    ProjectUnknownError,
    serve_project,
)
from vibesys.api.host_operations._saved_runs import (
    host_watch_failure,
    saved_run_frames,
    serve_saved_runs,
)
from vibesys.api.host_operations._start import StartRefusedError, StartUnknownError, serve_start
from vibesys.api.host_operations.reads import (
    MAX_REQUEST_BYTES,
    ReadSource,
    execute_host_read,
    execute_read,
    host_read_failure,
)
from vs_proto.api import (
    FailureCategory,
    Oneof,
    ReadRequest,
    ReadResponse,
    ValidationError,
    validate,
)

__all__ = [
    "ClusterRefusedError",
    "ClusterUnknownError",
    "LoginRefusedFact",
    "LoginStartedFact",
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
]


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
