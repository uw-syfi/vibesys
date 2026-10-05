"""The one crash-safe receipt store behind every runtime request executor.

A receipt is one atomically written file keyed by (family, part, key) below a
Project state namespace. Every read-modify-write runs under an exclusive
``flock`` on one lock file in that namespace, so two processes cannot both
begin a request, both advance a counter, or both claim a holder.

``run_once`` is the single execution rule: an executor supplies only the effect.
The store replays a sealed result, rejects the same request identity with another
payload, refuses to start an effect when host authority is not held (the lease
must verify and the host fence must be the newest seen for the owner), writes a
``begun`` marker before the effect so a restart knows an effect may have started,
and seals the result only while authority still holds. A restarted effect is told
it is resuming, so it inspects instead of repeating.
"""

from __future__ import annotations

import fcntl
import hashlib
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from vs_core.api import ContractError, HostFence
from vs_project.api import ProjectStateError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from vs_core.api import RequestBase
    from vs_project.api import StateNamespace
    from vs_runtime._core_requests import ExecutionContext


class ReceiptCorruptError(Exception):
    """A stored receipt cannot be read back; an executor reports Unknown."""


class ExecutionPhase(StrEnum):
    """How far one request's effect progressed."""

    BEGUN = "begun"
    DONE = "done"


