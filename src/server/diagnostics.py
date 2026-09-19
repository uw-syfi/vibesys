"""Presentation-neutral diagnostics carried on the wire as ``Diagnostic`` messages."""

from __future__ import annotations

import re
import uuid
from typing import TYPE_CHECKING

from server.wire.v2 import common_pb2

if TYPE_CHECKING:
    from server.wire.v2.common_pb2 import Diagnostic

DiagnosticScope = common_pb2.DiagnosticScope
DiagnosticSeverity = common_pb2.DiagnosticSeverity
DiagnosticRetryability = common_pb2.DiagnosticRetryability
"""Proto enums, re-exported so callers write ``DiagnosticScope.DIAGNOSTIC_SCOPE_RUN``."""


def make_diagnostic(  # noqa: PLR0913  # independent contract dimensions
    *,
    code: str,
    summary: str,
    scope: DiagnosticScope,
    severity: DiagnosticSeverity = DiagnosticSeverity.DIAGNOSTIC_SEVERITY_ERROR,
    retryability: DiagnosticRetryability = (DiagnosticRetryability.DIAGNOSTIC_RETRYABILITY_UNKNOWN),
    detail: str | None = None,
    hint: str | None = None,
    cause_id: str | None = None,
    debug_ref: str | None = None,
    source: str | None = None,
    id: str | None = None,  # noqa: A002  # the wire field is named ``id``
) -> Diagnostic:
    """Build a diagnostic with a fresh id and credential redaction applied.

    ``summary``, ``detail``, and ``hint`` are redacted here, at construction,
    which is where the Pydantic model applied the same rule.
    """
    diagnostic = common_pb2.Diagnostic(
        id=id or uuid.uuid4().hex,
        code=code,
        summary=redact_diagnostic_text(summary),
        scope=scope,
        severity=severity,
        retryability=retryability,
    )
    if detail is not None:
        diagnostic.detail = redact_diagnostic_text(detail)
    if hint is not None:
        diagnostic.hint = redact_diagnostic_text(hint)
    if cause_id is not None:
        diagnostic.cause_id = cause_id
    if debug_ref is not None:
        diagnostic.debug_ref = debug_ref
    if source is not None:
        diagnostic.source = source
    return diagnostic


_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:[A-Z][A-Z0-9_]*(?:TOKEN|PASSWORD|SECRET|KEY)|"
    r"api[_-]?key|auth[_-]?token|password|secret|token)\b\s*[:=]\s*)"
    r"([^\s,;]+)"
)
_BEARER_TOKEN = re.compile(r"(?i)(\bbearer\s+)[^\s,;]+")


def redact_diagnostic_text(text: str) -> str:
    """Redact common credential assignments before they cross the UI boundary."""
    text = _SECRET_ASSIGNMENT.sub(r"\1[REDACTED]", text)
    return _BEARER_TOKEN.sub(r"\1[REDACTED]", text)


def exception_detail(error: BaseException) -> str:
    """Return sanitized technical detail while retaining exception identity."""
    return " <- ".join(_exception_fragment(item) for item in _exception_chain(error))


def exception_summary(error: BaseException, operation: str = "Operation") -> str:
    """Map common exceptions to concise, high-level user-facing summaries."""
    error = _classified_exception(error)
    for exception_type, summary in (
        (PermissionError, f"{operation} was denied"),
        (FileNotFoundError, f"{operation} could not find a required file"),
        (TimeoutError, f"{operation} timed out"),
        (ValueError, f"{operation} received invalid input"),
    ):
        if isinstance(error, exception_type):
            return summary
    return f"{operation} failed"


def exception_to_diagnostic(  # noqa: PLR0913  # independent contract dimensions
    error: BaseException,
    *,
    scope: DiagnosticScope,
    operation: str = "Operation",
    summary: str | None = None,
    code: str | None = None,
    hint: str | None = None,
    severity: DiagnosticSeverity = DiagnosticSeverity.DIAGNOSTIC_SEVERITY_ERROR,
    retryability: DiagnosticRetryability = (DiagnosticRetryability.DIAGNOSTIC_RETRYABILITY_UNKNOWN),
    cause_id: str | None = None,
    debug_ref: str | None = None,
) -> Diagnostic:
    """Map an exception to the canonical diagnostic contract."""
    return make_diagnostic(
        code=code or _default_code(error),
        summary=summary or exception_summary(error, operation),
        detail=exception_detail(error),
        hint=hint,
        scope=scope,
        severity=severity,
        retryability=retryability,
        cause_id=cause_id,
        debug_ref=debug_ref,
    )


def _default_code(error: BaseException) -> str:
    """Provide a deterministic fallback code without exposing exception text."""
    error = _classified_exception(error)
    for exception_type, code in (
        (PermissionError, "permission_denied"),
        (FileNotFoundError, "not_found"),
        (TimeoutError, "timeout"),
        (ValueError, "invalid_value"),
    ):
        if isinstance(error, exception_type):
            return code
    return "operation_failed"


_KNOWN_EXCEPTIONS = (PermissionError, FileNotFoundError, TimeoutError, ValueError)
_MAX_EXCEPTION_CHAIN = 8


def _exception_chain(error: BaseException) -> list[BaseException]:
    """Follow explicit causes, then unsuppressed contexts, without looping."""
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and len(chain) < _MAX_EXCEPTION_CHAIN:
        marker = id(current)
        if marker in seen:
            break
        seen.add(marker)
        chain.append(current)
        cause = current.__cause__
        if cause is not None:
            current = cause
        elif not current.__suppress_context__:
            current = current.__context__
        else:
            current = None
    return chain


def _classified_exception(error: BaseException) -> BaseException:
    """Prefer the first known underlying error when wrappers obscure it."""
    chain = _exception_chain(error)
    for item in chain:
        if isinstance(item, _KNOWN_EXCEPTIONS):
            return item
    return error


def _exception_fragment(error: BaseException) -> str:
    text = redact_diagnostic_text(str(error).strip())
    return f"{type(error).__name__}: {text}" if text else type(error).__name__
