"""Memory and crash-atomic local implementations of the StateStore role."""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from threading import RLock
from typing import TYPE_CHECKING

from vs_project._state_io import LocalAtomicWriteEffects
from vs_project._store_operations import StoreDocument, StoreOperations
from vs_project.api.state_store import CommitFault
from vs_project.errors import ProjectStateError

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from pathlib import Path

    from vs_project._state_io import AtomicWriteEffects
    from vs_project.project import Project


class _FaultAtomicWriteEffects(LocalAtomicWriteEffects):
    """Lose publication acknowledgement at an actual atomic-rename boundary."""

    def __init__(self, fault: CommitFault) -> None:
        self._fault = fault

    def replace(self, temporary: Path, destination: Path) -> None:
        if self._fault == CommitFault.UNKNOWN_BEFORE:
            message = "state-store acknowledgement lost before rename"
            raise OSError(message)
        super().replace(temporary, destination)
        message = "state-store acknowledgement lost after rename"
        raise OSError(message)


class FakeStateStore(StoreOperations):
    """In-memory faithful store with deterministic public commit faults."""

    def __init__(self, *, fault_plan: Iterable[CommitFault] = ()) -> None:
        """Create one store shared by every simulated host using this object."""
        super().__init__(fault_plan)
        self._document = StoreDocument()
        self._lock = RLock()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            yield

    def _read(self) -> StoreDocument:
        return self._document

    def _write(self, document: StoreDocument) -> None:
        self._document = StoreDocument.model_validate_json(document.model_dump_json())


class LocalStateStore(StoreOperations):
    """Shared-filesystem store with flock and fsync/rename atomic publication.

    The filesystem must support cooperating flock users and atomic rename.
    Lock and document share portable run storage, never machine-local storage.
    Every mutation publishes record and lease in one document. Namespace writes
    fsync staging, replacement directory and ancestors for first-use durability.
    """

    def __init__(
        self, project: Project, run_id: str, *, fault_plan: Iterable[CommitFault] = ()
    ) -> None:
        """Bind a validated Project run namespace, without decoding payloads."""
        super().__init__(fault_plan)
        self._namespace = project.state.state_store_namespace(run_id)

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
        filesystem = LocalAtomicWriteEffects()
        for parent in (directory, *directory.parents):
            filesystem.sync_directory(parent)
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
