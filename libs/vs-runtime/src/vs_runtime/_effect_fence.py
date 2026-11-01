"""The one fence between creating something for a request and answering "nothing held" for it.

An executor that is about to create something (a session binding, a provider turn, a
workspace) first *enters* the fence of what it creates. An executor that answers "nothing is
held" for it (a cancel, a close, a discard, an inspection that proves a turn never began)
first *closes* that fence. Each is one read-decide-write of one durable record under the
receipt store's cross-process lock, so whichever comes first wins, on any host and across
restarts:

* closed first: every later entry is refused, so nothing is ever created for the key;
* entered first: the close learns that something may be held (``MayHold``), so it must find
  and release what the entries made, and its answer is "held", never "nothing held".

An entry is durable before the effect it guards, so a host that dies after creating and
before answering leaves it behind: a later close still answers ``MayHold``. A creation that
is itself a record (a binding, a dispatch record) is written in the same locked step as its
entry (``ReceiptStore.exclusive``), and a creation outside the store enters again just before
it commits, so a close that came in between stops it.

``NothingHeld`` is the only proof ``ObservationFacts.never_held`` accepts, and only this
module makes one: from a close of a fence nobody entered, or from ``cleared`` with typed
``Evidence`` that the entries hold nothing, once no entry can still be creating. An entry can
still be creating while it has not left and its host is not older than the closer's: an
older host is a process a restart replaced (fencing a still running stale host is the host
epoch's job, not this fence's).
"""

from __future__ import annotations

from dataclasses import InitVar, dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from vs_core.api import HostFence, RequestId
from vs_runtime._operation_catalog import NotApplied

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from vs_runtime._operation_catalog import Applied, Indeterminate, Inspection
    from vs_runtime._receipt_store import ReceiptStore

_FAMILY = "effect-fences"
_PART = "fence"


class FenceKind(StrEnum):
    """What a fence guards; each kind has one creating executor."""

    SESSION_EPISODE = "session-episode"
    """The binding of one session in one admission episode (EnsureSession, CloseSession)."""
    INVOCATION = "invocation"
    """The provider turn of one invocation (DispatchTurn, CancelTurn, InspectTurn)."""
    SCOPE_EPISODE = "scope-episode"
    """The measurement jobs of one scope admission episode (SubmitMeasurement, CloseAttemptScope)."""
    MEASUREMENT_JOB = "measurement-job"
    """One measurement job of one scope episode (SubmitMeasurement, CancelOwnedJob)."""


@dataclass(frozen=True)
class FenceKey:
    """One thing a request may create: its kind and a key unique within the kind."""

    kind: FenceKind
    subject: str

    @property
    def storage_key(self) -> str:
        """The key of the fence's record in the receipt store."""
        return f"{self.kind}:{self.subject}"


class _Entry(BaseModel):
    """One request that entered the fence, on which host, and whether its delivery ended."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    request_id: RequestId
    host: HostFence
    left: bool = False


class _FenceRecord(BaseModel):
    """The durable state of one fence: who entered it and who closed it, first writer kept."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    entries: tuple[_Entry, ...] = ()
    closed_by: RequestId | None = None

    def entry(self, request_id: RequestId) -> _Entry | None:
        return next((e for e in self.entries if e.request_id == request_id), None)

    def with_entry(self, entry: _Entry) -> _FenceRecord:
        others = tuple(e for e in self.entries if e.request_id != entry.request_id)
        return self.model_copy(update={"entries": (*others, entry)})


@dataclass(frozen=True)
class Entered:
    """The fence is open: the entrant may create, and a later close will know it may hold."""

    key: FenceKey


@dataclass(frozen=True)
class Fenced:
    """The fence is closed: create nothing.

    ``entered_before`` says this request entered before the close. It may have created
    something then, so it may report what exists, but it starts nothing new.
    """

    key: FenceKey
    entered_before: bool


type Admission = Entered | Fenced

# The mint is an init-only argument, never a field: an instance does not carry it, so
# ``dataclasses.replace`` cannot copy it (it raises), and no copy of a proof about one
# fence becomes a proof about another.
_MINT = object()


