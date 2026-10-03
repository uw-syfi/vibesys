"""Credential-free JSON lifecycle store with atomic writes and process locking."""

# TRY003 is suppressed on handle/schema boundary ValueErrors: the message names
# the offending contract field/path. A generic error factory would obscure it.

from __future__ import annotations

import asyncio
import fcntl
from typing import TYPE_CHECKING, TypeVar

from pydantic import ValidationError

from vs_evaluation.coordinator import (
    EvaluationKeyConflictError,
    RevisionConflictError,
    stable_handle_id,
)
from vs_evaluation.models import EvaluationRequest, EvaluationState, StoredEvaluation
from vs_project.api import atomic_write_bytes

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

T = TypeVar("T")
_HANDLE_ID_LENGTH = len("eval_") + 64
_TERMINAL = frozenset(
    {
        EvaluationState.SUCCEEDED,
        EvaluationState.FAILED,
        EvaluationState.CANCELED,
        EvaluationState.SUPERSEDED,
    }
)


class EvaluationStoreCorruptionError(RuntimeError):
    """A persisted record is malformed or inconsistent with its path."""

    def __init__(self, path: Path, cause: Exception | None = None) -> None:
        """Name the corrupted record path and retain validation failure."""
        super().__init__(f"invalid evaluation record at {path}")
        if cause is not None:
            self.__cause__ = cause


class FilesystemEvaluationStore:
    """Store lifecycle records as atomic JSON replacements in one directory.

    One advisory lock file serializes claims and compare-and-set updates across
    processes. Records are keyed by a hash of the caller's idempotency key, so
    raw keys and credentials never appear in filenames.
    """

    def __init__(self, root: Path) -> None:
        """Create the storage directory if needed and retain its path."""
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)
        self._lock_path = self._root / ".lock"

    async def claim(self, request: EvaluationRequest, *, handle_id: str) -> StoredEvaluation:
        """Atomically insert an accepted record or return its existing owner."""
        if stable_handle_id(request.key) != handle_id:
            raise ValueError("handle_id does not match request key")  # noqa: TRY003  # lint-waiver: LW-930012 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.

        def claim_locked() -> StoredEvaluation:
            path = self._record_path(handle_id)
            existing = self._read_path(path)
            if existing is not None:
                if existing.request != request or existing.handle_id != handle_id:
                    raise EvaluationKeyConflictError(request.key)
                return existing
            record = StoredEvaluation(
                handle_id=handle_id,
                request=request,
                state=EvaluationState.QUEUED,
                revision=0,
                submission_pending=True,
            )
            self._write_atomic(path, record)
            return record

        return await self._run_locked(claim_locked)

    async def get(self, handle_id: str) -> StoredEvaluation | None:
        """Read and validate a record by stable handle ID."""
        path = self._record_path(handle_id)
        return await self._run_locked(lambda: self._read_path(path))

    async def get_by_key(self, key: str) -> StoredEvaluation | None:
        """Read the record owned by an idempotency key."""
        return await self.get(stable_handle_id(key))

    async def compare_and_set(
        self, record: StoredEvaluation, *, expected_revision: int
    ) -> StoredEvaluation:
        """Atomically replace a record if its revision is unchanged."""

        def write_locked() -> StoredEvaluation:
            path = self._record_path(record.handle_id)
            current = self._read_path(path)
            if current is None:
                raise KeyError(record.handle_id)
            if current.revision != expected_revision:
                raise RevisionConflictError(record.handle_id, current.revision, expected_revision)
            if record.revision != expected_revision + 1:
                raise ValueError("record revision must increment by one")  # noqa: TRY003  # lint-waiver: LW-930013 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.
            if stable_handle_id(record.request.key) != record.handle_id:
                raise ValueError("record handle_id does not match request key")  # noqa: TRY003  # lint-waiver: LW-930014 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.
            self._write_atomic(path, record)
            return record

        return await self._run_locked(write_locked)

    async def records(self) -> tuple[StoredEvaluation, ...]:
        """Return all durable records in stable handle order."""

        def read_locked() -> tuple[StoredEvaluation, ...]:
            return tuple(
                record
                for path in sorted(self._root.glob("eval_*.json"))
                if (record := self._read_path(path)) is not None
            )

        return await self._run_locked(read_locked)

    async def nonterminal(self) -> tuple[StoredEvaluation, ...]:
        """Return all durable records that need lifecycle reconciliation."""
        return tuple(record for record in await self.records() if record.state not in _TERMINAL)

    async def _run_locked(self, operation: Callable[[], T]) -> T:
        """Run blocking filesystem and flock operations off the event loop."""
        return await asyncio.to_thread(lambda: self._locked(operation))

    def _locked(self, operation: Callable[[], T]) -> T:
        with self._lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                return operation()
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _record_path(self, handle_id: str) -> Path:
        if len(handle_id) != _HANDLE_ID_LENGTH or not handle_id.startswith("eval_"):
            raise ValueError("invalid evaluation handle ID")  # noqa: TRY003  # lint-waiver: LW-930015 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.
        if any(character not in "0123456789abcdef" for character in handle_id[5:]):
            raise ValueError("invalid evaluation handle ID")  # noqa: TRY003  # lint-waiver: LW-930016 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.
        return self._root / f"{handle_id}.json"

    @staticmethod
    def _read_path(path: Path) -> StoredEvaluation | None:
        try:
            contents = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        try:
            record = StoredEvaluation.model_validate_json(contents)
        except ValidationError as error:
            raise EvaluationStoreCorruptionError(path, error) from error
        if path.name != f"{record.handle_id}.json":
            raise EvaluationStoreCorruptionError(path)
        if stable_handle_id(record.request.key) != record.handle_id:
            raise EvaluationStoreCorruptionError(path)
        return record

    @staticmethod
    def _write_atomic(path: Path, record: StoredEvaluation) -> None:
        atomic_write_bytes(path, record.model_dump_json().encode("utf-8"))


__all__ = ["EvaluationStoreCorruptionError", "FilesystemEvaluationStore"]
