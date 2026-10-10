"""Memory and crash-atomic local implementations of the StateStore role."""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from threading import RLock
from typing import TYPE_CHECKING

from vs_project._state_io import (
    LocalAtomicWriteEffects,
    decode_state_document,
    sync_directory_chain,
)
from vs_project._store_operations import STORE_DOCUMENT_VERSION, StoreDocument, StoreOperations
from vs_project.api.state_store import CommitFault, ObservationFault, StateStoreWriteError
from vs_project.errors import ProjectStateError

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from pathlib import Path

    from vs_project._state_io import AtomicWriteEffects, AtomicWriteStream
    from vs_project.project import Project


def _decode_store_document(source: bytes, *, location: str) -> StoreDocument:
    """Decode store bytes; damage, or a newer release's version, is a typed error."""
    return decode_state_document(
        StoreDocument, source, source=location, versioned_by=("version", STORE_DOCUMENT_VERSION)
    )


class _FaultAtomicWriteEffects(LocalAtomicWriteEffects):
    """Lose publication acknowledgement at an actual atomic-rename boundary."""

    def __init__(self, fault: CommitFault) -> None:
        self._fault = fault

    def sync_file(self, stream: AtomicWriteStream) -> None:
        if self._fault == CommitFault.FAILED:
            message = "state-store staging synchronization failed"
            raise StateStoreWriteError(message)
        super().sync_file(stream)

    def replace(self, temporary: Path, destination: Path) -> None:
        if self._fault == CommitFault.UNKNOWN_BEFORE:
            message = "state-store acknowledgement lost before rename"
            raise OSError(message)
        super().replace(temporary, destination)
        if self._fault == CommitFault.UNKNOWN_AFTER:
            message = "state-store acknowledgement lost after rename"
            raise OSError(message)

    def sync_directory(self, directory: Path) -> None:
        if self._fault == CommitFault.UNKNOWN_SYNC:
            message = "state-store directory synchronization failed"
            raise OSError(message)
        super().sync_directory(directory)


class _FaultObservationEffects(LocalAtomicWriteEffects):
    """Fail directory synchronization at the filesystem observation boundary."""

    def sync_directory(self, directory: Path) -> None:
        message = f"state-store observation synchronization failed: {directory}"
        raise OSError(message)


class FakeStateStore(StoreOperations):
    """In-memory faithful store with deterministic public commit faults."""

    def __init__(
        self,
        *,
        fault_plan: Iterable[CommitFault] = (),
        lease_fault_plan: Iterable[CommitFault | None] = (),
        observation_fault_plan: Iterable[ObservationFault | None] = (),
    ) -> None:
        """Create a shared store with deterministic mutation and read faults."""
        super().__init__(fault_plan, lease_fault_plan, observation_fault_plan)
        self._contents: bytes | None = None
        self._lock = RLock()

    def replace_document(self, contents: bytes) -> None:
        """Replace the stored document bytes, including deliberately damaged ones."""
        with self._lock:
            self._contents = contents

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            yield

    def _read(self) -> StoreDocument:
        fault = next(self._observation_faults, None)
        if fault == ObservationFault.READ or (
            fault == ObservationFault.SYNC and self._contents is not None
        ):
            message = f"state-store observation {fault} failed"
            raise OSError(message)
        if self._contents is None:
            return StoreDocument()
        return _decode_store_document(self._contents, location="store.json")

    def _write(self, document: StoreDocument) -> None:
        self._contents = document.model_dump_json().encode()


class FakeStateStores:
    """A ``StateStoreFactory`` of memory stores that outlive any one run host.

    One store per (project root, run id) is kept for the Fake's lifetime, so a
    run opened again for the same run id reads exactly the records and lease the
    earlier host committed, as a restart over the shared filesystem does. A test
    that wants a crash keeps this object and starts the next host with it.
    """

    def __init__(self) -> None:
        """Start with no run stores."""
        self._stores: dict[tuple[Path, str], FakeStateStore] = {}

    def __call__(self, project: Project, run_id: str) -> FakeStateStore:
        """Return the run's store, creating an empty one on first use."""
        # ``dict.setdefault`` is atomic, so concurrent first uses of one run agree
        # on a single store without a lock of our own.
        return self._stores.setdefault((project.root, run_id), FakeStateStore())


class LocalStateStore(StoreOperations):
    """Shared-filesystem store with flock and fsync/rename atomic publication.

    The filesystem must support cooperating flock users and atomic rename.
    Lock and document share portable run storage, never machine-local storage.
    Every mutation publishes record and lease in one document. Namespace writes
    fsync staging and namespace links through the existing project root for
    first-use durability. Ancestors outside the project need only traversal.
    """

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-901701 [PLR0913]; every parameter after run_id is keyword-only and independently optional.
        self,
        project: Project,
        run_id: str,
        *,
        fault_plan: Iterable[CommitFault] = (),
        lease_fault_plan: Iterable[CommitFault | None] = (),
        observation_fault_plan: Iterable[ObservationFault | None] = (),
        effects: AtomicWriteEffects | None = None,
    ) -> None:
        """Bind a validated Project run namespace, without decoding payloads."""
        # > The three fault plans are the store contract's public injection points and
        # > ``effects`` is the durability seam; a settings object would change every
        # > caller of Project.state_store for no added safety.
        super().__init__(fault_plan, lease_fault_plan, observation_fault_plan)
        self._effects = effects if effects is not None else LocalAtomicWriteEffects()
        # The document bytes this store knows are durable: the last it published
        # (every sync succeeded) or the last it reloaded and synchronized. A reload
        # of exactly these bytes has nothing left to synchronize; any other bytes,
        # or any publication that did not finish, make the next reload sync again.
        self._durable_source: bytes | None = None
        self._namespace = project.state.state_store_namespace(run_id)
        self._durable_root = project.root

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        directory = self._namespace.external_directory()
        descriptor = os.open(
            directory / "store.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def _read(self) -> StoreDocument:
        fault = next(self._observation_faults, None)
        if fault == ObservationFault.READ:
            message = "state-store observation read failed"
            raise OSError(message)
        directory = self._namespace.external_directory()
        source = self._namespace.read_bytes("store.json")
        if source is None:
            return StoreDocument()
        document = _decode_store_document(source, location=str(directory / "store.json"))
        # Reload resolves a lost durability acknowledgement: synchronize the
        # whole observed document before any caller can dispatch from it. Bytes
        # this store already made durable need no second synchronization.
        if fault != ObservationFault.SYNC and source == self._durable_source:
            return document
        self._effects.sync_existing_file(directory / "store.json")
        filesystem = _FaultObservationEffects() if fault == ObservationFault.SYNC else self._effects
        sync_directory_chain(directory, self._durable_root, effects=filesystem)
        self._durable_source = source
        return document

    def _write(self, document: StoreDocument) -> None:
        self._publish_document(document)

    def _write_record(self, document: StoreDocument, fault: CommitFault | None) -> None:
        effects = self._effects if fault is None else _FaultAtomicWriteEffects(fault)
        self._publish_document(document, effects=effects)

    def _publish_document(
        self, document: StoreDocument, *, effects: AtomicWriteEffects | None = None
    ) -> None:
        contents = document.model_dump_json().encode()
        try:
            self._namespace.write_bytes("store.json", contents, effects=effects or self._effects)
            self._durable_source = contents
        except ProjectStateError as exc:
            if isinstance(exc.__cause__, StateStoreWriteError):
                message = str(exc.__cause__)
                raise StateStoreWriteError(message) from exc
            if isinstance(exc.__cause__, OSError):
                message = str(exc)
                raise OSError(message) from exc
            raise
