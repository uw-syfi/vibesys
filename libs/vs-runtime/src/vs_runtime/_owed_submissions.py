"""The durable record of agent submissions the host charged and the agent may not know of.

An agent's submission is charged when core admits it, and the reply that carries its
handle can be lost. The submission is then *owed* to the agent: its retry must get the
same handle and charge nothing. That has to hold across a host restart, so the record
lives in the run's receipt store, not in the bridge's memory.

One entry per (scope, candidate revision, policy). The entry is the *intent*: it is
written before the charge, with the id (``call_id``) the charge will carry. The charge
itself is core's, and core's committed state is the authority on whether it happened:
an entry whose ``call_id`` core knows is charged, one it does not know never was. The
entry is cleared when the client confirms it read the reply.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from vs_core.api import Scope

if TYPE_CHECKING:
    from vs_runtime._keyed_documents import KeyedDocuments

_FAMILY = "agent-owed-submissions"


class OwedKey(BaseModel):
    """The full identity of a submission: who asked, for what content, under which policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    scope: Scope
    revision: str
    policy: str
    """Digest of the host's measurement policy; another policy is another measurement."""


class OwedSubmission(BaseModel):
    """The intent to charge one submission, and the id the charge carries."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    key: OwedKey
    call_id: str = Field(min_length=1)
    submitted_at: float
    """When the call was made (the answer's deadline is core's, in its record of the call)."""


class _Entries(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    entries: tuple[OwedSubmission, ...] = ()


class OwedSubmissions:
    """The run's owed submissions, as one record in the shared ``KeyedDocuments``."""

    def __init__(self, store: KeyedDocuments) -> None:
        """Bind the receipt store of the run."""
        self._store = store

    def _load(self) -> tuple[OwedSubmission, ...]:
        stored = self._store.load(_FAMILY, "owed", "run", _Entries)
        return () if stored is None else stored.entries

    def find(self, key: OwedKey) -> OwedSubmission | None:
        """The entry for ``key``, if one is recorded."""
        return next((item for item in self._load() if item.key == key), None)

    def all(self) -> tuple[OwedSubmission, ...]:
        """Every recorded entry."""
        return self._load()

    def record(self, owed: OwedSubmission) -> None:
        """Durably record ``owed``, replacing any entry for the same key."""

        def decide(stored: _Entries | None) -> tuple[_Entries, None]:
            kept = () if stored is None else stored.entries
            rest = tuple(item for item in kept if item.key != owed.key)
            return _Entries(entries=(*rest, owed)), None

        self._store.modify(_FAMILY, "owed", "run", _Entries, decide)

    def clear(self, key: OwedKey, call_id: str) -> None:
        """Drop the entry for ``key`` if it is still the one that carries ``call_id``."""

        def decide(stored: _Entries | None) -> tuple[_Entries | None, None]:
            if stored is None:
                return None, None
            rest = tuple(
                item for item in stored.entries if not (item.key == key and item.call_id == call_id)
            )
            return (None if len(rest) == len(stored.entries) else _Entries(entries=rest)), None

        self._store.modify(_FAMILY, "owed", "run", _Entries, decide)
