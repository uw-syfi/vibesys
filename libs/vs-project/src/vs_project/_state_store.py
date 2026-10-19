"""Memory and crash-atomic local implementations of the StateStore role."""

from __future__ import annotations

import copy
import fcntl
import os
from contextlib import contextmanager
from threading import RLock
from typing import TYPE_CHECKING

from vs_project._decode_memo import DecodeMemo
from vs_project._state_io import (
    LocalAtomicWriteEffects,
    decode_state_document,
    sync_directory_chain,
)
from vs_project._store_operations import (
    LEASE_DOCUMENT_VERSION,
    LEGACY_STORE_DOCUMENT_VERSION,
    STORE_DOCUMENT_VERSION,
    LeaseDocument,
    StoreDocument,
    StoreFile,
    StoreOperations,
)
from vs_project.api.state_store import CommitFault, ObservationFault, StateStoreWriteError
from vs_project.errors import ProjectStateError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator
    from pathlib import Path

    from vs_project._state_io import AtomicWriteEffects, AtomicWriteStream, StoreDurabilityEffects
    from vs_project.project import Project


def _decode_store_document(source: bytes) -> StoreDocument:
    """Decode ``store.json`` in this or the older format; damage, or a newer one, is a typed error."""
    return decode_state_document(
        StoreDocument,
        source,
        source=StoreFile.RECORD,
        versioned_by=(
            "version",
            range(LEGACY_STORE_DOCUMENT_VERSION, STORE_DOCUMENT_VERSION + 1),
        ),
    )


def _decode_lease_document(source: bytes) -> LeaseDocument:
    """Decode ``lease.json``; damage, or a newer release's version, is a typed error."""
    return decode_state_document(
        LeaseDocument,
        source,
        source=StoreFile.LEASE,
        versioned_by=("version", LEASE_DOCUMENT_VERSION),
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
        self._contents: dict[StoreFile, bytes] = {}
        # Every operation reads its documents, so decoding them each time would cost the size
        # of the run's record on every call; each memo decodes only bytes it has not seen.
        self._records = DecodeMemo(_decode_store_document)
        self._leases = DecodeMemo(_decode_lease_document)
        self._lock = RLock()

    def __deepcopy__(self, memo: dict[int, object]) -> FakeStateStore:
        """An independent store holding the same documents and the same place in its fault plans.

        The lock is the one attribute that is not data: the copy gets a new lock of its own kind.
        The memos hold only frozen documents keyed on immutable bytes, so sharing them is safe.
        """
        clone = type(self).__new__(type(self))
        memo[id(self)] = clone
        for name, value in vars(self).items():
            if name == "_lock":
                copied: object = type(value)()
            elif name in {"_records", "_leases"}:
                copied = value
            else:
                copied = copy.deepcopy(value, memo)
            setattr(clone, name, copied)
        return clone

    def replace_document(self, contents: bytes, *, file: StoreFile = StoreFile.RECORD) -> None:
        """Replace one stored document's bytes, including deliberately damaged ones."""
        with self._lock:
            self._contents[file] = contents

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            yield

    def _read_record(self) -> StoreDocument | None:
        source = self._read_file(StoreFile.RECORD)
        return None if source is None else self._records.decode(source)

    def _read_lease(self) -> LeaseDocument | None:
        source = self._read_file(StoreFile.LEASE)
        return None if source is None else self._leases.decode(source)

    def _read_file(self, file: StoreFile) -> bytes | None:
        contents = self._contents.get(file)
        fault = self._observation(exists=contents is not None)
        if fault is not None:
            message = f"state-store observation {fault} failed"
            raise OSError(message)
        return contents

    def _write(self, file: StoreFile, document: StoreDocument | LeaseDocument) -> None:
        contents = document.encode()
        self._contents[file] = contents
        if isinstance(document, StoreDocument):
            self._records.remember(contents, document)
        else:
            self._leases.remember(contents, document)


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
    A commit publishes the record, then the lease's time watermark; lease operations
    publish only the small lease document. Namespace writes
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
        effects: StoreDurabilityEffects | None = None,
        on_decode: Callable[[], None] | None = None,
    ) -> None:
        """Bind a validated Project run namespace, without decoding payloads.

        ``on_decode`` observes each document decode, which runs once per distinct stored bytes.
        """
        # > The three fault plans are the store contract's public injection points and
        # > ``effects`` is the durability seam; a settings object would change every
        # > caller of Project.state_store for no added safety.
        super().__init__(fault_plan, lease_fault_plan, observation_fault_plan)
        self._effects = effects if effects is not None else LocalAtomicWriteEffects()
        # The bytes of each document this store knows are durable: the last it published
        # (every sync succeeded) or the last it reloaded and synchronized. A reload of exactly
        # these bytes has nothing left to synchronize; any other bytes, or any publication
        # that did not finish, make the next reload sync again.
        self._durable_source: dict[StoreFile, bytes] = {}
        self._records = DecodeMemo(_decode_store_document, on_decode)
        self._leases = DecodeMemo(_decode_lease_document, on_decode)
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

    def _read_record(self) -> StoreDocument | None:
        source = self._read_file(StoreFile.RECORD)
        return None if source is None else self._records.decode(source)

    def _read_lease(self) -> LeaseDocument | None:
        source = self._read_file(StoreFile.LEASE)
        return None if source is None else self._leases.decode(source)

    def _read_file(self, file: StoreFile) -> bytes | None:
        directory = self._namespace.external_directory()
        source = self._namespace.read_bytes(file)
        fault = self._observation(exists=source is not None)
        if fault == ObservationFault.READ:
            message = "state-store observation read failed"
            raise OSError(message)
        if source is None:
            return None
        # Reload resolves a lost durability acknowledgement: synchronize the
        # whole observed document before any caller can dispatch from it. Bytes
        # this store already made durable need no second synchronization.
        if fault != ObservationFault.SYNC and source == self._durable_source.get(file):
            return source
        self._effects.sync_existing_file(directory / file)
        filesystem = _FaultObservationEffects() if fault == ObservationFault.SYNC else self._effects
        sync_directory_chain(directory, self._durable_root, effects=filesystem)
        self._durable_source[file] = source
        return source

    def _write(self, file: StoreFile, document: StoreDocument | LeaseDocument) -> None:
        self._publish_document(file, document)

    def _write_document(
        self, file: StoreFile, document: StoreDocument | LeaseDocument, fault: CommitFault | None
    ) -> None:
        effects = self._effects if fault is None else _FaultAtomicWriteEffects(fault)
        self._publish_document(file, document, effects=effects)

    def _publish_document(
        self,
        file: StoreFile,
        document: StoreDocument | LeaseDocument,
        *,
        effects: AtomicWriteEffects | None = None,
    ) -> None:
        contents = document.encode()
        try:
            self._namespace.write_bytes(file, contents, effects=effects or self._effects)
            self._durable_source[file] = contents
            if isinstance(document, StoreDocument):
                self._records.remember(contents, document)
            else:
                self._leases.remember(contents, document)
        except ProjectStateError as exc:
            if isinstance(exc.__cause__, StateStoreWriteError):
                message = str(exc.__cause__)
                raise StateStoreWriteError(message) from exc
            if isinstance(exc.__cause__, OSError):
                message = str(exc)
                raise OSError(message) from exc
            raise
