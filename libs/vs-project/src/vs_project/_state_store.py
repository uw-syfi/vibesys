"""Memory and local-filesystem readers of a legacy run's stored record."""

from __future__ import annotations

import copy
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
from vs_project._store_document import (
    LEGACY_STORE_DOCUMENT_VERSION,
    STORE_DOCUMENT_VERSION,
    STORE_FILE,
    StoreDocument,
    StoreReader,
)
from vs_project.api.state_store import ObservationFault

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from pathlib import Path

    from vs_project._state_io import StoreDurabilityEffects
    from vs_project.api.state_store import StoreRecord
    from vs_project.project import Project


def _decode_store_document(source: bytes) -> StoreDocument:
    """Decode ``store.json`` in this or the older format; damage, or a newer one, is a typed error."""
    return decode_state_document(
        StoreDocument,
        source,
        source=STORE_FILE,
        versioned_by=(
            "version",
            range(LEGACY_STORE_DOCUMENT_VERSION, STORE_DOCUMENT_VERSION + 1),
        ),
    )


class _FaultObservationEffects(LocalAtomicWriteEffects):
    """Fail directory synchronization at the filesystem observation boundary."""

    def sync_directory(self, directory: Path) -> None:
        message = f"state-store observation synchronization failed: {directory}"
        raise OSError(message)


class FakeStateStore(StoreReader):
    """In-memory reader of one stored document, with deterministic read faults.

    A test puts the document there with ``replace_document``.
    """

    def __init__(self, *, observation_fault_plan: Iterable[ObservationFault | None] = ()) -> None:
        """Create an empty store with a deterministic plan of read faults."""
        super().__init__(observation_fault_plan)
        self._contents: bytes | None = None
        self._lock = RLock()

    def __deepcopy__(self, memo: dict[int, object]) -> FakeStateStore:
        """An independent store holding the same document and the same place in its fault plan.

        The lock is the one attribute that is not data: the copy gets a new lock of its own kind.
        """
        clone = type(self).__new__(type(self))
        memo[id(self)] = clone
        for name, value in vars(self).items():
            copied: object = type(value)() if name == "_lock" else copy.deepcopy(value, memo)
            setattr(clone, name, copied)
        return clone

    def replace_document(self, contents: bytes) -> None:
        """Replace the stored document's bytes, including deliberately damaged ones."""
        with self._lock:
            self._contents = contents

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            yield

    def observe(self) -> StoreRecord | None:
        """Inspect the stored bytes without joining a transaction."""
        source = self._contents
        document = None if source is None else _decode_store_document(source)
        return None if document is None else document.record

    def _read_record(self) -> StoreDocument | None:
        source = self._contents
        fault = self._observation(exists=source is not None)
        if fault is not None:
            message = f"state-store observation {fault} failed"
            raise OSError(message)
        return None if source is None else _decode_store_document(source)


class LocalStateStore(StoreReader):
    """Shared-filesystem reader with flock and fsync of what it reads.

    The filesystem must support cooperating flock users. Lock and document share portable
    run storage, never machine-local storage. A read synchronizes the document and the
    namespace links it observed through the existing project root, as the writer that
    published it may not have: nothing is read as durable that was not made durable.
    Ancestors outside the project need only traversal.
    """

    def __init__(
        self,
        project: Project,
        run_id: str,
        *,
        observation_fault_plan: Iterable[ObservationFault | None] = (),
        effects: StoreDurabilityEffects | None = None,
    ) -> None:
        """Bind a validated Project run namespace, without decoding the document."""
        super().__init__(observation_fault_plan)
        self._effects = effects if effects is not None else LocalAtomicWriteEffects()
        # The bytes of the document this store has already synchronized: a reload of exactly
        # these bytes has nothing left to synchronize.
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

    def observe(self) -> StoreRecord | None:
        """Inspect the stored bytes without the lock or durability repair."""
        source = self._namespace.read_bytes(STORE_FILE)
        document = None if source is None else _decode_store_document(source)
        return None if document is None else document.record

    def _read_record(self) -> StoreDocument | None:
        directory = self._namespace.external_directory()
        source = self._namespace.read_bytes(STORE_FILE)
        fault = self._observation(exists=source is not None)
        if fault == ObservationFault.READ:
            message = "state-store observation read failed"
            raise OSError(message)
        if source is None:
            return None
        if fault != ObservationFault.SYNC and source == self._durable_source:
            return _decode_store_document(source)
        self._effects.sync_existing_file(directory / STORE_FILE)
        filesystem = _FaultObservationEffects() if fault == ObservationFault.SYNC else self._effects
        sync_directory_chain(directory, self._durable_root, effects=filesystem)
        self._durable_source = source
        return _decode_store_document(source)
