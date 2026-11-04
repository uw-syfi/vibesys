"""Scoped file admission ownership with durable protobuf receipts.

Target occupancy is the unresolved receipts in this store. Releasing the
process locks does not release an intent that completion or absence has not
proven.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import stat
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from vs_project._admission_rules import (
    checked_receipt,
    compare,
    initial_receipt,
    reconcile,
    record_intent,
    reject_occupied_targets,
    settle,
)
from vs_project._state_io import LocalAtomicWriteEffects, atomic_write_bytes, sync_directory_chain
from vs_project.api.admissions import AdmissionBusyError
from vs_proto.api import admission, validate

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from vs_project._state_io import AtomicWriteEffects
    from vs_project.api.admissions import AdmissionClaim


def _subdirectories(path: Path) -> tuple[Path, ...]:
    """Child directories of an admission root. A symlink is damage, not a root."""
    if not path.is_dir():
        return ()
    found: list[Path] = []
    for child in path.iterdir():
        if child.is_symlink():
            message = f"admission path is a symlink: {child}"
            raise ValueError(message)
        if child.is_dir():
            found.append(child)
    return tuple(found)


def normalized_targets(targets: tuple[str, ...]) -> tuple[str, ...]:
    """Path targets have one spelling before hashing and lock ordering."""
    if not targets:
        message = "admission targets must not be empty"
        raise ValueError(message)
    normalized = tuple(sorted({str(Path(target).expanduser().resolve()) for target in targets}))
    if any(not target or "\x00" in target for target in targets):
        message = "invalid admission target"
        raise ValueError(message)
    return normalized


class _Claim:
    def __init__(
        self, receipt: admission.AdmissionReceipt, persist: Callable[[bytes], None]
    ) -> None:
        self._receipt = checked_receipt(receipt.to_binary())
        self._persist = persist
        self.active = True

    def _require_owner(self) -> None:
        if not self.active:
            message = "admission claim is closed"
            raise RuntimeError(message)

    def observe(self) -> admission.AdmissionReceipt:
        self._require_owner()
        return checked_receipt(self._receipt.to_binary())

    def reconcile(self, observation: admission.PublicationObservation) -> None:
        self._require_owner()
        self._publish(reconcile(self._receipt, observation))

    def _publish(self, receipt: admission.AdmissionReceipt) -> None:
        self._require_owner()
        payload = receipt.to_binary()
        owned = checked_receipt(payload)
        try:
            self._persist(payload)
        except BaseException:
            self.active = False
            raise
        self._receipt = owned

    def record_intent(self, evidence: admission.PublicationEvidence) -> None:
        self._require_owner()
        self._publish(record_intent(self._receipt, evidence))

    def settle(self, status: admission.AdmissionStatus, result: admission.OperationResult) -> None:
        self._require_owner()
        self._publish(settle(self._receipt, status, result))


class FileAdmissionStore:
    """Host-user receipts, never expired; lock failure admits no effect.

    The identity lock is acquired first, followed by sorted normalized target
    locks. Nonblocking exclusion reports busy, avoiding unbounded lock waits.
    Atomic replacement syncs both receipt bytes and their containing directory.
    """

    def __init__(
        self,
        root: Path,
        *,
        effects: AtomicWriteEffects | None = None,
        durable_root: Path | None = None,
    ) -> None:
        """Open the Project-owned admission root, with injectable durability effects."""
        self._root = root.resolve()
        self._durable_root = (durable_root or root.parent).resolve()
        self._root.relative_to(self._durable_root)
        if not self._durable_root.is_dir():
            message = f"admission durable root does not exist: {self._durable_root}"
            raise ValueError(message)
        self._effects = effects or LocalAtomicWriteEffects()

    def _path(self, identity: admission.AdmissionIdentity) -> Path:
        validate(identity)
        return self._root / str(identity.operation_id) / identity.request_id / "receipt.pb"

    @contextmanager
    def _lock(self, path: Path) -> Iterator[None]:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NONBLOCK | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "r+b") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                message = f"admission lock is not a regular file: {path}"
                raise ValueError(message)
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                message = f"admission is owned: {path.name}"
                raise AdmissionBusyError(message) from error
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _read_receipt(self, path: Path) -> admission.AdmissionReceipt:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                message = f"admission receipt is not a regular file: {path}"
                raise ValueError(message)
            return checked_receipt(stream.read((4 << 20) + 1))

    def lookup(self, identity: admission.AdmissionIdentity) -> admission.AdmissionReceipt | None:
        """Read a complete atomic publication, treating damage as an error."""
        path = self._path(identity)
        try:
            receipt = self._read_receipt(path)
        except FileNotFoundError:
            return None
        if receipt.identity != identity:
            message = "admission receipt identity mismatch"
            raise ValueError(message)
        return receipt

    def _durable_receipts(self) -> tuple[admission.AdmissionReceipt, ...]:
        """Every persisted receipt. Occupancy is derived from these, not a side index."""
        receipts: list[admission.AdmissionReceipt] = []
        for operation in _subdirectories(self._root):
            if operation.name == "targets":
                continue
            for request in _subdirectories(operation):
                path = request / "receipt.pb"
                if path.is_symlink():
                    message = f"admission receipt is not a regular file: {path}"
                    raise ValueError(message)
                if not path.exists():
                    continue
                receipts.append(self._read_receipt(path))
        return tuple(receipts)

    @contextmanager
    def claim(
        self,
        identity: admission.AdmissionIdentity,
        targets: tuple[str, ...],
        accepted_request: admission.AcceptedOperation,
    ) -> Iterator[AdmissionClaim]:
        """Hold exclusion through publication; reopening never authorizes redispatch."""
        targets = normalized_targets(targets)
        requested = initial_receipt(identity, targets, accepted_request)
        path = self._path(identity)
        with ExitStack() as locks:
            locks.enter_context(self._lock(path.with_suffix(".lock")))
            for target in targets:
                key = hashlib.sha256(os.fsencode(target)).hexdigest()
                locks.enter_context(self._lock(self._root / "targets" / (key + ".lock")))
            reject_occupied_targets(self._durable_receipts(), identity, targets)
            receipt = self.lookup(identity)
            if receipt is None:
                receipt = requested
                self._persist(path, receipt.to_binary())
            else:
                compare(receipt, requested)
            claim = _Claim(receipt, lambda payload: self._persist(path, payload))
            try:
                yield claim
            finally:
                claim.active = False

    def _persist(self, path: Path, payload: bytes) -> None:
        atomic_write_bytes(path, payload, effects=self._effects)
        sync_directory_chain(path.parent, self._durable_root, effects=self._effects)