class ExecutionRecord(BaseModel):
    """Durable intent (``begun``) or sealed result (``done``) of one request."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    payload_digest: str
    phase: ExecutionPhase
    result_json: str | None = None
    result_type: str | None = None


@dataclass(frozen=True)
class NeverBegun:
    """No execution record exists: the store holds no trace that the effect began.

    ``run_once`` writes the begun marker before the effect, so for a kind that runs
    on it this is positive proof that the effect never started.
    """


@dataclass(frozen=True)
class BegunUnsealed:
    """The effect may have started and has no sealed result yet."""

    result_type: str | None


@dataclass(frozen=True)
class SealedExecution:
    """The sealed result of the request, still encoded as ``result_type``."""

    result_type: str | None
    payload_digest: str
    result_json: str


type ExecutionHistory = NeverBegun | BegunUnsealed | SealedExecution


def result_type_name(model: type[BaseModel]) -> str:
    """The tag a record carries so an inspection can decode its result without its executor."""
    return f"{model.__module__}.{model.__qualname__}"


@dataclass(frozen=True)
class Settled[ResultT: BaseModel]:
    """The effect reached a terminal result: seal it for replay."""

    result: ResultT


@dataclass(frozen=True)
class Transient[ResultT: BaseModel]:
    """The effect has no final answer yet (Unknown, retryable failure): never sealed."""

    result: ResultT


@dataclass(frozen=True)
class Replayed[ResultT: BaseModel]:
    """A sealed result of an earlier execution, returned without any effect."""

    result: ResultT


@dataclass(frozen=True)
class Performed[ResultT: BaseModel]:
    """The result of the effect that ran now; sealed only if it was ``Settled``."""

    result: ResultT


@dataclass(frozen=True)
class Conflict:
    """The request identity was recorded for another payload; no effect ran."""


@dataclass(frozen=True)
class Declined:
    """No effect ran: host authority is not held, or a receipt is unreadable."""

    reason: str


class Performer[ResultT: BaseModel](Protocol):
    """The effect of one request, told whether an earlier host may have run it."""

    async def __call__(self, *, resumed: bool) -> Settled[ResultT] | Transient[ResultT]: ...


type Execution[ResultT: BaseModel] = Replayed[ResultT] | Performed[ResultT] | Conflict | Declined


class ReceiptStore:
    """Atomic, cross-process receipt files below one state namespace."""

    _EXECUTIONS = "executions"
    _FENCES = "fences"

    def __init__(self, namespace: StateNamespace) -> None:
        """Bind to the namespace that holds this run's receipts."""
        self._namespace = namespace
        self._lock_path = namespace.external_directory() / "receipts.lock"
        self._guard = threading.RLock()
        self._depth = 0

    @staticmethod
    def _path(family: str, part: str, key: str) -> str:
        return f"{family}/{hashlib.sha256(key.encode()).hexdigest()}.{part}.json"

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        """Hold the cross-process lock; reentrant within one store instance."""
        with self._guard:
            if self._depth:
                self._depth += 1
                try:
                    yield
                finally:
                    self._depth -= 1
                return
            with self._lock_path.open("a") as stream:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                self._depth = 1
                try:
                    yield
                finally:
                    self._depth = 0
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def load[ReceiptT: BaseModel](
        self, family: str, part: str, key: str, model: type[ReceiptT]
    ) -> ReceiptT | None:
        """The recorded receipt, or None when nothing was recorded.

        Raises ``ReceiptCorruptError`` when a file exists but cannot be read back.
        """
        try:
            return self._namespace.load_optional(self._path(family, part, key), model)
        except (ValidationError, ProjectStateError) as error:
            message = f"{family} {part} receipt is unreadable"
            raise ReceiptCorruptError(message) from error

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

    def sealed[ResultT: BaseModel](
        self, key: str, result_type: type[ResultT]
    ) -> tuple[str, ResultT] | None:
        """The payload digest and sealed result of request *key*, or None before it is sealed."""
        record = self.load(self._EXECUTIONS, "execution", key, ExecutionRecord)
        if record is None or record.phase is not ExecutionPhase.DONE:
            return None
        return record.payload_digest, _decode(record, result_type)

    def history(self, key: str) -> ExecutionHistory:
        """What the store recorded about request *key*: never begun, begun, or sealed.

        This is the one answer to "did this request's effect happen?" for every kind
        that runs on ``run_once``. Raises ``ReceiptCorruptError`` for an unreadable record.
        """
        record = self.load(self._EXECUTIONS, "execution", key, ExecutionRecord)
        if record is None:
            return NeverBegun()
        if record.phase is not ExecutionPhase.DONE:
            return BegunUnsealed(record.result_type)
        if record.result_json is None:
            message = "sealed execution receipt has no result"
            raise ReceiptCorruptError(message)
        return SealedExecution(record.result_type, record.payload_digest, record.result_json)

    def seal(self, key: str, digest: str, result: BaseModel) -> None:
        """Seal *result* for request *key* outside ``run_once`` (an inspection that proved it).

        Sealing the identical result again is a no-op; another result or payload
        for a sealed identity raises ``ContractError``.
        """
        sealed = ExecutionRecord(
            payload_digest=digest,
            phase=ExecutionPhase.DONE,
            result_json=result.model_dump_json(),
            result_type=result_type_name(type(result)),
        )
        with self.exclusive():
            prior = self.load(self._EXECUTIONS, "execution", key, ExecutionRecord)
            if prior == sealed:
                return
            if prior is not None and (
                prior.phase is ExecutionPhase.DONE or prior.payload_digest != digest
            ):
                raise ContractError(("request_id",), "same request identity with another payload")
            self._namespace.save(self._path(self._EXECUTIONS, "execution", key), sealed)

    def authorize(self, owner: str, context: ExecutionContext) -> str | None:
        """Why this host may not perform an effect for *owner* now, or None when it may.

        The lease must verify at the current time, and the host fence must be the
        newest seen for the owner: a lower epoch, or the same epoch from another
        host, is a stale host. The newest fence is recorded.
        """
        if context.lease is None or not context.lease.verify(now_at=context.now_at):
            return "host authority is not held"
        fence = context.fence

        def decide(stored: HostFence | None) -> tuple[HostFence | None, bool]:
            if stored is not None and (
                fence.epoch < stored.epoch
                or (fence.epoch == stored.epoch and fence.host_id != stored.host_id)
            ):
                return None, False
            return (None if stored == fence else fence), True

        if not self.modify(self._FENCES, "fence", owner, HostFence, decide):
            return "host fence is older than the newest seen"
        return None

    async def run_once[ResultT: BaseModel](
        self,
        key: str,
        *,
        owner: str,
        context: ExecutionContext,
        result_type: type[ResultT],
        perform: Performer[ResultT],
    ) -> Execution[ResultT]:
        """Run *perform* at most once per request identity ``key``.

        ``perform`` receives ``resumed``: true when an earlier host began this
        request and may have finished its effect, so the effect must inspect before
        it repeats anything. A ``Settled`` result is sealed only while authority
        still holds; a ``Transient`` one is returned unsealed, and the next attempt
        resumes from the ``begun`` marker.
        """
        try:
            start = self._start(key, owner, context, result_type)
            if not isinstance(start, bool):
                return start
            settled = await perform(resumed=start)
            if isinstance(settled, Transient):
                return Performed(settled.result)
            reason = self.authorize(owner, context)
            if reason is not None:
                return Declined(f"{reason} before the result was recorded")
            self.replace(
                self._EXECUTIONS,
                "execution",
                key,
                ExecutionRecord(
                    payload_digest=context.payload_digest,
                    phase=ExecutionPhase.DONE,
                    result_json=settled.result.model_dump_json(),
                    result_type=result_type_name(result_type),
                ),
            )
            return Performed(settled.result)
        except ReceiptCorruptError as error:
            return Declined(str(error))

    def _start[ResultT: BaseModel](
        self, key: str, owner: str, context: ExecutionContext, result_type: type[ResultT]
    ) -> Replayed[ResultT] | Conflict | Declined | bool:
        """Replay, conflict or refusal; else write the begun marker and say if it was resumed."""
        sealed = self.load(self._EXECUTIONS, "execution", key, ExecutionRecord)
        if sealed is not None and sealed.payload_digest != context.payload_digest:
            return Conflict()
        if sealed is not None and sealed.phase is ExecutionPhase.DONE:
            return Replayed(_decode(sealed, result_type))
        reason = self.authorize(owner, context)
        if reason is not None:
            return Declined(reason)
        begun = ExecutionRecord(
            payload_digest=context.payload_digest,
            phase=ExecutionPhase.BEGUN,
            result_type=result_type_name(result_type),
        )

        def begin(stored: ExecutionRecord | None) -> tuple[ExecutionRecord | None, bool]:
            return (begun if stored is None else None), stored is not None

        return self.modify(self._EXECUTIONS, "execution", key, ExecutionRecord, begin)


def owner_key(request: RequestBase) -> str:
    """The fence owner of a request: its scope's owner and generation."""
    return f"{request.scope.owner.kind}:{request.scope.owner.root}:{request.scope.generation}"


def _decode[ResultT: BaseModel](record: ExecutionRecord, model: type[ResultT]) -> ResultT:
    if record.result_json is None:
        message = "sealed execution receipt has no result"
        raise ReceiptCorruptError(message)
    try:
        return model.model_validate_json(record.result_json)
    except ValidationError as error:
        message = "sealed execution result is unreadable"
        raise ReceiptCorruptError(message) from error


__all__ = [
    "BegunUnsealed",
    "Conflict",
    "Declined",
    "Execution",
    "ExecutionHistory",
    "ExecutionPhase",
    "ExecutionRecord",
    "NeverBegun",
    "Performed",
    "Performer",
    "ReceiptCorruptError",
    "ReceiptStore",
    "Replayed",
    "SealedExecution",
    "Settled",
    "Transient",
    "owner_key",
    "result_type_name",
]
