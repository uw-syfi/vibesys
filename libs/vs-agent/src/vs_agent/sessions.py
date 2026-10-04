"""Durable dispatch guards for key-based agent continuations.

The ledger promises one logical dispatch. A persisted unfinished invocation is
Unknown after reconstruction, because provider acceptance cannot be inspected
through today's agentshim API. Such an invocation is never blindly replayed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from threading import RLock
from typing import TYPE_CHECKING, Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from vs_agent.contracts import (
    AgentOutputSchemaError,
    AgentSessionSpec,
    AgentTurnRequest,
    AgentTurnResult,
    SessionDisposition,
    session_spec_fingerprint,
)
from vs_agent.session_errors import (
    InvocationConflictError,
    SessionConfigurationError,
    SessionPersistenceError,
    SessionResumeError,
)
from vs_agent.session_key import AgentSessionKey
from vs_project.api import ProjectError
from vs_prompts.api import RenderedPrompt

if TYPE_CHECKING:
    from vs_agent.client import AgentClient


class _SessionObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    session_key: str

    @field_validator("session_key")
    @classmethod
    def _durable_key(cls, value: str) -> str:
        if not AgentSessionKey.parse(value).durable:
            message = f"session_key {value!r} is not durable"
            raise ValueError(message)
        return value


class AgentSessionCheckpoint(_SessionObservation):
    """Identity of the conversation a key continues, suitable for durable intent."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    session_key: str
    provider_session_id: str = Field(min_length=1)


class _InvocationObservation(_SessionObservation):
    invocation_id: str = Field(min_length=1)
    checkpoint: AgentSessionCheckpoint | None = None

    @model_validator(mode="after")
    def _checkpoint_key(self) -> _InvocationObservation:
        if self.checkpoint is not None and self.checkpoint.session_key != self.session_key:
            message = "checkpoint session_key must match invocation session_key"
            raise ValueError(message)
        return self


class Completed(_InvocationObservation):
    """A validated same-conversation turn result was durably recorded."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["completed"] = "completed"
    session_key: str
    invocation_id: str = Field(min_length=1)
    result: AgentTurnResult
    checkpoint: AgentSessionCheckpoint

    @model_validator(mode="after")
    def _same_conversation(self) -> Completed:
        if (
            self.session_key != self.checkpoint.session_key
            or self.result.provider_session_id != self.checkpoint.provider_session_id
            or self.result.disposition is not SessionDisposition.REUSABLE
        ):
            message = "completed invocation must preserve its checkpoint conversation"
            raise ValueError(message)
        return self


class Pending(_InvocationObservation):
    """The current implementation instance owns the in-flight dispatch."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["pending"] = "pending"
    session_key: str
    invocation_id: str = Field(min_length=1)
    checkpoint: AgentSessionCheckpoint | None = None


class Unknown(_InvocationObservation):
    """Acceptance or completion is ambiguous; inspection never authorizes replay."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["unknown"] = "unknown"
    session_key: str
    invocation_id: str = Field(min_length=1)
    detail: str
    checkpoint: AgentSessionCheckpoint | None = None


class InvalidResponse(_InvocationObservation):
    """The provider reported a completed schema rejection, retaining conversation."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["invalid_response"] = "invalid_response"
    detail: str


type InvocationOutcome = Annotated[
    Completed | Pending | Unknown | InvalidResponse, Field(discriminator="kind")
]


class AgentSessions(Protocol):
    """Resume durable keyed conversations without replacing their history.

    Repeated invocation identities return their recorded outcome; a changed
    payload raises InvocationConflictError. Unknown never triggers dispatch.
    Configuration, missing checkpoints and persistence failures are typed.
    """

    def start(
        self, key: AgentSessionKey, spec: AgentSessionSpec, turn: AgentTurnRequest
    ) -> InvocationOutcome:
        """Journal a keyed initial turn, replaying only durable completion evidence."""
        ...

    def release_interrupted(self, key: AgentSessionKey, invocation_id: str) -> None:
        """Release key ownership after the caller drains an explicit interruption."""
        ...

    def checkpoint(self, key: AgentSessionKey) -> AgentSessionCheckpoint:
        """Return conversation identity or raise SessionResumeError."""
        ...

    def resume(
        self, key: AgentSessionKey, message: RenderedPrompt, invocation_id: str
    ) -> InvocationOutcome:
        """Dispatch once, continuing the checkpointed conversation."""
        ...

    def inspect(self, key: AgentSessionKey, invocation_id: str) -> InvocationOutcome:
        """Inspect known evidence; missing and recovered unfinished work is Unknown."""
        ...


