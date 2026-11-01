"""Durable workspace tables over the shared ``ReceiptStore``.

Bindings, the exclusive-root holder, attempt generations, revision ownership and
release proofs are records in the one crash-safe store every executor uses, so each
read-modify-write that decides between two hosts runs under its cross-process lock.
Whether an effect runs again is not recorded here: the step loop owns it, and an executor
repeats an effect through ``ExecutionContext.repeat``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, model_validator

from vs_core.api import AttemptRef, RequestId, ResourceId, RevisionRef, WorkspaceMode

if TYPE_CHECKING:
    from vs_runtime._receipt_store import ReceiptStore


class AttemptBinding(BaseModel):
    """The workspace one attempt owns, fixed by its first accepted ensure."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    ensure_request: RequestId
    mode: WorkspaceMode
    base: RevisionRef
    resource_id: ResourceId
    workspace_id: str | None


class RootGrant(StrEnum):
    """Outcome of asking for the exclusive root."""

    GRANTED = "granted"
    HELD = "held"
    SUPERSEDED = "superseded"
    RELEASED = "released"
    """This attempt generation already ended its hold, so it cannot take the root again."""


class RootRelease(StrEnum):
    """What a request to end an attempt generation's hold of the exclusive root did."""

    RELEASED = "released"
    """The generation held the root and its hold ended now."""
    ALREADY_RELEASED = "already_released"
    """The generation's hold had ended before, whoever holds the root since."""
    NOT_HOLDER = "not_holder"
    """The generation never ended its hold and does not hold the root (a newer one took it)."""


class BaselineEntryKind(StrEnum):
    """What a baseline path held when it was recorded, and so how drift is checked."""

    DIRECTORY = "directory"
    """A path recorded with a trailing ``/``: checked only for removal."""
    FILE = "file"
    """A contained regular file with a content fingerprint: checked for removal and change."""
    UNFINGERPRINTED = "unfingerprinted"
    """Something that is not a contained regular file (a symlink, a FIFO, an unreadable
    file): checked only for removal, as nothing at the path any more."""


class BaselineEntry(BaseModel):
    """One path the exclusive root held before the run, with a file's content fingerprint.

    ``fingerprint`` is a file's content hash, set when *path* names a contained regular
    file. A directory (a path recorded with a trailing ``/``) has none: fingerprinting its
    contents would need the walk PR #2011 chose not to pay for (a ``.venv`` can hold 100k
    files), so a directory is only ever checked for removal, never for a change inside it.
    Neither has anything else that could not be fingerprinted; ``kind`` tells them apart.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    path: str
    fingerprint: str | None = None

    @model_validator(mode="after")
    def _directory_has_no_fingerprint(self) -> Self:
        if self.path.endswith("/") and self.fingerprint is not None:
            message = f"directory baseline entry {self.path!r} must not carry a fingerprint"
            raise ValueError(message)
        return self

    @property
    def kind(self) -> BaselineEntryKind:
        """What the path held when recorded."""
        if self.path.endswith("/"):
            return BaselineEntryKind.DIRECTORY
        if self.fingerprint is not None:
            return BaselineEntryKind.FILE
        return BaselineEntryKind.UNFINGERPRINTED


class RootBaseline(BaseModel):
    """The untracked and ignored paths the root held before any attempt ran.

    Directories without tracked files are listed once, with a trailing ``/``. These
    belong to the user (a ``.venv``, data, notes), so no root grant removes them.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    entries: tuple[BaselineEntry, ...]

    @property
    def paths(self) -> tuple[str, ...]:
        """The recorded paths alone: ``git clean``'s keep list and the exact-tree exemption."""
        return tuple(entry.path for entry in self.entries)


class RootBaselineDrift(BaseModel):
    """Pre-run paths found changed or gone when an attempt's hold of the root ended.

    Empty when the root holds every baseline path exactly as the first grant found it.
    This is reported to the operator, never acted on: the restore already decided (see
    ``RootBaseline``) to leave baseline paths untouched, whether or not an attempt changed
    them.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    modified: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        """Return whether the root matches the baseline exactly."""
        return not self.modified and not self.removed


class WorkspaceReceipts(Protocol):
    """Durable bindings, root holder, generations, revision ownership and release proofs."""

    def load_binding(self, attempt: AttemptRef) -> AttemptBinding | None: ...

    def bind(self, attempt: AttemptRef, binding: AttemptBinding) -> AttemptBinding:
        """Store the binding if absent; return the binding that won.

        A repeated ensure finds the binding an earlier attempt made.
        """
        ...

    def acquire_root(self, attempt: AttemptRef) -> RootGrant:
        """Grant the exclusive root to one attempt generation at a time."""
        ...

    def release_root(self, attempt: AttemptRef) -> RootRelease:
        """End *attempt*'s hold of the exclusive root, if it is the recorded holder.

        Only the holder's own generation releases, so a stale or repeated release
        changes nothing and a later holder is never released by an earlier attempt.
        The result says which of those happened.
        """
        ...

    def root_baseline(self) -> RootBaseline | None:
        """The recorded pre-run baseline, or ``None`` before the first root grant."""
        ...

    def record_root_baseline(self, baseline: RootBaseline) -> RootBaseline:
        """Store the baseline if none is stored; return the baseline that won.

        The first scan wins and is never replaced, so a replay after attempts ran
        keeps the pre-run answer instead of treating their files as the user's.
        """
        ...

    def admit_generation(self, attempt: AttemptRef) -> bool:
        """Record the attempt's generation; ``False`` once a higher one was recorded."""
        ...

    def root_holder(self) -> AttemptRef | None: ...

    def record_revision(self, commit: str, owner: str) -> None: ...

    def revision_owners(self, commit: str) -> frozenset[str]: ...

    def mark_released(self, attempt: AttemptRef) -> None: ...

    def is_released(self, attempt: AttemptRef) -> bool: ...