@dataclass(frozen=True)
class NothingHeld:
    """Proof that nothing is held for ``key``, now or later: its fence is closed and empty.

    Only ``EffectFences`` makes one; constructing or copying it elsewhere raises.
    """

    key: FenceKey
    _mint: InitVar[object]

    def __post_init__(self, _mint: object) -> None:
        if _mint is not _MINT:
            message = "only EffectFences proves that nothing is held"
            raise TypeError(message)

    def vouch(self, key: FenceKey) -> None:
        """Raise ``TypeError`` unless this proof is about the fence of ``key``."""
        if self.key != key:
            message = (
                f"a proof for {self.key.storage_key} is about another fence than {key.storage_key}"
            )
            raise TypeError(message)


@dataclass(frozen=True)
class MayHold:
    """The fence is closed, but requests entered it first: something may be held.

    ``live`` says an entry may still be creating: it has not left, and its host is not
    older than the closer's. The closer then answers Unknown and asks again later.
    """

    key: FenceKey
    live: bool
    _mint: InitVar[object]

    def __post_init__(self, _mint: object) -> None:
        if _mint is not _MINT:
            message = "only EffectFences reports a closed fence"
            raise TypeError(message)


type Closure = NothingHeld | MayHold


@dataclass(frozen=True)
class _Minted:
    """Evidence only the step that establishes it returns (an ``EffectFences`` method)."""

    key: FenceKey
    _mint: InitVar[object]

    def __post_init__(self, _mint: object) -> None:
        if _mint is not _MINT:
            message = f"{type(self).__name__} comes only from the step that establishes it"
            raise TypeError(message)


@dataclass(frozen=True)
class Unrecorded(_Minted):
    """Read after the close: the record an entry writes with its creation is absent for ``key``.

    For a creation that is itself a record written in the same locked step as its entry (a
    binding, a job registration), an entry without the record created nothing. Only
    ``EffectFences.close_and_load`` returns one.
    """


@dataclass(frozen=True)
class Removed(_Minted):
    """After the close, the fence found what the entries of ``key`` left and removed it.

    Only ``EffectFences.remove`` returns one, from a lookup it ran itself after the close.
    """


@dataclass(frozen=True)
class OwnerNotApplied(_Minted):
    """The owner, asked after the close, says ``key``'s effect did not happen.

    Only ``EffectFences.ask_owner`` returns one, from the ``NotApplied`` answer to the question
    it asked itself after the close.
    """


type Evidence = Unrecorded | Removed | OwnerNotApplied
"""Why nothing is held behind a closed fence that requests entered; ``cleared`` requires one."""


class StillCreatingError(Exception):
    """``cleared`` was asked for a fence an entry may still be creating behind."""


def _older(entrant: HostFence, closer: HostFence) -> bool:
    """Whether the entrant's host is one a restart replaced before the closer's host began."""
    return entrant.epoch < closer.epoch


