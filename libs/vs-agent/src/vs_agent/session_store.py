"""Durable, machine-local checkpoints of coding-agent provider session IDs.

A provider session ID (a Codex thread, a Claude session) lets a resumed run
continue an implementer's conversation with ``codex exec resume <id>`` /
``claude --resume <id>`` instead of replaying the round from a fresh command.
The IDs are machine-local: the provider transcripts they name live under the
invoking user's home, so they must persist in the run's *local* state
namespace, never in the portable ``agent/state.json`` snapshot that travels
across machines.

Each record stores the fingerprint of the session spec that produced it, so a
resumed process refuses a checkpoint whose configuration has since changed,
using the same comparison an in-process client makes before reusing a live
session. Nothing here decides when a session is invalid: callers persist after
each completed turn and clear when a driver reports a restart.

The map is keyed by the stored form of an :class:`AgentSessionKey`, and only
keys whose scope opts into durability are ever written (see
:mod:`vs_agent.session_key`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from vs_agent.session_key import AgentSessionKey
from vs_project.api import ProjectError

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_project.api import StateSlot


class ProviderSessionRecord(BaseModel):
    """One checkpointed provider conversation."""

    model_config = ConfigDict(extra="forbid")

    #: Digest of the :class:`~vs_agent.contracts.AgentSessionSpec` whose
    #: turn produced ``session_id``. It is the whole resume decision: a
    #: checkpoint is adopted only by a session built from an identical spec.
    spec_fingerprint: str
    session_id: str
    #: Human-facing provenance for anyone reading ``sessions.json``. Never read
    #: when deciding whether to resume; ``spec_fingerprint`` owns that.
    provider: str
    model: str | None = None
    role: str | None = None


class AgentSessionState(BaseModel):
    """The machine-local session map for one run."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    sessions: dict[str, ProviderSessionRecord] = Field(default_factory=dict)

    @field_validator("sessions")
    @classmethod
    def _reject_unownable_keys(
        cls, sessions: dict[str, ProviderSessionRecord]
    ) -> dict[str, ProviderSessionRecord]:
        """Require every stored key to be one this version may resume.

        A key that does not parse, or whose scope does not opt into durability,
        was written by a different contract. Rejecting the file is safe because
        :class:`DurableSessionStore` treats a load failure as "no checkpoints"
        and rewrites the map on the next completed turn.
        """
        for stored in sessions:
            key = AgentSessionKey.parse(stored)
            if not key.durable:
                message = f"session scope is not persistable: {stored!r}"
                raise ValueError(message)
        return sessions


@runtime_checkable
class SessionStore(Protocol):
    """Checkpoint and look up provider session IDs by session key.

    Implementations must never raise. A store is a cache of an optimization
    (resuming instead of replaying), so a broken backing file degrades to no
    persistence rather than failing the turn that tried to use it.
    """

    def get(self, key: AgentSessionKey) -> ProviderSessionRecord | None:
        """Return the checkpoint for ``key``, or ``None``."""
        ...

    def record(
        self,
        key: AgentSessionKey,
        *,
        spec_fingerprint: str,
        provider: str,
        model: str | None,
        session_id: str,
        role: str | None = None,
    ) -> None:
        """Checkpoint ``session_id`` for ``key``, replacing any earlier one."""
        ...

    def clear(self, key: AgentSessionKey) -> None:
        """Forget any checkpoint for ``key``."""
        ...


class NullSessionStore:
    """A no-op store: nothing is persisted, every lookup misses.

    The default when no durable namespace is wired (ephemeral runs, tests, and
    non-agent loops), so session persistence is opt-in and never a hard
    dependency of :class:`~vs_agent.client.AgentClient`.
    """

    def get(self, key: AgentSessionKey) -> ProviderSessionRecord | None:
        """Return no record because this store never persists sessions."""
        return None

    def record(
        self,
        key: AgentSessionKey,
        *,
        spec_fingerprint: str,
        provider: str,
        model: str | None,
        session_id: str,
        role: str | None = None,
    ) -> None:
        """Ignore session updates because this store is intentionally inert."""
        del key, spec_fingerprint, provider, model, session_id, role

    def clear(self, key: AgentSessionKey) -> None:
        """Ignore clears because this store has no persisted records."""
        return


def _ignore_diagnostic(_message: str) -> None:
    """Discard a store diagnostic when no log sink was configured."""


class DurableSessionStore:
    """A :class:`SessionStore` backed by one machine-local state slot.

    Each operation reloads the slot so the on-disk map, not an in-memory cache,
    is the source of truth across process restarts; the file is small and
    written at most once per agent turn.

    Every filesystem and schema failure is reported through ``log`` and then
    swallowed: a malformed or unreadable map degrades this run to no session
    persistence, and the next completed turn overwrites it with a valid one.
    Keys whose scope does not opt into durability are ignored, so a caller
    that falls back to a role key never leaves a checkpoint behind.
    """

    def __init__(
        self,
        slot: StateSlot[AgentSessionState],
        *,
        log: Callable[[str], None] = _ignore_diagnostic,
    ) -> None:
        """Bind the store to a local-namespace ``sessions.json`` slot."""
        self._slot = slot
        self._log = log

    def get(self, key: AgentSessionKey) -> ProviderSessionRecord | None:
        """Return the checkpoint for ``key``, or ``None``."""
        if not key.durable:
            return None
        return self._load().sessions.get(str(key))

    def record(
        self,
        key: AgentSessionKey,
        *,
        spec_fingerprint: str,
        provider: str,
        model: str | None,
        session_id: str,
        role: str | None = None,
    ) -> None:
        """Checkpoint ``session_id`` for ``key``, replacing any earlier one."""
        if not key.durable:
            # Not an error: unscoped calls fall back to a role key, and those
            # conversations belong to one process only.
            return
        state = self._load()
        state.sessions[str(key)] = ProviderSessionRecord(
            spec_fingerprint=spec_fingerprint,
            session_id=session_id,
            provider=provider,
            model=model,
            role=role,
        )
        self._save(state)

    def clear(self, key: AgentSessionKey) -> None:
        """Forget any checkpoint for ``key``."""
        if not key.durable:
            return
        state = self._load()
        if state.sessions.pop(str(key), None) is not None:
            self._save(state)

    def _load(self) -> AgentSessionState:
        try:
            return self._slot.load_optional() or AgentSessionState()
        except (ProjectError, OSError) as exc:
            self._log(
                "[agent-session] ignoring unreadable provider-session checkpoints; "
                f"this run will not resume conversations: {type(exc).__name__}: {exc}"
            )
            return AgentSessionState()

    def _save(self, state: AgentSessionState) -> None:
        try:
            self._slot.save(state)
        except (ProjectError, OSError) as exc:
            self._log(
                "[agent-session] could not checkpoint the provider session; "
                f"the completed turn is unaffected: {type(exc).__name__}: {exc}"
            )
