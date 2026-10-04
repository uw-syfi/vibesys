"""Durable release intent for evaluation workspace scopes.

Ownership is committed before dispatch. Closing fences admission and remains
replayable until every resource is observed terminal. No cleanup acknowledgement
can erase the intent until its caller commits completion.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from vs_evaluation.state_namespace import EvaluationStateNamespace

_PATH = "agent-evaluation-released-scopes.json"
_SCHEMA_VERSION = 2


class ScopePhase(StrEnum):
    """A scope is absent while Open; persisted release phases fence admission."""

    CLOSING = "closing"
    CLOSED = "closed"


class ScopeIntent(BaseModel):
    """One scope generation's durable release intent and completion."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    scope_id: str = Field(min_length=1)
    generation: int = Field(default=0, ge=0)
    phase: ScopePhase | None = None
    reconcile_legacy_captures: bool = False


class ScopeState(BaseModel):
    """Versioned successor of the PR's released-scope marker file."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[2] = 2
    scopes: tuple[ScopeIntent, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def migrate(cls, value: object) -> object:
        """Legacy release markers prove closure, never completed cleanup."""
        if not isinstance(value, dict):
            return value
        if value.get("schema_version") == 1 and not set(value).difference(
            {"schema_version", "scope_ids"}
        ):
            return {
                "schema_version": 2,
                "scopes": tuple(
                    ScopeIntent(
                        scope_id=scope_id, phase=ScopePhase.CLOSING, reconcile_legacy_captures=True
                    )
                    for scope_id in value.get("scope_ids", ())
                ),
            }
        if value.get("schema_version") == _SCHEMA_VERSION and isinstance(value.get("scopes"), list):
            # A model migration receives Python values even from strict JSON
            # loading. Validate children as JSON to preserve strict enum/tuple
            # semantics, instead of loosening the persisted contract.
            return {
                **value,
                "scopes": tuple(
                    ScopeIntent.model_validate_json(json.dumps(scope), strict=True)
                    for scope in value["scopes"]
                ),
            }
        return value

    @model_validator(mode="after")
    def unique(self) -> ScopeState:
        """Reject ambiguous scope generations or duplicate resource ownership."""
        ids = [scope.scope_id for scope in self.scopes]
        if len(ids) != len(set(ids)):
            message = "scope IDs must be unique"
            raise ValueError(message)
        return self


class ScopeClosingError(RuntimeError):
    """Admission or reopening attempted while resources remain unreconciled."""

    def __init__(self, scope_id: str) -> None:
        """Name the scope whose cleanup remains unfinished."""
        super().__init__(f"evaluation scope {scope_id!r} is closing")


class ScopeLifecycleStore:
    """Project-backed scope ledger shared by dispatch and release handlers.

    Methods commit synchronously, so the single-host shell cannot interleave a
    load and save. The existing namespace owns the layout and atomic write.
    """

    def __init__(self, namespace: EvaluationStateNamespace) -> None:
        """Bind the existing release-state namespace."""
        self._namespace = namespace

    def snapshot(self) -> ScopeState:
        """Read and validate all intents, migrating legacy markers as Closing."""
        return self._namespace.load_optional(_PATH, ScopeState) or ScopeState()

    def released(self, scope_id: str) -> bool:
        """Return whether admission is fenced for this scope generation."""
        return any(
            scope.scope_id == scope_id and scope.phase is not None
            for scope in self.snapshot().scopes
        )

    def begin(self, scope_id: str) -> tuple[ScopeIntent, bool]:
        """Commit Closing before cancellation; retry keeps the same generation."""
        state = self.snapshot()
        current = next(
            (scope for scope in state.scopes if scope.scope_id == scope_id),
            ScopeIntent(scope_id=scope_id),
        )
        first = current.phase is None
        if current.phase is ScopePhase.CLOSED:
            return current, False
        updated = current.model_copy(update={"phase": ScopePhase.CLOSING})
        self._save(state, updated)
        return updated, first

    def complete(self, scope_id: str) -> None:
        """Acknowledge observed terminal resources in one durable state write."""
        state = self.snapshot()
        current = next(scope for scope in state.scopes if scope.scope_id == scope_id)
        self._save(state, current.model_copy(update={"phase": ScopePhase.CLOSED}))

    def reopen(self, scope_id: str) -> None:
        """Open a fresh generation only after cleanup has been acknowledged."""
        state = self.snapshot()
        current = next((scope for scope in state.scopes if scope.scope_id == scope_id), None)
        if current is None or current.phase is None:
            return
        if current.phase is ScopePhase.CLOSING:
            raise ScopeClosingError(scope_id)
        self._save(state, ScopeIntent(scope_id=scope_id, generation=current.generation + 1))

    def _save(self, state: ScopeState, current: ScopeIntent) -> None:
        scopes = tuple(scope for scope in state.scopes if scope.scope_id != current.scope_id)
        self._namespace.save(_PATH, ScopeState(scopes=(*scopes, current)))


class EvaluationAdmissionStoppedError(RuntimeError):
    """Submission attempted after the process stop admission fence."""


class ScopeSubmissionTracker:
    """Drain process-local submissions after durable closure fences new admission.

    Completion tokens are only a live-handler join. Claimed requests remain
    the durable resource owner when a process or caller disappears.
    """

    def __init__(self) -> None:
        """Start without dispatched handlers."""
        self._active: dict[str | None, set[asyncio.Future[None]]] = {}
        self._stopped = False

    @asynccontextmanager
    async def track(self, scope_id: str | None) -> AsyncIterator[None]:
        """Join every exit path of a submission that was admitted before closure."""
        self.check_admission()
        completion = asyncio.get_running_loop().create_future()
        active = self._active.setdefault(scope_id, set())
        active.add(completion)
        try:
            yield
        finally:
            active.remove(completion)
            completion.set_result(None)

    def check_admission(self) -> None:
        """Reject external dispatch after the global stop fence."""
        if self._stopped:
            raise EvaluationAdmissionStoppedError

    async def drain(self, scope_id: str | None) -> None:
        """Wait for the fenced scope's admitted submits to expose durable ownership."""
        if scope_id is None:
            self._stopped = True
            active = tuple(done for scope in self._active.values() for done in scope)
        else:
            active = tuple(self._active.get(scope_id, ()))
        await asyncio.gather(*(asyncio.shield(done) for done in active))


__all__ = [
    "EvaluationAdmissionStoppedError",
    "ScopeClosingError",
    "ScopeIntent",
    "ScopeLifecycleStore",
    "ScopePhase",
    "ScopeState",
    "ScopeSubmissionTracker",
]
