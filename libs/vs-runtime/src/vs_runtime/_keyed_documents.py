"""The crash-safe keyed document store behind the runtime's executors.

A receipt is one atomically written file keyed by (family, part, key) below a Project state
namespace. Every read-modify-write runs under an exclusive ``flock`` on one lock file in that
namespace, so two processes cannot both begin a request, both advance a counter, or both claim a
holder. The store keeps documents (workspace bindings and receipts, access receipts, the
evaluation job registry, the evidence ledger, observation sequences). It no longer decides
whether an effect runs: the step loop owns that, and an executor repeats an effect through
``ExecutionContext.repeat``.
"""

from __future__ import annotations

import fcntl
import hashlib
from contextlib import contextmanager
from typing import TYPE_CHECKING

from pydantic import BaseModel

from vs_core.api import ContractError
from vs_project.api import StateDocumentDamagedError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from vs_core.api import RequestBase
    from vs_project.api import StateNamespace
    from vs_sim.api import Threads


class DocumentCorruptError(Exception):
    """A stored receipt cannot be read back; an executor reports Unknown."""


class KeyedDocuments:
    """Atomic, cross-process receipt files below one state namespace."""

    def __init__(self, namespace: StateNamespace, threads: Threads) -> None:
        """Bind to the namespace that holds this run's receipts; ``threads`` makes its lock."""
        self._namespace = namespace
        self._lock_path = namespace.external_directory() / "receipts.lock"
        self._guard = threads.rlock()
        self._depth = 0

    @staticmethod
    def _path(family: str, part: str, key: str) -> str:
        return f"{family}/{hashlib.sha256(key.encode()).hexdigest()}.{part}.json"

    @contextmanager
    def exclusive(self, *, blocking: bool = True) -> Iterator[None]:
        """Hold cross-process exclusion; nonblocking admission raises BlockingIOError."""
        if not self._guard.acquire(blocking=blocking):
            problem = "receipt store is owned by another thread"
            raise BlockingIOError(problem)
        try:
            if self._depth:
                self._depth += 1
                try:
                    yield
                finally:
                    self._depth -= 1
                return
            with self._lock_path.open("a") as stream:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
                self._depth = 1
                try:
                    yield
                finally:
                    self._depth = 0
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            self._guard.release()

    def load[ReceiptT: BaseModel](
        self, family: str, part: str, key: str, model: type[ReceiptT]
    ) -> ReceiptT | None:
        """The recorded receipt, or None when nothing was recorded.

        Raises ``DocumentCorruptError`` when a file exists but fails to decode. Other
        project-state errors (unreadable file, invalid path) are not corruption and propagate.
        """
        try:
            return self._namespace.load_optional(self._path(family, part, key), model)
        except StateDocumentDamagedError as error:
            message = f"{family} {part} receipt is unreadable"
            raise DocumentCorruptError(message) from error

    def record_once(self, family: str, part: str, key: str, receipt: BaseModel) -> None:
        """Durably record the receipt; identical replays pass, a different payload conflicts."""
        with self.exclusive():
            prior = self.load(family, part, key, type(receipt))
            if prior == receipt:
                return
            if prior is not None:
                raise ContractError(("request_id",), f"same {part} identity with another payload")
            self._namespace.save(self._path(family, part, key), receipt)

    def replace(self, family: str, part: str, key: str, receipt: BaseModel) -> None:
        """Durably overwrite a record that is defined to change over time."""
        with self.exclusive():
            self._namespace.save(self._path(family, part, key), receipt)

    def modify[ReceiptT: BaseModel, ResultT](
        self,
        family: str,
        part: str,
        key: str,
        model: type[ReceiptT],
        decide: Callable[[ReceiptT | None], tuple[ReceiptT | None, ResultT]],
    ) -> ResultT:
        """Atomically read, decide and write one record.

        ``decide`` receives the stored record (or None) and returns the record to
        store (None keeps the stored one) with the value to return.
        """
        with self.exclusive():
            stored, result = decide(self.load(family, part, key, model))
            if stored is not None:
                self._namespace.save(self._path(family, part, key), stored)
            return result


def owner_key(request: RequestBase) -> str:
    """The fence owner of a request: its scope's owner and generation."""
    return f"{request.scope.owner.kind}:{request.scope.owner.root}:{request.scope.generation}"


__all__ = ["DocumentCorruptError", "KeyedDocuments", "owner_key"]