class AgentInvocationRecord(BaseModel):
    """Payload fence and observed outcome for one stable invocation identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    payload_digest: str
    outcome: InvocationOutcome
    interrupted: bool = False


class AgentInvocationState(BaseModel):
    """Strict machine-local invocation ledger, stored through Project's StateSlot."""

    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    invocations: dict[str, AgentInvocationRecord] = Field(default_factory=dict)

    @field_validator("invocations")
    @classmethod
    def _matching_identities(
        cls, records: dict[str, AgentInvocationRecord]
    ) -> dict[str, AgentInvocationRecord]:
        for identity, record in records.items():
            if identity != record.outcome.invocation_id:
                message = f"invocations.{identity} does not match outcome invocation_id"
                raise ValueError(message)
        return records


def _payload_digest(
    key: AgentSessionKey, spec: AgentSessionSpec, turn: AgentTurnRequest, message: str
) -> str:
    payload = {
        "session_key": str(key),
        "spec_fingerprint": session_spec_fingerprint(spec),
        "message": str(message),
        "instructions": turn.instructions,
        "output_schema": None
        if turn.output_schema is None
        else turn.output_schema.model_json_schema(),
        "timeout_s": None if turn.timeout is None else turn.timeout.total_seconds(),
        "label": turn.label,
        "expected_provider_session_id": turn.expected_provider_session_id,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class AgentInvocationStore(Protocol):
    """Strict invocation persistence, fulfilled by Project's StateSlot and the Fake.

    A missing slot returns None. Failed loads/commits raise ProjectError,
    OSError or ValidationError; implementations never silently convert corrupt state to absence.
    The owner must serialize access and provide exclusive host ownership.
    """

    def load_optional(self) -> AgentInvocationState | None:
        """Load and validate the full ledger, or report a missing slot."""
        ...

    def save(self, model: AgentInvocationState) -> None:
        """Atomically persist the validated full ledger, or raise."""
        ...


class ClientAgentSessions:
    """Continue AgentClient sessions with a persisted write-ahead dispatch guard.

    One instance owns its slot for the duration of a run; the shell supplies
    exclusive host ownership. The slot must be machine-local, alongside the
    provider checkpoints, and must survive reconstruction of this instance.
    """

    def __init__(self, client: AgentClient, slot: AgentInvocationStore) -> None:
        """Preflight durable-resume capability and bind the exclusively owned ledger."""
        if not client.capabilities.provider_session_resume:
            message = "provider_session_resume is required for AgentSessions"
            raise SessionConfigurationError.because(message)
        self._client = client
        self._slot = slot
        self._bindings: dict[AgentSessionKey, tuple[AgentSessionSpec, AgentTurnRequest]] = {}
        self._active: set[str] = set()
        self._schema_rejections: set[str] = set()
        self._active_keys: set[AgentSessionKey] = set()
        self._lock = RLock()

    def bind(self, key: AgentSessionKey, spec: AgentSessionSpec, turn: AgentTurnRequest) -> None:
        """Install immutable dispatch configuration; no workspace/role policy lives here."""
        if not key.durable:
            detail = f"session key {key} is not durable"
            raise SessionConfigurationError.because(detail)
        with self._lock:
            old = self._bindings.get(key)
            if old is not None and old != (spec, turn):
                detail = f"session key {key} is already bound"
                raise SessionConfigurationError.because(detail)
            self._bindings[key] = (spec, turn)

    def checkpoint(self, key: AgentSessionKey) -> AgentSessionCheckpoint:
        """Report an existing durable conversation, refusing missing checkpoints."""
        if not key.durable:
            detail = f"session key {key} is not durable"
            raise SessionConfigurationError.because(detail)
        identity = self._client.provider_session_id(key)
        if identity is None:
            raise SessionResumeError(str(key), "provider checkpoint is missing")
        return AgentSessionCheckpoint(session_key=str(key), provider_session_id=identity)

    def release_interrupted(self, key: AgentSessionKey, invocation_id: str) -> None:
        """Release the key only after its owner has drained an explicit interruption.

        The interrupted invocation remains recorded and is never replayed. A
        restarted ambiguous dispatch does not itself authorize this operation.
        """
        self._validate_invocation(key, invocation_id)
        with self._lock:
            if {invocation_id, f"{invocation_id}/correction"} & self._active:
                detail = "cannot release an active invocation"
                raise InvocationConflictError.because(detail)
            state = self._load()
            record = state.invocations.get(invocation_id)
            if record is None:
                return
            if record.outcome.session_key != str(key):
                detail = "interrupted invocation belongs to another key"
                raise InvocationConflictError.because(detail)
            for identity in (invocation_id, f"{invocation_id}/correction"):
                recorded = state.invocations.get(identity)
                if recorded is not None:
                    if recorded.outcome.session_key != str(key):
                        detail = "interrupted correction belongs to another key"
                        raise InvocationConflictError.because(detail)
                    state.invocations[identity] = recorded.model_copy(update={"interrupted": True})
            try:
                self._slot.save(state)
            except (ProjectError, OSError, ValidationError) as error:
                detail = f"cannot release interrupted invocation {invocation_id}: {error}"
                raise SessionPersistenceError.because(detail) from error

    def inspect(self, key: AgentSessionKey, invocation_id: str) -> InvocationOutcome:
        """Read acknowledgement evidence without submitting another turn."""
        self._validate_invocation(key, invocation_id)
        with self._lock:
            record = self._load().invocations.get(invocation_id)
            if record is None:
                return Unknown(
                    session_key=str(key),
                    invocation_id=invocation_id,
                    detail="no invocation evidence",
                )
            if record.outcome.session_key != str(key):
                detail = f"invocation {invocation_id} belongs to another key"
                raise InvocationConflictError.because(detail)
            if isinstance(record.outcome, Pending) and invocation_id not in self._active:
                return Unknown(
                    session_key=str(key),
                    invocation_id=invocation_id,
                    detail="unfinished dispatch recovered without acceptance evidence",
                    checkpoint=record.outcome.checkpoint,
                )
            return record.outcome

    def resume(
        self, key: AgentSessionKey, message: RenderedPrompt, invocation_id: str
    ) -> InvocationOutcome:
        """Persist dispatch intent before continuing exactly the bound conversation."""
        if not isinstance(message, RenderedPrompt):
            detail = "resume message must be a RenderedPrompt"
            raise SessionConfigurationError.because(detail)
        spec, template = self._binding(key)
        return self._dispatch(
            key,
            spec,
            replace(template, message=message, invocation_id=invocation_id),
            initial=False,
        )

    def start(
        self, key: AgentSessionKey, spec: AgentSessionSpec, turn: AgentTurnRequest
    ) -> InvocationOutcome:
        """Journal an initial turn before dispatch and replay its recorded reply.

        A recovered unfinished dispatch stays Unknown: provider acceptance is
        unavailable. A completed reply survives a crash in its consumer.
        """
        if turn.expected_provider_session_id is not None:
            detail = "initial turn must not require an existing provider conversation"
            raise SessionConfigurationError.because(detail)
        return self._dispatch(key, spec, turn, initial=True)

    def _dispatch(
        self,
        key: AgentSessionKey,
        spec: AgentSessionSpec,
        template: AgentTurnRequest,
        *,
        initial: bool,
    ) -> InvocationOutcome:
        invocation_id = template.invocation_id or ""
        message = template.message
        self._validate_invocation(key, invocation_id)
        with self._lock:
            digest = _payload_digest(key, spec, template, message)
            state = self._load()
            previous = state.invocations.get(invocation_id)
            if previous is not None:
                if previous.payload_digest != digest:
                    detail = f"invocation {invocation_id} payload changed"
                    raise InvocationConflictError.because(detail)
                return self.inspect(key, invocation_id)
            if key in self._active_keys:
                detail = f"session {key} already has an active invocation"
                raise InvocationConflictError.because(detail)
            self._ensure_session_resolved(state, key)
            checkpoint = None if initial else self.checkpoint(key)
            expected = template.expected_provider_session_id
            if (
                checkpoint is not None
                and expected is not None
                and checkpoint.provider_session_id != expected
            ):
                raise SessionResumeError(str(key), "bound checkpoint identity changed")
            pending = Pending(
                session_key=str(key), invocation_id=invocation_id, checkpoint=checkpoint
            )
            self._record(state, invocation_id, digest, pending)
            self._active.add(invocation_id)
            self._active_keys.add(key)
        outcome: InvocationOutcome = Unknown(
            session_key=str(key),
            invocation_id=invocation_id,
            detail="dispatch interrupted before acknowledgement",
            checkpoint=checkpoint,
        )
        try:
            result = self._client.run(
                session_spec=spec,
                turn=replace(
                    template,
                    message=message,
                    invocation_id=invocation_id,
                    expected_provider_session_id=(
                        checkpoint.provider_session_id if checkpoint is not None else None
                    ),
                ),
                session_key=key,
            )
            if checkpoint is None:
                checkpoint = self.checkpoint(key)
            outcome = Completed(
                session_key=str(key),
                invocation_id=invocation_id,
                result=result,
                checkpoint=checkpoint,
            )
        except AgentOutputSchemaError as error:
            outcome = self._schema_failure(pending, error, initial=initial)
        except BaseException as error:
            # Invocation acceptance is unknowable after any external failure.
            # Classifying a specific provider exception as safe would permit a
            # replay that the provider may already have executed.
            outcome = Unknown(
                session_key=str(key),
                invocation_id=invocation_id,
                detail=f"{type(error).__name__}: {error}",
                checkpoint=checkpoint,
            )
            if not isinstance(error, Exception):
                raise
        finally:
            with self._lock:
                try:
                    self._record(self._load(), invocation_id, digest, outcome)
                finally:
                    self._active.discard(invocation_id)
                    self._active_keys.discard(key)
        return outcome

    def _schema_failure(
        self, pending: Pending, error: AgentOutputSchemaError, *, initial: bool
    ) -> InvocationOutcome:
        if not initial:
            return Unknown(
                session_key=pending.session_key,
                invocation_id=pending.invocation_id,
                detail=str(error),
                checkpoint=pending.checkpoint,
            )
        checkpoint = pending.checkpoint
        identity = self._client.provider_session_id(AgentSessionKey.parse(pending.session_key))
        if identity is not None:
            checkpoint = AgentSessionCheckpoint(
                session_key=pending.session_key, provider_session_id=identity
            )
        self._schema_rejections.add(pending.invocation_id)
        return InvalidResponse(
            session_key=pending.session_key,
            invocation_id=pending.invocation_id,
            detail=error.detail,
            checkpoint=checkpoint,
        )

    @staticmethod
    def _validate_invocation(key: AgentSessionKey, invocation_id: str) -> None:
        if not key.durable:
            detail = f"session key {key} is not durable"
            raise SessionConfigurationError.because(detail)
        if not invocation_id:
            detail = "invocation_id must not be empty"
            raise SessionConfigurationError.because(detail)

    def _ensure_session_resolved(self, state: AgentInvocationState, key: AgentSessionKey) -> None:
        for record in state.invocations.values():
            outcome = record.outcome
            if isinstance(outcome, InvalidResponse) and (
                outcome.checkpoint is not None or outcome.invocation_id in self._schema_rejections
            ):
                continue
            if (
                outcome.session_key == str(key)
                and not isinstance(outcome, Completed)
                and not record.interrupted
            ):
                detail = f"session {key} has unresolved invocation {outcome.invocation_id}"
                raise InvocationConflictError.because(detail)

    def _binding(self, key: AgentSessionKey) -> tuple[AgentSessionSpec, AgentTurnRequest]:
        try:
            return self._bindings[key]
        except KeyError as error:
            detail = f"session key {key} is not bound"
            raise SessionConfigurationError.because(detail) from error

    def _load(self) -> AgentInvocationState:
        try:
            return self._slot.load_optional() or AgentInvocationState()
        except (ProjectError, OSError, ValidationError) as error:
            detail = f"cannot read invocation ledger: {error}"
            raise SessionPersistenceError.because(detail) from error

    def _record(
        self,
        state: AgentInvocationState,
        invocation_id: str,
        digest: str,
        outcome: InvocationOutcome,
    ) -> None:
        state.invocations[invocation_id] = AgentInvocationRecord(
            payload_digest=digest, outcome=outcome
        )
        try:
            self._slot.save(state)
        except (ProjectError, OSError, ValidationError) as error:
            detail = f"cannot commit invocation {invocation_id}: {error}"
            raise SessionPersistenceError.because(detail) from error