class EffectFences:
    """Durable fences in one receipt store; every operation is one locked read-decide-write."""

    def __init__(self, store: ReceiptStore) -> None:
        """Keep fences beside the executors' receipts, under the same lock."""
        self._store = store

    def enter(self, key: FenceKey, *, by: RequestId, host: HostFence) -> Admission:
        """Enter the fence before creating anything for *key*, or learn that it is closed.

        Entering again (the same request, just before it commits a creation, or after a
        restart) is allowed while the fence is open and refused once it is closed.
        """

        def decide(stored: _FenceRecord | None) -> tuple[_FenceRecord | None, Admission]:
            record = stored or _FenceRecord()
            if record.closed_by is not None:
                return None, Fenced(key, entered_before=record.entry(by) is not None)
            entry = _Entry(request_id=by, host=host)
            updated = record.with_entry(entry)
            return (None if updated == stored else updated), Entered(key)

        return self._store.modify(_FAMILY, _PART, key.storage_key, _FenceRecord, decide)

    def leave(self, key: FenceKey, *, by: RequestId) -> None:
        """Record that *by*'s delivery ended: whatever it created is made, and it creates no more.

        Every exit of an entrant's delivery leaves, so a close on the same host can tell
        a finished entry from one still creating. A host that dies never leaves; a later
        host's close treats its entries as finished (see ``MayHold.live``).
        """

        def decide(stored: _FenceRecord | None) -> tuple[_FenceRecord | None, None]:
            entry = None if stored is None else stored.entry(by)
            if stored is None or entry is None or entry.left:
                return None, None
            return stored.with_entry(entry.model_copy(update={"left": True})), None

        self._store.modify(_FAMILY, _PART, key.storage_key, _FenceRecord, decide)

    def is_closed(self, key: FenceKey) -> bool:
        """Whether a release closed the fence of *key*; read under ``ReceiptStore.exclusive``."""
        record = self._store.load(_FAMILY, _PART, key.storage_key, _FenceRecord)
        return record is not None and record.closed_by is not None

    def close(self, key: FenceKey, *, by: RequestId, host: HostFence) -> Closure:
        """Close the fence so nothing new is created for *key*; say whether anything may be held."""

        def decide(stored: _FenceRecord | None) -> tuple[_FenceRecord | None, Closure]:
            record = stored or _FenceRecord()
            closed = (
                record
                if record.closed_by is not None
                else record.model_copy(update={"closed_by": by})
            )
            if not record.entries:
                return (None if closed == stored else closed), NothingHeld(key, _MINT)
            live = any(not e.left and not _older(e.host, host) for e in record.entries)
            return (None if closed == stored else closed), MayHold(key, live, _MINT)

        return self._store.modify(_FAMILY, _PART, key.storage_key, _FenceRecord, decide)

    def close_and_load[T](
        self, key: FenceKey, *, by: RequestId, host: HostFence, load: Callable[[], T | None]
    ) -> tuple[Closure, T | Unrecorded]:
        """Close the fence of *key*, then ``load`` the record its entries write as they create.

        Both run under one hold of the store's lock. An entry writes that record in the same
        locked step as it enters, so when ``load`` finds none the entries created nothing,
        and the answer is ``Unrecorded`` evidence for ``cleared``.
        """
        with self._store.exclusive():
            closure = self.close(key, by=by, host=host)
            found = load()
        return closure, (Unrecorded(key, _MINT) if found is None else found)

    async def remove(
        self, closure: MayHold, find: Callable[[], Awaitable[Callable[[], Awaitable[None]] | None]]
    ) -> Removed:
        """Look for what the entries of *closure* left behind, and remove it if anything is.

        ``find`` returns the removal of the leftover, or ``None`` when it finds none. The fence
        runs the lookup itself, after the close, so "nothing is left" is its finding, never a
        caller's word. A lookup or a removal that fails raises, and then there is no evidence.
        """
        leftover = await find()
        if leftover is not None:
            await leftover()
        return Removed(closure.key, _MINT)

    async def ask_owner(
        self, closure: MayHold, inspect: Callable[[], Awaitable[Inspection]]
    ) -> OwnerNotApplied | Applied | Indeterminate:
        """Ask the owner, now that *closure* closed its fence, whether the effect happened.

        Only a ``NotApplied`` answer to this question becomes evidence for ``cleared``: it is
        asked after the close, and nothing starts behind a closed fence, so it holds for good.
        An answer got before the close cannot reach here. Any other answer is returned as is.
        """
        answer = await inspect()
        if isinstance(answer, NotApplied):
            return OwnerNotApplied(closure.key, _MINT)
        return answer

    def cleared(self, closure: MayHold, evidence: Evidence) -> NothingHeld:
        """Nothing is held: ``evidence`` shows the entries hold nothing, and none is still creating.

        Raises ``StillCreatingError`` for a live closure and ``TypeError`` for evidence about
        another fence.
        """
        if closure.live:
            message = f"an entry of {closure.key.storage_key} may still be creating"
            raise StillCreatingError(message)
        if evidence.key != closure.key:
            message = (
                f"evidence about {evidence.key.storage_key} clears no other fence than its own, "
                f"not {closure.key.storage_key}"
            )
            raise TypeError(message)
        return NothingHeld(closure.key, _MINT)


__all__ = [
    "Admission",
    "Closure",
    "EffectFences",
    "Entered",
    "Evidence",
    "FenceKey",
    "FenceKind",
    "Fenced",
    "MayHold",
    "NothingHeld",
    "OwnerNotApplied",
    "Removed",
    "StillCreatingError",
    "Unrecorded",
]