def attempt_key(attempt: AttemptRef) -> str:
    """Return the stable key of one attempt generation."""
    return f"{attempt.attempt_id.root}:{attempt.generation}"


_BINDINGS = "workspace-bindings"
_ROOT = "workspace-root"
_BASELINE = "workspace-root-baseline"
_GENERATIONS = "workspace-generations"
_REVISIONS = "workspace-revisions"
_RELEASED = "workspace-released"


class RevisionOwners(BaseModel):
    """Who made or retained one commit, so a later request may only name known revisions."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    owners: tuple[str, ...] = ()


class Released(BaseModel):
    """Proof that one attempt generation's discard completed."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    released: Literal[True] = True


class StoreWorkspaceReceipts:
    """The workspace tables, as records in the shared ``ReceiptStore``.

    Every read-modify-write runs under the store's cross-process lock, so two hosts
    cannot both bind an attempt, hold the exclusive root or advance a generation.
    Execution receipts (begun and sealed results) are the store's ``run_once``.
    """

    def __init__(self, store: ReceiptStore) -> None:
        self._store = store

    def load_binding(self, attempt: AttemptRef) -> AttemptBinding | None:
        return self._store.load(_BINDINGS, "binding", attempt_key(attempt), AttemptBinding)

    def bind(self, attempt: AttemptRef, binding: AttemptBinding) -> AttemptBinding:
        def decide(stored: AttemptBinding | None) -> tuple[AttemptBinding | None, AttemptBinding]:
            return (None, stored) if stored is not None else (binding, binding)

        return self._store.modify(
            _BINDINGS, "binding", attempt_key(attempt), AttemptBinding, decide
        )

    def acquire_root(self, attempt: AttemptRef) -> RootGrant:
        def decide(holder: AttemptRef | None) -> tuple[AttemptRef | None, RootGrant]:
            if self.is_released(attempt):
                # An ended generation is never granted again, whoever holds the root now.
                return None, RootGrant.RELEASED
            ended = holder is not None and self.is_released(holder)
            if holder is None or (ended and holder.attempt_id != attempt.attempt_id):
                return attempt, RootGrant.GRANTED
            if holder.attempt_id != attempt.attempt_id:
                return None, RootGrant.HELD
            if attempt.generation < holder.generation:
                return None, RootGrant.SUPERSEDED
            if attempt == holder:
                return None, RootGrant.GRANTED  # The holder asks again: still its root.
            return attempt, RootGrant.GRANTED

        with self._store.exclusive():
            return self._store.modify(_ROOT, "holder", "root", AttemptRef, decide)

    def release_root(self, attempt: AttemptRef) -> RootRelease:
        with self._store.exclusive():
            if self.is_released(attempt):
                return RootRelease.ALREADY_RELEASED
            if self._store.load(_ROOT, "holder", "root", AttemptRef) != attempt:
                return RootRelease.NOT_HOLDER
            self.mark_released(attempt)
            return RootRelease.RELEASED

    def root_baseline(self) -> RootBaseline | None:
        return self._store.load(_BASELINE, "baseline", "root", RootBaseline)

    def record_root_baseline(self, baseline: RootBaseline) -> RootBaseline:
        def decide(stored: RootBaseline | None) -> tuple[RootBaseline | None, RootBaseline]:
            return (baseline, baseline) if stored is None else (None, stored)

        return self._store.modify(_BASELINE, "baseline", "root", RootBaseline, decide)

    def admit_generation(self, attempt: AttemptRef) -> bool:
        def decide(stored: AttemptRef | None) -> tuple[AttemptRef | None, bool]:
            if stored is not None and attempt.generation < stored.generation:
                return None, False
            newer = stored is None or attempt.generation > stored.generation
            return (attempt if newer else None), True

        return self._store.modify(
            _GENERATIONS, "generation", attempt.attempt_id.root, AttemptRef, decide
        )

    def root_holder(self) -> AttemptRef | None:
        return self._store.load(_ROOT, "holder", "root", AttemptRef)

    def record_revision(self, commit: str, owner: str) -> None:
        def decide(stored: RevisionOwners | None) -> tuple[RevisionOwners | None, None]:
            owners = () if stored is None else stored.owners
            return (None if owner in owners else RevisionOwners(owners=(*owners, owner))), None

        self._store.modify(_REVISIONS, "owners", commit, RevisionOwners, decide)

    def revision_owners(self, commit: str) -> frozenset[str]:
        stored = self._store.load(_REVISIONS, "owners", commit, RevisionOwners)
        return frozenset() if stored is None else frozenset(stored.owners)

    def mark_released(self, attempt: AttemptRef) -> None:
        self._store.replace(_RELEASED, "released", attempt_key(attempt), Released())

    def is_released(self, attempt: AttemptRef) -> bool:
        return self._store.load(_RELEASED, "released", attempt_key(attempt), Released) is not None
