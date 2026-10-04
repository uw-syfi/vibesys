"""Typed failures of durable conversation continuation."""

from __future__ import annotations


class SessionConfigurationError(ValueError):
    """The configured provider cannot honor durable session continuation."""

    @classmethod
    def because(cls, detail: str) -> SessionConfigurationError:
        """Create an actionable configuration diagnostic."""
        return cls(detail)


class SessionResumeError(RuntimeError):
    """Continuation was refused before dispatch or lost its conversation identity."""

    def __init__(self, session_key: str, detail: str) -> None:
        """Preserve the refused key and its boundary diagnostic."""
        self.session_key = session_key
        self.detail = detail
        super().__init__(f"cannot resume {session_key}: {detail}")


class InvocationConflictError(ValueError):
    """An invocation identity was reused with a different immutable payload."""

    @classmethod
    def because(cls, detail: str) -> InvocationConflictError:
        """Name the conflicting invocation payload or session."""
        return cls(detail)


class SessionPersistenceError(RuntimeError):
    """The invocation ledger could not be read or committed; dispatch is unsafe."""

    @classmethod
    def because(cls, detail: str) -> SessionPersistenceError:
        """Preserve the failed persistence operation."""
        return cls(detail)
