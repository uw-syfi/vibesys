"""Memory and crash-atomic local implementations of the StateStore role."""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from threading import RLock
from typing import TYPE_CHECKING

from vs_project._state_io import LocalAtomicWriteEffects, sync_directory_chain
from vs_project._store_operations import StoreDocument, StoreOperations
from vs_project.api.state_store import CommitFault, ObservationFault, StateStoreWriteError
from vs_project.errors import ProjectStateError

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from pathlib import Path

    from vs_project._state_io import AtomicWriteEffects, AtomicWriteStream
    from vs_project.project import Project


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
        self._document = StoreDocument()
        self._lock = RLock()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            yield

    def _read(self) -> StoreDocument:
        fault = next(self._observation_faults, None)
        if fault == ObservationFault.READ or (
            fault == ObservationFault.SYNC and self._document != StoreDocument()
        ):
            message = f"state-store observation {fault} failed"
            raise OSError(message)
        return self._document

    def _write(self, document: StoreDocument) -> None:
        self._document = StoreDocument.model_validate_json(document.model_dump_json())


class LocalStateStore(StoreOperations):
    """Shared-filesystem store with flock and fsync/rename atomic publication.

    The filesystem must support cooperating flock users and atomic rename.
    Lock and document share portable run storage, never machine-local storage.
    Every mutation publishes record and lease in one document. Namespace writes
    fsync staging and namespace links through the existing project root for
    first-use durability. Ancestors outside the project need only traversal.
    """

    def __init__(
        self,
        project: Project,
        run_id: str,
        *,
        fault_plan: Iterable[CommitFault] = (),
        lease_fault_plan: Iterable[CommitFault | None] = (),
        observation_fault_plan: Iterable[ObservationFault | None] = (),
    ) -> None:
        """Bind a validated Project run namespace, without decoding payloads."""
        super().__init__(fault_plan, lease_fault_plan, observation_fault_plan)
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
        source = self._namespace.read_bytes("store.json")
        if source is None:
            return StoreDocument()
        document = StoreDocument.model_validate_json(source)
        # Reload resolves a lost durability acknowledgement: synchronize the
        # whole observed document before any caller can dispatch from it.
        directory = self._namespace.external_directory()
        descriptor = os.open(directory / "store.json", os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        filesystem = (
            _FaultObservationEffects()
            if fault == ObservationFault.SYNC
            else LocalAtomicWriteEffects()
        )
        sync_directory_chain(directory, self._durable_root, effects=filesystem)
        return document

    def _write(self, document: StoreDocument) -> None:
        self._publish_document(document)

    def _write_record(self, document: StoreDocument, fault: CommitFault | None) -> None:
        effects = None if fault is None else _FaultAtomicWriteEffects(fault)
        self._publish_document(document, effects=effects)

    def _publish_document(
        self, document: StoreDocument, *, effects: AtomicWriteEffects | None = None
    ) -> None:
        try:
            self._namespace.write_bytes(
                "store.json", document.model_dump_json().encode(), effects=effects
            )
        except ProjectStateError as exc:
            if isinstance(exc.__cause__, OSError):
                message = str(exc)
                raise OSError(message) from exc
            raise
