"""Serialize machine-local provider session updates across agent workers."""

from __future__ import annotations

from threading import RLock
from typing import TYPE_CHECKING, NotRequired, TypedDict, Unpack

from vs_agent.api import AgentSessionState, DurableSessionStore

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_agent.api import AgentSessionKey
    from vs_project.api import StateSlot


class _RecordFields(TypedDict):
    spec_fingerprint: str
    provider: str
    model: str | None
    session_id: str
    role: NotRequired[str | None]


class SynchronizedSessionStore(DurableSessionStore):
    """Keep distinct agent clients from losing each other's session records."""

    def __init__(self, slot: StateSlot[AgentSessionState], *, log: Callable[[str], None]) -> None:
        """Initialize the durable slot and its process-local mutation lock."""
        super().__init__(slot, log=log)
        self._lock = RLock()

    def record(self, key: AgentSessionKey, **kwargs: Unpack[_RecordFields]) -> None:
        """Read, update, and save one provider session under a shared lock."""
        with self._lock:
            super().record(key, **kwargs)

    def clear(self, key: AgentSessionKey) -> None:
        """Remove one provider session under the same mutation lock."""
        with self._lock:
            super().clear(key)
