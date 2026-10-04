"""Durable workspace tables over the shared ``ReceiptStore``.

Bindings, the exclusive-root holder, attempt generations, revision ownership and
release proofs are records in the one crash-safe store every executor uses, so each
read-modify-write that decides between two hosts runs under its cross-process lock.
The per-request effect-once rule (begun marker, sealed result, fence and lease) is
the store's ``run_once``, not this module's.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict

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


class WorkspaceReceipts(Protocol):
    """Durable bindings, root holder, generations, revision ownership and release proofs."""

    def load_binding(self, attempt: AttemptRef) -> AttemptBinding | None: ...

    def bind(self, attempt: AttemptRef, binding: AttemptBinding) -> AttemptBinding:
        """Store the binding if absent; return the binding that won."""
        ...

    def acquire_root(self, attempt: AttemptRef) -> RootGrant:
        """Grant the exclusive root to one attempt generation at a time."""
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
            return (binding, binding) if stored is None else (None, stored)

        return self._store.modify(
            _BINDINGS, "binding", attempt_key(attempt), AttemptBinding, decide
        )

    def acquire_root(self, attempt: AttemptRef) -> RootGrant:
        def decide(holder: AttemptRef | None) -> tuple[AttemptRef | None, RootGrant]:
            if holder is not None:
                if holder.attempt_id != attempt.attempt_id:
                    return None, RootGrant.HELD
                if attempt.generation < holder.generation:
                    return None, RootGrant.SUPERSEDED
                if attempt.generation == holder.generation:
                    return None, RootGrant.GRANTED
            return attempt, RootGrant.GRANTED

        return self._store.modify(_ROOT, "holder", "root", AttemptRef, decide)

    def admit_generation(self, attempt: AttemptRef) -> bool:
        def decide(stored: AttemptRef | None) -> tuple[AttemptRef | None, bool]:
            if stored is not None and attempt.generation < stored.generation:
                return None, False
            newer = stored is None or attempt.generation > stored.generation
            return (attempt if newer else None), True

        return self._store.modify(
            _GENERATIONS, "generation", attempt.attempt_id.root, AttemptRef, decide
        )

    def admit_generation(self, attempt: AttemptRef) -> bool:
        path = f"generations/{_name(attempt.attempt_id.root)}"
        with self._exclusive():
            stored = self._read(path, AttemptRef)
            if stored is not None and attempt.generation < stored.generation:
                return False
            if stored is None or attempt.generation > stored.generation:
                self._write(path, attempt)
            return True

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
