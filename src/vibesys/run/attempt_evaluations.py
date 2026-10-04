"""Immutable host-side submission cursors for charged implementer attempts.

The run's host fence owns writes. Each cursor is committed before the initial
provider turn, independently of the legacy orchestration snapshot. Kernel
attempt contracts remain authoritative for their own lifecycle state.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, field_validator

from vs_runtime.api import RuntimeContractError

if TYPE_CHECKING:
    from vs_project.api import StateModels


class AttemptEvaluationCursor(BaseModel):
    """A charged turn's immutable position in its workspace's submission history.

    ``preceding_handles`` identifies all evaluations admitted before this attempt.
    A continuation never advances this position. ``invocation_id`` names the
    attempt's initial TURN, and ``workspace_id`` prevents attributing another
    workspace's history to that attempt.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    invocation_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    preceding_handles: tuple[str, ...]

    @property
    def submitted_before(self) -> int:
        """Derive the immutable history position from its authoritative prefix."""
        return len(self.preceding_handles)

    @field_validator("preceding_handles")
    @classmethod
    def valid_prefix(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Require unique, nonblank handles in their original admission order."""
        if len(values) != len(set(values)) or any(
            not value.strip() or value != value.strip() for value in values
        ):
            message = "attempt cursor prefix handles must be unique, nonblank and trimmed"
            raise ValueError(message)
        return values

    @field_validator("invocation_id", "workspace_id")
    @classmethod
    def trimmed_identity(cls, value: str) -> str:
        """Reject ambiguous initial-turn and workspace identities."""
        if not value.strip() or value != value.strip():
            message = "attempt cursor identities must be nonblank and trimmed"
            raise ValueError(message)
        return value


class AttemptEvaluationCursorError(RuntimeContractError):
    """An attempt cursor conflicts with the durable initial-turn identity."""


class AttemptEvaluationCursors:
    """Read and atomically preserve one immutable cursor per initial invocation.

    Absence returns ``None`` for the caller's explicit legacy migration policy.
    Invalid persisted data and conflicting repeated writes remain errors. The
    caller must hold the run's host fence while writing.
    """

    def __init__(self, namespace: StateModels) -> None:
        """Bind a Project-owned namespace or its strict in-memory implementation."""
        self._namespace = namespace

    def read(self, invocation_id: str) -> AttemptEvaluationCursor | None:
        """Load a strictly validated cursor without granting dispatch authority."""
        cursor = self._namespace.load_optional(_cursor_path(invocation_id), AttemptEvaluationCursor)
        if cursor is not None and cursor.invocation_id != invocation_id:
            message = f"attempt cursor identity differs from invocation {invocation_id!r}"
            raise AttemptEvaluationCursorError(message)
        return cursor

    def record(
        self, *, invocation_id: str, workspace_id: str, preceding_handles: tuple[str, ...]
    ) -> AttemptEvaluationCursor:
        """Commit before execution; repeating the exact cursor is idempotent."""
        cursor = AttemptEvaluationCursor(
            invocation_id=invocation_id,
            workspace_id=workspace_id,
            preceding_handles=preceding_handles,
        )
        previous = self.read(invocation_id)
        if previous is not None:
            if previous != cursor:
                message = f"attempt cursor conflicts with invocation {invocation_id!r}"
                raise AttemptEvaluationCursorError(message)
            return previous
        self._namespace.save(_cursor_path(invocation_id), cursor)
        return cursor


def _cursor_path(invocation_id: str) -> str:
    identity = hashlib.sha256(invocation_id.encode()).hexdigest()
    return f"cursors/{identity}.json"


__all__ = [
    "AttemptEvaluationCursor",
    "AttemptEvaluationCursorError",
    "AttemptEvaluationCursors",
]
