"""Failure messages for the control protocol.

One place builds the typed failure forms so every transport reports a failed
request the same way: a ``Response`` with ``ok`` false, or a
``ProtocolErrorMessage`` for a stream or a request that never became a typed
request.
"""

from __future__ import annotations

from server.diagnostics import DiagnosticScope, exception_to_diagnostic
from server.wire import PROTOCOL_VERSION, messages
from server.wire.v2 import responses_pb2, server_messages_pb2


def error_response(
    request_id: str,
    error: BaseException,
    *,
    operation: str = "Request",
    scope: DiagnosticScope.ValueType = DiagnosticScope.DIAGNOSTIC_SCOPE_REQUEST,
    code: str | None = None,
) -> responses_pb2.Response:
    """Build a failed response carrying both the legacy string and the diagnostic."""
    diagnostic = exception_to_diagnostic(error, scope=scope, operation=operation, code=code)
    return responses_pb2.Response(
        protocol_version=PROTOCOL_VERSION,
        request_id=request_id,
        timestamp=messages.now(),
        ok=False,
        error=diagnostic.summary,
        diagnostic=diagnostic,
    )


def protocol_error(
    error: BaseException,
    *,
    request_id: str | None = None,
    operation: str = "Protocol operation",
    code: str | None = None,
    message: str | None = None,
) -> server_messages_pb2.ServerMessage:
    """Build a server message reporting a protocol or stream failure.

    ``message`` overrides the diagnostic summary as the human-readable text,
    for errors whose own message already names the fix.
    """
    diagnostic = exception_to_diagnostic(
        error,
        scope=DiagnosticScope.DIAGNOSTIC_SCOPE_PROTOCOL,
        operation=operation,
        code=code,
    )
    return server_messages_pb2.ServerMessage(
        protocol_error=server_messages_pb2.ProtocolErrorMessage(
            request_id=request_id,
            code=diagnostic.code,
            message=message or diagnostic.summary,
            diagnostic=diagnostic,
        )
    )
