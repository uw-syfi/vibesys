"""Durable, cross-process receipts for the workspace request executors.

All files live in one machine-local ``StateNamespace`` opened through
``vs-project``'s ``Project`` (``Project.local_namespace``), so the layout and the
atomic, directory-synced writes belong to ``vs-project``. Every read-modify-write
that decides between two hosts runs under an exclusive ``flock`` on a lock file
in that namespace, so two processes cannot both begin one request or both hold the
exclusive root.
"""

from __future__ import annotations

import fcntl
import hashlib
from contextlib import contextmanager
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from vs_core.api import AttemptRef, HostFence, RequestId, ResourceId, RevisionRef, WorkspaceMode
from vs_project.api import ProjectStateError
from vs_runtime._core_requests import ExecutionResult

if TYPE_CHECKING:
    from collections.abc import Iterator

    from vs_project.api import StateNamespace


class ReceiptCorruptError(Exception):
    """A stored receipt cannot be read back; the executor reports Unknown."""


class ReceiptPhase(StrEnum):
    """How far one request's side effect progressed."""

    BEGUN = "begun"
    DONE = "done"


class ExecutionRecord(BaseModel):
    """Durable intent or result of one request, bound to its payload digest."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    payload_digest: str
    phase: ReceiptPhase
    result: ExecutionResult | None = None


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
    """Durable receipts, bindings, fences and revision ownership."""

    def check_fence(self, owner: str, fence: HostFence) -> bool:
        """Record *fence* as the newest for *owner*; ``False`` if it is stale."""
        ...

    def begin_execution(
        self, request_id: RequestId, record: ExecutionRecord
    ) -> ExecutionRecord | None:
        """Create the intent if absent (``None``), else return what is stored."""
        ...

    def load_execution(self, request_id: RequestId) -> ExecutionRecord | None: ...

    def save_execution(self, request_id: RequestId, record: ExecutionRecord) -> None: ...

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


def _name(identity: str) -> str:
    return hashlib.sha256(identity.encode()).hexdigest() + ".json"


class NamespaceWorkspaceReceipts:
    """Receipts stored in a ``Project`` local namespace."""

    def __init__(self, namespace: StateNamespace) -> None:
        self._namespace = namespace
        self._lock_path = namespace.external_directory() / "receipts.lock"

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        with self._lock_path.open("a") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _read[Model: BaseModel](self, path: str, model: type[Model]) -> Model | None:
        try:
            raw = self._namespace.read_bytes(path)
            return None if raw is None else model.model_validate_json(raw)
        except (ValidationError, ProjectStateError) as error:
            message = f"workspace receipt {path} is unreadable"
            raise ReceiptCorruptError(message) from error

    def _write(self, path: str, model: BaseModel) -> None:
        self._namespace.write_bytes(path, model.model_dump_json().encode())

    def check_fence(self, owner: str, fence: HostFence) -> bool:
        path = f"fences/{_name(owner)}"
        with self._exclusive():
            stored = self._read(path, HostFence)
            if stored is not None and (
                fence.epoch < stored.epoch
                or (fence.epoch == stored.epoch and fence.host_id != stored.host_id)
            ):
                return False
            if stored != fence:
                self._write(path, fence)
            return True

    def begin_execution(
        self, request_id: RequestId, record: ExecutionRecord
    ) -> ExecutionRecord | None:
        path = f"executions/{_name(request_id.root)}"
        with self._exclusive():
            stored = self._read(path, ExecutionRecord)
            if stored is None:
                self._write(path, record)
            return stored

    def load_execution(self, request_id: RequestId) -> ExecutionRecord | None:
        return self._read(f"executions/{_name(request_id.root)}", ExecutionRecord)

    def save_execution(self, request_id: RequestId, record: ExecutionRecord) -> None:
        with self._exclusive():
            self._write(f"executions/{_name(request_id.root)}", record)

    def load_binding(self, attempt: AttemptRef) -> AttemptBinding | None:
        return self._read(f"bindings/{_name(attempt_key(attempt))}", AttemptBinding)

    def bind(self, attempt: AttemptRef, binding: AttemptBinding) -> AttemptBinding:
        path = f"bindings/{_name(attempt_key(attempt))}"
        with self._exclusive():
            stored = self._read(path, AttemptBinding)
            if stored is not None:
                return stored
            self._write(path, binding)
            return binding

    def acquire_root(self, attempt: AttemptRef) -> RootGrant:
        with self._exclusive():
            holder = self._read("root-holder.json", AttemptRef)
            if holder is not None:
                if holder.attempt_id != attempt.attempt_id:
                    return RootGrant.HELD
                if attempt.generation < holder.generation:
                    return RootGrant.SUPERSEDED
                if attempt.generation == holder.generation:
                    return RootGrant.GRANTED
            self._write("root-holder.json", attempt)
            return RootGrant.GRANTED

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
        return self._read("root-holder.json", AttemptRef)

    def record_revision(self, commit: str, owner: str) -> None:
        self._namespace.write_bytes(f"revisions/{commit}/{_name(owner)}", owner.encode())

    def revision_owners(self, commit: str) -> frozenset[str]:
        owners: set[str] = set()
        for entry in self._namespace.entries(f"revisions/{commit}"):
            raw = self._namespace.read_bytes(f"revisions/{commit}/{entry}")
            if raw is not None:
                owners.add(raw.decode())
        return frozenset(owners)

    def mark_released(self, attempt: AttemptRef) -> None:
        self._namespace.write_bytes(f"released/{_name(attempt_key(attempt))}", b"released")

    def is_released(self, attempt: AttemptRef) -> bool:
        return self._namespace.read_bytes(f"released/{_name(attempt_key(attempt))}") is not None
