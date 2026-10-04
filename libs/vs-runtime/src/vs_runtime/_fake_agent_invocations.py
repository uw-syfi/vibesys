"""Internal durable-invocation implementation for the workspace-session Fake.

The adapter supplies a fixed session identity and dispatch observations. This
pure implementation owns payload fences, ledger merging, and recorded outcomes.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel, ValidationError

from vs_agent.api import (
    AgentInvocationRecord,
    AgentInvocationState,
    AgentOutputSchemaError,
    AgentSessionCheckpoint,
    AgentSessionKey,
    AgentTurnResult,
    Completed,
    InvalidResponse,
    InvocationConflictError,
    Pending,
    SessionConfigurationError,
    SessionPersistenceError,
    SessionResumeError,
    Unknown,
    parse_typed_response,
)
from vs_project.api import ProjectError

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from vs_agent.api import AgentInvocationStore, AgentSessions, InvocationOutcome

__all__ = ["FakeAgentInvocations", "FakeInvocationIdentity"]

ResponseT = TypeVar("ResponseT", bound=BaseModel)


@dataclass(frozen=True)
class FakeInvocationIdentity:
    """Immutable authority and workspace inputs for initial-turn identity."""

    key: AgentSessionKey
    role: str
    workspace: str
    writable_paths: tuple[str, ...]


class FakeAgentInvocations:
    """Journal fake provider turns with the real initial-invocation guarantees."""

    def __init__(
        self,
        identity: FakeInvocationIdentity,
        store: AgentInvocationStore | None,
        transport: AgentSessions | None,
    ) -> None:
        """Own one session's journal over shared durable state and optional transport."""
        self._identity = identity
        self._session_key = identity.key
        self._invocation_store = store
        self._session_transport = transport
        self._active: set[str] = set()

    def _transport(self) -> AgentSessions:
        if self._session_transport is None:
            detail = "durable agent session transport is not configured"
            raise SessionConfigurationError.because(detail)
        return self._session_transport

    def _journal(self) -> AbstractContextManager[AgentInvocationStore | None]:
        """Use the continuation owner's transaction, or the initial-only backing."""
        if self._session_transport is not None:
            return self._session_transport.invocation_transaction()
        return nullcontext(self._invocation_store)

    @staticmethod
    def _save(store: AgentInvocationStore, state: AgentInvocationState) -> None:
        try:
            store.save(state)
        except (ProjectError, OSError, ValidationError) as error:
            detail = f"cannot commit invocation ledger: {error}"
            raise SessionPersistenceError.because(detail) from error

    def checkpoint(self) -> AgentSessionCheckpoint:
        """Read checkpoint identity from the injected agent session interface."""
        with self._journal() as store:
            if self._session_transport is not None:
                return self._session_transport.checkpoint(self._session_key)
            if store is not None:
                state = store.load_optional() or AgentInvocationState()
                records = [
                    record
                    for record in state.invocations.values()
                    if record.outcome.session_key == str(self._session_key)
                    and record.outcome.checkpoint is not None
                ]
                if records:
                    checkpoint = max(records, key=lambda record: record.sequence).outcome.checkpoint
                    if checkpoint is not None:
                        return checkpoint
                raise SessionResumeError(str(self._session_key), "provider checkpoint is missing")
            return self._transport().checkpoint(self._session_key)

    def release_interrupted(self, invocation_id: str, *, active: bool) -> None:
        """Release the original and its one correction only after explicit drain."""
        with self._journal() as store:
            if store is not None:
                state = store.load_optional() or AgentInvocationState()
                identities = (invocation_id, f"{invocation_id}/correction")
                records = [
                    (identity, state.invocations[identity])
                    for identity in identities
                    if identity in state.invocations
                ]
                if records:
                    if active or any(
                        record.outcome.session_key != str(self._session_key)
                        for _, record in records
                    ):
                        detail = "cannot release an active invocation or another key"
                        raise InvocationConflictError.because(detail)
                    for identity, record in records:
                        state.invocations[identity] = record.model_copy(
                            update={"interrupted": True}
                        )
                    try:
                        self._save(store, state)
                    except (ProjectError, OSError, ValidationError) as error:
                        detail = f"cannot release interrupted invocation {invocation_id}: {error}"
                        raise SessionPersistenceError.because(detail) from error
                    return
            if active:
                detail = "cannot release an active invocation"
                raise InvocationConflictError.because(detail)
            if self._session_transport is not None:
                self._session_transport.release_interrupted(self._session_key, invocation_id)

    def inspect(self, invocation_id: str) -> InvocationOutcome:
        """Preserve the owning interface's explicit invocation outcome."""
        with self._journal() as store:
            if store is not None:
                state = store.load_optional() or AgentInvocationState()
                record = state.invocations.get(invocation_id)
                if record is not None:
                    if record.outcome.session_key != str(self._session_key):
                        detail = "invocation belongs to another key"
                        raise InvocationConflictError.because(detail)
                    if isinstance(record.outcome, Pending) and invocation_id not in self._active:
                        if self._session_transport is not None:
                            return self._session_transport.inspect(self._session_key, invocation_id)
                        return Unknown(
                            session_key=str(self._session_key),
                            invocation_id=invocation_id,
                            detail="unfinished initial dispatch recovered",
                        )
                    return record.outcome
            if self._session_transport is None:
                return Unknown(
                    session_key=str(self._session_key),
                    invocation_id=invocation_id,
                    detail="no invocation evidence",
                )
            return self._session_transport.inspect(self._session_key, invocation_id)

    def begin(
        self,
        message: str,
        response_schema: dict[str, Any] | None,
        invocation_id: str,
        *,
        current_checkpoint: AgentSessionCheckpoint | None,
        checkpoint: AgentSessionCheckpoint | None = None,
    ) -> InvocationOutcome | None:
        """Fence a new dispatch or return immutable evidence for replay."""
        with self._journal() as store:
            if not invocation_id or store is None:
                detail = "initial invocation persistence is not configured"
                raise SessionConfigurationError.because(detail)
            state = store.load_optional() or AgentInvocationState()
            digest = hashlib.sha256(
                json.dumps(
                    {
                        "key": str(self._session_key),
                        "role": self._identity.role,
                        "workspace": str(self._identity.workspace),
                        "writable_paths": self._identity.writable_paths,
                        "message": message,
                        "response": response_schema,
                        "checkpoint": None if checkpoint is None else checkpoint.model_dump(),
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            previous = state.invocations.get(invocation_id)
            if previous is not None:
                if previous.payload_digest != digest:
                    detail = "invocation payload changed"
                    raise InvocationConflictError.because(detail)
                return self.inspect(invocation_id)
            self._ensure_session_resolved(state)
            self._validate_checkpoint(state, current_checkpoint)
            state.record(
                AgentInvocationRecord(
                    payload_digest=digest,
                    outcome=Pending(
                        session_key=str(self._session_key),
                        invocation_id=invocation_id,
                        checkpoint=checkpoint or current_checkpoint,
                    ),
                )
            )
            self._save(store, state)
            self._active.add(invocation_id)
            return None

    def _validate_checkpoint(
        self, state: AgentInvocationState, current: AgentSessionCheckpoint | None
    ) -> None:
        records = [
            record
            for record in state.invocations.values()
            if record.outcome.session_key == str(self._session_key) and not record.interrupted
        ]
        if not records:
            return
        latest = max(record.sequence for record in records)
        for record in records:
            if record.sequence != latest:
                continue
            prior = record.outcome.checkpoint
            if prior is None or current is None:
                raise SessionResumeError(
                    str(self._session_key), "acknowledged provider checkpoint is missing"
                )
            if prior != current:
                raise SessionResumeError(
                    str(self._session_key), "provider checkpoint identity changed"
                )

    def _ensure_session_resolved(self, state: AgentInvocationState) -> None:
        for record in state.invocations.values():
            outcome = record.outcome
            if (
                outcome.session_key != str(self._session_key)
                or isinstance(outcome, Completed)
                or record.interrupted
            ):
                continue
            if isinstance(outcome, InvalidResponse) and outcome.checkpoint is not None:
                continue
            if isinstance(outcome, Unknown):
                detail = outcome.detail
            elif isinstance(outcome, Pending):
                detail = "unfinished dispatch recovered without acceptance evidence"
            else:
                detail = "acknowledged provider checkpoint is missing"
            raise SessionResumeError(str(self._session_key), detail)

    @staticmethod
    def replay(outcome: InvocationOutcome, response: type[ResponseT] | None) -> str | ResponseT:
        """Decode accepted immutable evidence or preserve its typed schema failure."""
        if isinstance(outcome, InvalidResponse) and response is not None:
            raise AgentOutputSchemaError(detail=outcome.detail)
        if not isinstance(outcome, Completed):
            detail = (
                outcome.detail
                if isinstance(outcome, Unknown)
                else "initial dispatch has no acknowledgement"
            )
            raise SessionResumeError(outcome.session_key, detail)
        if response is None:
            return outcome.result.text
        return parse_typed_response(outcome.result.text, response)

    def end(self, invocation_id: str) -> None:
        """Release live dispatch ownership without changing durable evidence."""
        self._active.discard(invocation_id)

    def rejected(self, invocation_id: str, detail: str, *, completed: bool) -> None:
        """Persist provider rejection evidence while preserving validated raw replies."""
        with self._journal() as store:
            if store is None:
                return
            state = store.load_optional() or AgentInvocationState()
            previous = state.invocations[invocation_id]
            if isinstance(previous.outcome, Completed):
                return
            checkpoint = previous.outcome.checkpoint
            if checkpoint is None:
                try:
                    checkpoint = self.checkpoint()
                except (SessionResumeError, SessionConfigurationError):
                    # A native schema failure names no new conversation. An
                    # already checkpointed conversation survives it; otherwise
                    # InvalidResponse must retain the absence of identity.
                    checkpoint = None
            if checkpoint is None and completed:
                checkpoint = AgentSessionCheckpoint(
                    session_key=str(self._session_key),
                    provider_session_id=f"fake:{self._session_key}",
                )
            state.record(
                AgentInvocationRecord(
                    payload_digest=previous.payload_digest,
                    outcome=InvalidResponse(
                        session_key=str(self._session_key),
                        invocation_id=invocation_id,
                        detail=detail,
                        checkpoint=checkpoint,
                    ),
                )
            )
            self._save(store, state)

    def failed(self, invocation_id: str, error: BaseException) -> None:
        """Keep an ambiguous boundary failure's real cause without authorizing replay."""
        with self._journal() as store:
            if store is None:
                return
            state = store.load_optional() or AgentInvocationState()
            previous = state.invocations[invocation_id]
            if isinstance(previous.outcome, Completed):
                return
            state.record(
                AgentInvocationRecord(
                    payload_digest=previous.payload_digest,
                    outcome=Unknown(
                        session_key=str(self._session_key),
                        invocation_id=invocation_id,
                        detail=f"{type(error).__name__}: {error}",
                        checkpoint=previous.outcome.checkpoint,
                    ),
                )
            )
            self._save(store, state)

    def accepted(self, invocation_id: str, text: str) -> None:
        """Merge accepted provider output with the latest shared invocation ledger."""
        with self._journal() as store:
            if store is None:
                detail = "initial invocation persistence is not configured"
                raise SessionConfigurationError.because(detail)
            checkpoint = (
                self._session_transport.checkpoint(self._session_key)
                if self._session_transport is not None
                else AgentSessionCheckpoint(
                    session_key=str(self._session_key),
                    provider_session_id=f"fake:{self._session_key}",
                )
            )
            state = store.load_optional() or AgentInvocationState()
            prior = state.invocations[invocation_id].outcome.checkpoint
            if prior is not None and prior != checkpoint:
                raise SessionResumeError(
                    str(self._session_key), "provider checkpoint identity changed"
                )
            completed = Completed(
                session_key=str(self._session_key),
                invocation_id=invocation_id,
                checkpoint=checkpoint,
                result=AgentTurnResult(
                    text=text, provider_session_id=checkpoint.provider_session_id
                ),
            )
            state.record(
                AgentInvocationRecord(
                    payload_digest=state.invocations[invocation_id].payload_digest,
                    outcome=completed,
                )
            )
            self._save(store, state)
