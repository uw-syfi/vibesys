"""Durable dispatch guards for key-based agent continuations.

The ledger promises one logical dispatch. A persisted unfinished invocation is
Unknown after reconstruction, because provider acceptance cannot be inspected
through today's agentshim API. Such an invocation is never blindly replayed.
"""

from __future__ import annotations

import copy
import hashlib
import json
from contextlib import contextmanager
from dataclasses import replace
from typing import TYPE_CHECKING, Annotated, Literal, Protocol, runtime_checkable

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from vs_agent.contracts import (
    AgentOutputSchemaError,
    AgentSessionSpec,
    AgentTurnRequest,
    AgentTurnResult,
    SchemaViolation,
    SteerOutcome,
    session_spec_fingerprint,
)
from vs_agent.runner import parse_typed_response
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
    from collections.abc import Iterator
    from contextlib import AbstractContextManager

    from pydantic import GetCoreSchemaHandler
    from pydantic_core import core_schema

    from vs_agent.contracts import AgentCapabilities, AgentObserver
    from vs_sim.api import Threads


class _EveryFieldStored:
    """Decode a stored dataclass only if it names every field, defaulted ones included.

    The result type gives its later fields defaults so a provider can omit them, but a stored
    result was written by this version, which writes every field. Without this, a stored
    result missing ``refused_steers`` would decode as "nothing refused" and a restarted host
    would settle a refused steer as consumed. The defaults are dropped from a copy of the
    decoding schema only, so construction in code and strict JSON decoding are unchanged.
    """

    def __get_pydantic_core_schema__(
        self, source: type[object], handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        schema = copy.deepcopy(handler.resolve_ref_schema(handler(source)))
        if schema["type"] != "dataclass" or schema["schema"]["type"] != "dataclass-args":
            message = f"{source!r} is not decoded as a dataclass"
            raise TypeError(message)
        schema.pop("ref", None)
        for stored in schema["schema"]["fields"]:
            if stored["schema"]["type"] == "default":
                stored["schema"] = stored["schema"]["schema"]
        return schema


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
    result: Annotated[AgentTurnResult, _EveryFieldStored()]
    checkpoint: AgentSessionCheckpoint

    def parse[T: BaseModel](self, response_cls: type[T]) -> T:
        """The reply as *response_cls*, recovered from prose by ``parse_typed_response``.

        Raises ``AgentOutputSchemaError`` when no JSON value in the text validates.
        This is the only way completed agent text becomes a model.
        """
        return parse_typed_response(self.result.text, response_cls)

    @model_validator(mode="after")
    def _same_conversation(self) -> Completed:
        if (
            self.session_key != self.checkpoint.session_key
            or self.result.provider_session_id != self.checkpoint.provider_session_id
            or self.result.restarted
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
    violations: tuple[SchemaViolation, ...] = ()


type InvocationOutcome = Annotated[
    Completed | Pending | Unknown | InvalidResponse, Field(discriminator="kind")
]


class AgentSessions(Protocol):
    """Resume durable keyed conversations without replacing their history.

    Repeated invocation identities return their recorded outcome; a changed
    payload raises InvocationConflictError. Unknown never triggers dispatch.
    Configuration, missing checkpoints and persistence failures are typed.
    """

    def invocation_transaction(self) -> AbstractContextManager[AgentInvocationStore]:
        """Serialize one complete read/modify/write against every keyed turn path."""
        ...

    def start(
        self, key: AgentSessionKey, spec: AgentSessionSpec, turn: AgentTurnRequest
    ) -> InvocationOutcome:
        """Journal a keyed initial turn, replaying only durable completion evidence."""
        ...

    def cancel(self, key: AgentSessionKey, invocation_id: str) -> None:
        """Ask the provider to stop *invocation_id* if this instance is running it.

        A request, not a proof: only a later ``inspect`` that is not ``Pending``
        shows the turn ended. Any other invocation is left alone.
        """
        ...

    def steer(
        self, key: AgentSessionKey, invocation_id: str, text: str, *, steer_id: str
    ) -> SteerOutcome:
        """Offer *text* to *invocation_id*'s turn if this instance is running it.

        ``NO_RUNNING_TURN`` when it is not. ``DELIVERED`` says the provider took the
        message; the turn's result settles it exactly once, whether the turn returns or
        raises and however often a capacity stop resends it: consumed, or named by
        *steer_id* in ``AgentTurnResult.refused_steers`` of the completed invocation.
        """
        ...

    def release(self, key: AgentSessionKey) -> None:
        """Release the key's live provider resources, keeping its durable checkpoint.

        Raises InvocationConflictError while an invocation of the key is active.
        """
        ...

    def release_interrupted(self, key: AgentSessionKey, invocation_id: str) -> None:
        """Release key ownership after the caller drains an explicit interruption."""
        ...

    def checkpoint(self, key: AgentSessionKey) -> AgentSessionCheckpoint:
        """Return conversation identity or raise SessionResumeError."""
        ...

    def resume(
        self,
        key: AgentSessionKey,
        message: RenderedPrompt,
        invocation_id: str,
        *,
        standalone: bool = False,
    ) -> InvocationOutcome:
        """Dispatch once, continuing the checkpointed conversation.

        A *standalone* message carries the role's whole prompt, so it may run in a fresh
        conversation instead: when the checkpointed one is near its model's context window,
        or once after the provider refused it for context overflow. The completed outcome's
        checkpoint then names the fresh conversation, which later turns continue.
        """
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
    sequence: int = Field(default=0, ge=0)


class AgentInvocationState(BaseModel):
    """Strict machine-local invocation ledger, stored through Project's StateSlot."""

    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[2] = 2
    """2: a completed turn's result stores ``refused_steers``, and every result field is required."""
    invocations: dict[str, AgentInvocationRecord] = Field(default_factory=dict)

    @field_validator("schema_version", mode="before")
    @classmethod
    def _this_version(cls, value: object) -> object:
        """Refuse a ledger another VibeSys version wrote, naming the remedy."""
        current = cls.model_fields["schema_version"].default
        if isinstance(value, int) and not isinstance(value, bool) and value != current:
            message = (
                f"the ledger is version {value} and this VibeSys reads only version "
                f"{current}; a run whose agent ledger another version wrote cannot resume "
                "here: start a new run"
            )
            raise ValueError(message)
        return value

    def record(self, record: AgentInvocationRecord) -> None:
        """Preserve dispatch order independently of JSON object key ordering.

        Legacy records have sequence zero; they remain unordered until a new
        dispatch supplies an authoritative checkpoint.
        """
        identity = record.outcome.invocation_id
        previous = self.invocations.get(identity)
        sequence = (
            previous.sequence
            if previous is not None
            else max((item.sequence for item in self.invocations.values()), default=0) + 1
        )
        self.invocations[identity] = record.model_copy(update={"sequence": sequence})

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


def inspect_invocation_journal(
    key: AgentSessionKey, invocation_id: str, store: AgentInvocationStore | None
) -> InvocationOutcome:
    """Inspect persisted evidence without opening or assuming live dispatch ownership.

    Unfinished dispatch is Unknown because this reader has no provider ownership.
    Corrupt journals remain typed errors.
    """
    if not key.durable or not invocation_id:
        detail = "durable key and invocation_id are required"
        raise SessionConfigurationError.because(detail)
    try:
        state = None if store is None else store.load_optional()
    except (ProjectError, OSError, ValidationError) as error:
        detail = f"cannot inspect invocation journal: {error}"
        raise SessionPersistenceError.because(detail) from error
    record = None if state is None else state.invocations.get(invocation_id)
    if record is None:
        return Unknown(
            session_key=str(key), invocation_id=invocation_id, detail="no invocation evidence"
        )
    if record.outcome.session_key != str(key):
        detail = "invocation belongs to another key"
        raise InvocationConflictError.because(detail)
    if isinstance(record.outcome, Pending):
        return Unknown(
            session_key=str(key),
            invocation_id=invocation_id,
            detail="unfinished dispatch recovered without acceptance evidence",
            checkpoint=record.outcome.checkpoint,
        )
    return record.outcome


@runtime_checkable
class AgentTurnExecutor(Protocol):
    """Raw keyed turns required by the durable session journal.

    The executor preserves provider history and reports its exact conversation
    identity. Unsupported clients are rejected before journal dispatch.
    """

    @property
    def capabilities(self) -> AgentCapabilities:
        """Report execution and durable-resume guarantees."""
        ...

    def provider_session_id(self, session_key: AgentSessionKey) -> str | None:
        """Return the conversation the next keyed turn will continue."""
        ...

    def cancel_session(self, key: AgentSessionKey) -> None:
        """Ask the keyed conversation's in-flight turn to stop, keeping its checkpoint."""
        ...

    def steer_session(self, key: AgentSessionKey, text: str, *, steer_id: str) -> SteerOutcome:
        """Offer *text* to the keyed conversation's in-flight turn, queuing nothing.

        A ``DELIVERED`` message the provider does not keep is named by *steer_id* in the
        turn's ``AgentTurnResult.refused_steers``.
        """
        ...

    def release_session(self, key: AgentSessionKey) -> None:
        """Release the keyed conversation's live resources, keeping its checkpoint."""
        ...

    def run(
        self,
        *,
        session_spec: AgentSessionSpec,
        turn: AgentTurnRequest,
        session_key: AgentSessionKey | None = None,
        observer: AgentObserver | None = None,
    ) -> AgentTurnResult:
        """Run a keyed turn, preserving strict expected conversation identity."""
        ...


class ClientAgentSessions:
    """Continue AgentClient sessions with a persisted write-ahead dispatch guard.

    One instance owns its slot for the duration of a run; the shell supplies
    exclusive host ownership. The slot must be machine-local, alongside the
    provider checkpoints, and must survive reconstruction of this instance.
    """

    def __init__(
        self,
        client: AgentTurnExecutor,
        slot: AgentInvocationStore,
        *,
        threads: Threads,
    ) -> None:
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
        self._lock = threads.rlock()

    @contextmanager
    def invocation_transaction(self) -> Iterator[AgentInvocationStore]:
        """Hold journal ownership across an adapter's complete ledger mutation."""
        with self._lock:
            yield self._slot

    def bind(self, key: AgentSessionKey, spec: AgentSessionSpec, turn: AgentTurnRequest) -> None:
        """Bind fixed session configuration and the requested turn response schema.

        Only the response schema may change between turns. Active dispatch and
        all session, identity, and other turn configuration remain fenced.
        """
        if not key.durable:
            detail = f"session key {key} is not durable"
            raise SessionConfigurationError.because(detail)
        with self._lock:
            old = self._bindings.get(key)
            if (
                old is not None
                and old != (spec, turn)
                and (
                    key in self._active_keys
                    or old[0] != spec
                    or replace(old[1], output_schema=turn.output_schema) != turn
                )
            ):
                detail = f"session key {key} is already bound"
                raise SessionConfigurationError.because(detail)
            self._bindings[key] = (spec, turn)

    def cancel(self, key: AgentSessionKey, invocation_id: str) -> None:
        """Ask the provider to stop *invocation_id* if this instance is running it."""
        self._validate_invocation(key, invocation_id)
        with self._lock:
            running = invocation_id in self._active
        if running:
            self._client.cancel_session(key)

    def steer(
        self, key: AgentSessionKey, invocation_id: str, text: str, *, steer_id: str
    ) -> SteerOutcome:
        """Offer *text* to *invocation_id*'s turn if this instance is running it."""
        self._validate_invocation(key, invocation_id)
        with self._lock:
            running = invocation_id in self._active
        if not running:
            return SteerOutcome.NO_RUNNING_TURN
        return self._client.steer_session(key, text, steer_id=steer_id)

    def release(self, key: AgentSessionKey) -> None:
        """Release the key's live provider resources, keeping its durable checkpoint."""
        if not key.durable:
            detail = f"session key {key} is not durable"
            raise SessionConfigurationError.because(detail)
        with self._lock:
            if key in self._active_keys:
                detail = f"session {key} has an active invocation"
                raise InvocationConflictError.because(detail)
            self._client.release_session(key)

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
        self,
        key: AgentSessionKey,
        message: RenderedPrompt,
        invocation_id: str,
        *,
        standalone: bool = False,
    ) -> InvocationOutcome:
        """Persist dispatch intent before continuing the bound conversation.

        A *standalone* message may continue in a fresh conversation (see ``AgentSessions``);
        the binding then follows the fresh conversation.
        """
        if not isinstance(message, RenderedPrompt):
            detail = "resume message must be a RenderedPrompt"
            raise SessionConfigurationError.because(detail)
        spec, template = self._binding(key)
        return self._dispatch(
            key,
            spec,
            replace(template, message=message, invocation_id=invocation_id, renewable=standalone),
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
            checkpoint = self._initial_checkpoint(key) if initial else self.checkpoint(key)
            self._ensure_session_resolved(state, key)
            self._validate_checkpoint(state, key, checkpoint, template.expected_provider_session_id)
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
                    require_provider_checkpoint=True,
                    expected_provider_session_id=(
                        checkpoint.provider_session_id if checkpoint is not None else None
                    ),
                ),
                session_key=key,
            )
            if checkpoint is None or (
                template.renewable and result.provider_session_id != checkpoint.provider_session_id
            ):
                # A first turn, or a standalone turn the client ran in a fresh conversation.
                checkpoint = self.checkpoint(key)
                self._follow(key, checkpoint)
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

    def _follow(self, key: AgentSessionKey, checkpoint: AgentSessionCheckpoint) -> None:
        """Bind the key's later turns to the conversation it continues now."""
        with self._lock:
            bound = self._bindings.get(key)
            if bound is None or bound[1].expected_provider_session_id is None:
                return
            spec, template = bound
            self._bindings[key] = (
                spec,
                replace(template, expected_provider_session_id=checkpoint.provider_session_id),
            )

    def _initial_checkpoint(self, key: AgentSessionKey) -> AgentSessionCheckpoint | None:
        identity = self._client.provider_session_id(key)
        return (
            None
            if identity is None
            else AgentSessionCheckpoint(session_key=str(key), provider_session_id=identity)
        )

    @staticmethod
    def _validate_checkpoint(
        state: AgentInvocationState,
        key: AgentSessionKey,
        checkpoint: AgentSessionCheckpoint | None,
        expected: str | None,
    ) -> None:
        if expected is not None and (
            checkpoint is None or checkpoint.provider_session_id != expected
        ):
            raise SessionResumeError(str(key), "bound checkpoint identity changed")
        records = [
            record
            for record in state.invocations.values()
            if record.outcome.session_key == str(key) and not record.interrupted
        ]
        latest = max((record.sequence for record in records), default=0)
        for record in records:
            if record.sequence != latest:
                continue
            prior = record.outcome
            if checkpoint is None or prior.checkpoint is None:
                raise SessionResumeError(str(key), "acknowledged provider checkpoint is missing")
            if prior.checkpoint != checkpoint:
                raise SessionResumeError(str(key), "provider checkpoint identity changed")

    def _schema_failure(
        self, pending: Pending, error: AgentOutputSchemaError, *, initial: bool
    ) -> InvocationOutcome:
        checkpoint = pending.checkpoint
        identity = self._client.provider_session_id(AgentSessionKey.parse(pending.session_key))
        if identity is not None:
            checkpoint = AgentSessionCheckpoint(
                session_key=pending.session_key, provider_session_id=identity
            )
        if initial:
            self._schema_rejections.add(pending.invocation_id)
        return InvalidResponse(
            session_key=pending.session_key,
            invocation_id=pending.invocation_id,
            detail=error.detail,
            violations=error.violations,
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
                if isinstance(outcome, Unknown):
                    detail = outcome.detail
                elif isinstance(outcome, Pending):
                    detail = "unfinished dispatch recovered without acceptance evidence"
                else:
                    detail = "acknowledged provider checkpoint is missing"
                raise SessionResumeError(str(key), detail)

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
        state.record(AgentInvocationRecord(payload_digest=digest, outcome=outcome))
        try:
            self._slot.save(state)
        except (ProjectError, OSError, ValidationError) as error:
            detail = f"cannot commit invocation {invocation_id}: {error}"
            raise SessionPersistenceError.because(detail) from error
