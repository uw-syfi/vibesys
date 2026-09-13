"""Presentation-neutral diagnostics shared by server protocol models."""

from __future__ import annotations

import re
import uuid
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DiagnosticScope(StrEnum):
    """Boundary at which a diagnostic was raised."""

    CONFIGURATION = "configuration"
    INVOCATION = "invocation"
    PHASE = "phase"
    RUN = "run"
    REQUEST = "request"
    PROTOCOL = "protocol"
    TRANSPORT = "transport"


class DiagnosticSeverity(StrEnum):
    """Operator-visible seriousness of a diagnostic."""

    WARNING = "warning"
    ERROR = "error"
    FATAL = "fatal"


class DiagnosticRetryability(StrEnum):
    """Whether retrying the failed operation is expected to help."""

    AUTOMATIC = "automatic"
    MANUAL = "manual"
    NEVER = "never"
    UNKNOWN = "unknown"


class Diagnostic(BaseModel):
    """Structured, provider-neutral description of an operator diagnostic.

    Frozen for the same reason as ``RunEvent``: diagnostics ride along on
    replayed events, which readers share rather than copy.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    code: str
    summary: str
    detail: str | None = None
    hint: str | None = None
    scope: DiagnosticScope
    severity: DiagnosticSeverity = DiagnosticSeverity.ERROR
    retryability: DiagnosticRetryability = DiagnosticRetryability.UNKNOWN
    cause_id: str | None = None
    debug_ref: str | None = None
    # Which subsystem raised the diagnostic (e.g. "git_tracking", "skills").
    # Optional and additive: events recorded before the field existed omit it.
    source: str | None = None

    @field_validator("summary", "detail", "hint", mode="before")
    @classmethod
    def _redact_text(cls, value: object) -> object:
        if isinstance(value, str):
            return redact_diagnostic_text(value)
        return value


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
    scope: DiagnosticScope
    | Literal[
        "configuration",
        "invocation",
        "phase",
        "run",
        "request",
        "protocol",
        "transport",
    ],
    operation: str = "Operation",
    summary: str | None = None,
    code: str | None = None,
    hint: str | None = None,
    severity: DiagnosticSeverity = DiagnosticSeverity.ERROR,
    retryability: DiagnosticRetryability = DiagnosticRetryability.UNKNOWN,
    cause_id: str | None = None,
    debug_ref: str | None = None,
) -> Diagnostic:
    """Map an exception to the canonical diagnostic contract."""
    return Diagnostic(
        code=code or _default_code(error),
        summary=summary or exception_summary(error, operation),
        detail=exception_detail(error),
        hint=hint,
        scope=DiagnosticScope(scope),
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
