"""Tests for the server diagnostic contract."""

import pytest

from server.api.errors import error_response, protocol_error
from server.diagnostics import (
    DiagnosticRetryability,
    DiagnosticScope,
    DiagnosticSeverity,
    exception_detail,
    exception_to_diagnostic,
    make_diagnostic,
    redact_diagnostic_text,
)
from server.wire import codec, messages
from server.wire.v2 import common_pb2, events_pb2, responses_pb2, server_messages_pb2


def test_diagnostic_round_trips_on_protocol_and_event_messages() -> None:
    diagnostic = make_diagnostic(
        code="permission_denied",
        summary="The operation was denied",
        detail="PermissionError: sandbox rejected the operation",
        scope=DiagnosticScope.DIAGNOSTIC_SCOPE_INVOCATION,
        severity=DiagnosticSeverity.DIAGNOSTIC_SEVERITY_FATAL,
        retryability=DiagnosticRetryability.DIAGNOSTIC_RETRYABILITY_NEVER,
        hint="Check the sandbox permissions.",
        debug_ref="run-events.jsonl:12",
    )

    response = responses_pb2.Response(
        request_id="request", ok=False, error=diagnostic.summary, diagnostic=diagnostic
    )
    restored_response = codec.loads(responses_pb2.Response, codec.dumps(response))
    assert restored_response.diagnostic == diagnostic

    protocol = server_messages_pb2.ProtocolErrorMessage(
        code="request_failed", message=diagnostic.summary, diagnostic=diagnostic
    )
    restored_protocol = codec.loads(server_messages_pb2.ProtocolErrorMessage, codec.dumps(protocol))
    assert restored_protocol.diagnostic == diagnostic

    event = messages.make_event(
        events_pb2.EventType.EVENT_TYPE_RUN_FAILED, diagnostic.summary, diagnostic=diagnostic
    )
    restored_event = codec.loads(events_pb2.RunEvent, codec.dumps(event))
    assert restored_event.diagnostic == diagnostic


def test_payloads_without_diagnostic_still_parse() -> None:
    response = codec.loads(
        responses_pb2.Response, '{"request_id":"request","ok":false,"error":"failed"}'
    )
    assert not response.HasField("diagnostic")
    event = codec.loads(
        events_pb2.RunEvent,
        '{"protocol_version":2,"timestamp":"2026-01-01T00:00:00Z",'
        '"type":"EVENT_TYPE_RUN_FAILED","text":"failed"}',
    )
    assert not event.HasField("diagnostic")


def test_exception_conversion_maps_type_and_redacts_credentials() -> None:
    diagnostic = exception_to_diagnostic(
        PermissionError("token=abc123 Bearer secret-value"),
        scope=DiagnosticScope.DIAGNOSTIC_SCOPE_TRANSPORT,
        operation="Codex startup",
        retryability=DiagnosticRetryability.DIAGNOSTIC_RETRYABILITY_MANUAL,
    )
    assert diagnostic.code == "permission_denied"
    assert diagnostic.summary == "Codex startup was denied"
    assert diagnostic.detail == "PermissionError: token=[REDACTED] Bearer [REDACTED]"
    assert diagnostic.summary != diagnostic.detail
    assert diagnostic.scope == DiagnosticScope.DIAGNOSTIC_SCOPE_TRANSPORT
    assert diagnostic.severity == DiagnosticSeverity.DIAGNOSTIC_SEVERITY_ERROR
    assert diagnostic.retryability == DiagnosticRetryability.DIAGNOSTIC_RETRYABILITY_MANUAL


def test_exception_conversion_classifies_wrapped_known_exception_and_keeps_chain() -> None:
    cause = PermissionError("OPENAI_API_KEY=secret")
    wrapped = RuntimeError("agent startup failed")
    wrapped.__cause__ = cause

    diagnostic = exception_to_diagnostic(
        wrapped, scope=DiagnosticScope.DIAGNOSTIC_SCOPE_INVOCATION, operation="Agent startup"
    )
    assert diagnostic.code == "permission_denied"
    assert diagnostic.summary == "Agent startup was denied"
    assert diagnostic.detail == (
        "RuntimeError: agent startup failed <- PermissionError: OPENAI_API_KEY=[REDACTED]"
    )


def test_exception_conversion_uses_unsuppressed_context_and_bounds_cycles() -> None:
    outer = RuntimeError("outer")
    inner = TimeoutError("inner")
    outer.__context__ = inner
    inner.__context__ = outer
    diagnostic = exception_to_diagnostic(
        outer, scope=DiagnosticScope.DIAGNOSTIC_SCOPE_RUN, operation="Run"
    )
    assert diagnostic.code == "timeout"
    assert diagnostic.detail == "RuntimeError: outer <- TimeoutError: inner"


def test_exception_conversion_maps_known_exception_subclasses() -> None:
    class SandboxPermissionError(PermissionError):
        pass

    diagnostic = exception_to_diagnostic(
        SandboxPermissionError("denied"),
        scope=DiagnosticScope.DIAGNOSTIC_SCOPE_INVOCATION,
        operation="Sandbox setup",
    )
    assert diagnostic.code == "permission_denied"
    assert diagnostic.summary == "Sandbox setup was denied"


def test_diagnostic_redacts_explicit_text_on_construction_and_round_trip() -> None:
    diagnostic = make_diagnostic(
        code="provider_failed",
        summary="OPENAI_API_KEY=summary-secret",
        detail="AWS_SECRET_ACCESS_KEY=detail-secret",
        hint="Use FOO_TOKEN=hint-secret",
        scope=DiagnosticScope.DIAGNOSTIC_SCOPE_REQUEST,
    )
    assert diagnostic.summary == "OPENAI_API_KEY=[REDACTED]"
    assert diagnostic.detail == "AWS_SECRET_ACCESS_KEY=[REDACTED]"
    assert diagnostic.hint == "Use FOO_TOKEN=[REDACTED]"

    restored = codec.loads(common_pb2.Diagnostic, codec.dumps(diagnostic))
    assert restored == diagnostic


def test_failure_factories_keep_legacy_fields_consistent_and_sanitized() -> None:
    error = PermissionError("OPENAI_API_KEY=secret")
    response = error_response("request", error, operation="Codex startup")
    assert response.ok is False
    assert response.HasField("diagnostic")
    assert response.error == response.diagnostic.summary
    assert response.error == "Codex startup was denied"
    assert response.diagnostic.detail == "PermissionError: OPENAI_API_KEY=[REDACTED]"

    server_message = protocol_error(
        error, request_id="request", operation="Event stream", code="stream_failed"
    )
    failure = server_message.protocol_error
    assert failure.HasField("diagnostic")
    assert failure.code == failure.diagnostic.code == "stream_failed"
    assert failure.message == failure.diagnostic.summary
    assert "=secret" not in codec.dumps(server_message)


def test_empty_exception_uses_exception_type_as_user_message() -> None:
    assert exception_detail(RuntimeError()) == "RuntimeError"
    assert (
        exception_to_diagnostic(
            TimeoutError(), scope=DiagnosticScope.DIAGNOSTIC_SCOPE_RUN, operation="Run"
        ).summary
        == "Run timed out"
    )
    assert redact_diagnostic_text("password: hidden") == "password: [REDACTED]"


def test_diagnostic_rejects_unknown_fields() -> None:
    with pytest.raises(codec.WireError):
        codec.from_dict(common_pb2.Diagnostic, {"code": "x", "surprise": 1})
