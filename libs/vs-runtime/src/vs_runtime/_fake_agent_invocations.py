"""Internal durable-invocation implementation for the workspace-session Fake.

The adapter supplies a fixed session identity and dispatch observations. This
pure implementation owns payload fences, ledger merging, and recorded outcomes.
"""

from __future__ import annotations

import hashlib
import json
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
    SessionResumeError,
    Unknown,
    describe_validation_error,
)

if TYPE_CHECKING:
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
        self._schema_rejections: set[str] = set()
        self._active: set[str] = set()

    def _transport(self) -> AgentSessions:
        if self._session_transport is None:
            detail = "durable agent session transport is not configured"
            raise SessionConfigurationError.because(detail)
        return self._session_transport

    def checkpoint(self) -> AgentSessionCheckpoint:
        """Read checkpoint identity from the injected agent session interface."""
        if self._session_transport is not None:
            return self._session_transport.checkpoint(self._session_key)
        if self._invocation_store is not None:
            state = self._invocation_store.load_optional() or AgentInvocationState()
            for record in reversed(tuple(state.invocations.values())):
                if record.outcome.session_key == str(self._session_key) and isinstance(
                    record.outcome, Completed
                ):
                    return record.outcome.checkpoint
        return self._transport().checkpoint(self._session_key)

    def release_interrupted(self, invocation_id: str, *, active: bool) -> None:
        """Release the original and its one correction only after explicit drain."""
        if self._invocation_store is not None:
            state = self._invocation_store.load_optional() or AgentInvocationState()
            identities = (invocation_id, f"{invocation_id}/correction")
            records = [
                (identity, state.invocations[identity])
                for identity in identities
                if identity in state.invocations
            ]
            if records:
                if active or any(
                    record.outcome.session_key != str(self._session_key) for _, record in records
                ):
                    detail = "cannot release an active invocation or another key"
                    raise InvocationConflictError.because(detail)
                for identity, record in records:
                    state.invocations[identity] = record.model_copy(update={"interrupted": True})
                self._invocation_store.save(state)
                return
        if active:
            detail = "cannot release an active invocation"
            raise InvocationConflictError.because(detail)
        if self._session_transport is not None:
            self._session_transport.release_interrupted(self._session_key, invocation_id)

    def inspect(self, invocation_id: str) -> InvocationOutcome:
        """Preserve the owning interface's explicit invocation outcome."""
        if self._invocation_store is not None:
            state = self._invocation_store.load_optional() or AgentInvocationState()
            record = state.invocations.get(invocation_id)
            if record is not None:
                if record.outcome.session_key != str(self._session_key):
                    detail = "invocation belongs to another key"
                    raise InvocationConflictError.because(detail)
                if isinstance(record.outcome, Pending) and invocation_id not in self._active:
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
    ) -> InvocationOutcome | None:
        """Fence a new dispatch or return immutable evidence for replay."""
        if not invocation_id or self._invocation_store is None:
            detail = "initial invocation persistence is not configured"
            raise SessionConfigurationError.because(detail)
        state = self._invocation_store.load_optional() or AgentInvocationState()
        digest = hashlib.sha256(
            json.dumps(
                {
                    "key": str(self._session_key),
                    "role": self._identity.role,
                    "workspace": str(self._identity.workspace),
                    "writable_paths": self._identity.writable_paths,
                    "message": message,
                    "response": response_schema,
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
        if any(
            record.outcome.session_key == str(self._session_key)
            and not isinstance(record.outcome, Completed)
            and not record.interrupted
            and not (
                isinstance(record.outcome, InvalidResponse)
                and (
                    record.outcome.checkpoint is not None
                    or record.outcome.invocation_id in self._schema_rejections
                )
            )
            for record in state.invocations.values()
        ):
            detail = "session has unresolved invocation"
            raise InvocationConflictError.because(detail)
        state.invocations[invocation_id] = AgentInvocationRecord(
            payload_digest=digest,
            outcome=Pending(session_key=str(self._session_key), invocation_id=invocation_id),
        )
        self._invocation_store.save(state)
        self._active.add(invocation_id)
        return None

    @staticmethod
    def replay(outcome: InvocationOutcome, response: type[ResponseT] | None) -> str | ResponseT:
        """Decode accepted immutable evidence or preserve its typed schema failure."""
        if isinstance(outcome, InvalidResponse) and response is not None:
            raise AgentOutputSchemaError(detail=outcome.detail)
        if not isinstance(outcome, Completed):
            raise SessionResumeError(outcome.session_key, "initial invocation is unresolved")
        if response is None:
            return outcome.result.text
        try:
            return response.model_validate_json(outcome.result.text)
        except ValidationError as error:
            raise AgentOutputSchemaError(detail=describe_validation_error(error)) from error

    def end(self, invocation_id: str) -> None:
        """Release live dispatch ownership without changing durable evidence."""
        self._active.discard(invocation_id)

    def rejected(self, invocation_id: str, detail: str) -> None:
        """Persist provider rejection evidence while preserving validated raw replies."""
        if self._invocation_store is None:
            return
        state = self._invocation_store.load_optional() or AgentInvocationState()
        previous = state.invocations[invocation_id]
        if isinstance(previous.outcome, Completed):
            return
        self._schema_rejections.add(invocation_id)
        checkpoint = (
            self._session_transport.checkpoint(self._session_key)
            if self._session_transport is not None
            else None
        )
        state.invocations[invocation_id] = AgentInvocationRecord(
            payload_digest=previous.payload_digest,
            outcome=InvalidResponse(
                session_key=str(self._session_key),
                invocation_id=invocation_id,
                detail=detail,
                checkpoint=checkpoint,
            ),
        )
        self._invocation_store.save(state)

    def accepted(self, invocation_id: str, text: str) -> None:
        """Merge accepted provider output with the latest shared invocation ledger."""
        if self._invocation_store is None:
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
        completed = Completed(
            session_key=str(self._session_key),
            invocation_id=invocation_id,
            checkpoint=checkpoint,
            result=AgentTurnResult(text=text, provider_session_id=checkpoint.provider_session_id),
        )
        state = self._invocation_store.load_optional() or AgentInvocationState()
        state.invocations[invocation_id] = AgentInvocationRecord(
            payload_digest=state.invocations[invocation_id].payload_digest,
            outcome=completed,
        )
        self._invocation_store.save(state)
