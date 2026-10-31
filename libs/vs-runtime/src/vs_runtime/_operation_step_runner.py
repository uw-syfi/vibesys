"""A step that is a registered operation: its owner runs again, after what it left is cleared.

An operation runs in this process, so nothing of it outlives the host except its effect, and
the effect is what a rerun must not repeat. Each operation therefore declares a ``RerunPolicy``
when it is given to the runner, and the runner refuses one that declares none or one that does
not fit its lifecycle:

- ``ReadOnly``: the owner reads and writes nothing, so ``clear`` has nothing to remove.
- ``KeyedWrite``: the owner's write is keyed, and its ``inspect`` reads the key back. ``clear``
  asks it, so a write that landed is adopted with its outcome and one that did not is run again.
- ``ClearedWork``: the owner runs a command or leaves files; ``clear`` hands the tag to a
  ``WorkCleaner`` that kills its process group and resets the workspace.

Operations that own a job or a turn (``OWNED_JOB``, ``SESSION_TURN``) are not this runner's:
their runners are the evaluation and agent-turn runners.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from vs_core.api import (
    ContractError,
    ExecuteRegisteredOperation,
    HostFence,
    HostId,
    LifecycleClass,
    ObservationStatus,
    RequestObserved,
    Until,
)
from vs_core.api.recovery import Cancelled, Finished
from vs_runtime._core_identity import core_identity
from vs_runtime._core_requests import ExecutionContext
from vs_runtime._observation_factory import ObservationFacts, ObservationSubject
from vs_runtime._operation_catalog import Applied, Indeterminate, NotApplied
from vs_runtime._step_runner import Adopted, Cleared, ClearUnavailableError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from vs_core.api import Observation, OperationRequest, Request, RequestId
    from vs_core.api.recovery import CancelReason, StepResult
    from vs_runtime._observation_factory import ObservationFactory
    from vs_runtime._operation_catalog import OperationCatalog, OperationEntry
    from vs_runtime._receipt_store import ReceiptStore
    from vs_runtime._step_runner import CancelSignal
    from vs_sim.api import Clock


@dataclass(frozen=True)
class ReadOnly:
    """The owner changes nothing, so a rerun needs no clear."""


@dataclass(frozen=True)
class KeyedWrite:
    """The owner's write carries a key its ``inspect`` reads back before a rerun writes."""

    key: str
    """Names the key: what the tracker or store checks before it writes."""

    def __post_init__(self) -> None:
        """A write with no named key is not keyed."""
        if not self.key.strip():
            raise ContractError(("rerun", "key"), "a keyed write names its key")


class WorkCleaner(Protocol):
    """Removes what an interrupted attempt of an operation left in the world."""

    async def clear(self, tag: RequestId, request: OperationRequest) -> None:
        """Kill the processes of ``tag`` and reset what it wrote; return once none remain.

        Raises ``ClearUnavailableError`` when the process table or the workspace cannot be
        reached.
        """
        ...


@dataclass(frozen=True)
class ClearedWork:
    """The owner runs processes or writes files, so a rerun is safe only after the cleaner ran."""

    cleaner: WorkCleaner


type RerunPolicy = ReadOnly | KeyedWrite | ClearedWork

_FITS: Mapping[LifecycleClass, tuple[type, ...]] = {
    LifecycleClass.QUERY: (ReadOnly, ClearedWork),
    LifecycleClass.IDEMPOTENT_WRITE: (KeyedWrite, ClearedWork),
}
_MARKS = "step-operations"
_NO_FENCE = HostFence(host_id=HostId(root="step-runner"), epoch=0)
"""``ExecutionContext`` still requires a host fence; the run's lock is the only fence now."""


class OperationMark(BaseModel):
    """The request an attempt began, kept so ``clear`` can find its operation from the tag."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    request: ExecuteRegisteredOperation


class OperationStepRunner:
    """Runs ``ExecuteRegisteredOperation`` steps on the owners of an operation catalog."""

    def __init__(
        self,
        catalog: OperationCatalog,
        policies: Mapping[str, RerunPolicy],
        store: ReceiptStore,
        observations: ObservationFactory,
        *,
        clock: Clock,
    ) -> None:
        """Bind the catalog to the rerun policy of each of its owned operations.

        Raises ``ContractError`` naming the operation kind when an owned operation has no
        policy, has one its lifecycle does not allow, or when a policy names an operation
        the catalog does not own.
        """
        _check_policies(catalog, policies)
        self._catalog = catalog
        self._policies = dict(policies)
        self._store = store
        self._observations = observations
        self._clock = clock

    async def run(
        self, step: Request, tag: RequestId, attempt: int, cancel: CancelSignal
    ) -> StepResult:
        """Run the operation's owner, or return the outcome a clear found it already wrote.

        An exception of the owner is "no result": it propagates, and the loop clears and
        runs the next attempt.
        """
        del attempt  # the owner is found by the tag, so no attempt number names its work
        request = _operation(step, tag)
        entry = self._catalog.find(request.operation.schema_ref)
        if entry is None or entry.owner is None:
            return self._refused(request, entry)
        begun = self._begin(tag, request)
        if begun:
            match await self.clear(tag):
                case Adopted(result) if result is not None:
                    return result
        decoded = self._catalog.registry.decode(request.operation)
        context = self._context(request)
        work = asyncio.ensure_future(entry.owner.execute(decoded, context))
        stop = asyncio.ensure_future(cancel.wait())
        try:
            await asyncio.wait({work, stop}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop.cancel()
            if not work.done():
                work.cancel()
            await asyncio.gather(work, stop, return_exceptions=True)
        if work.cancelled():
            await self._clean(tag, request)
            return Cancelled(reason=_why(cancel))
        return self._finished(request, entry, work.result())

    async def clear(self, tag: RequestId) -> Adopted | Cleared:
        """Adopt the outcome of a write that landed, or remove what the attempt left."""
        mark = self._mark(tag)
        if mark is None:
            return Cleared()
        request = mark.request
        entry = self._catalog.find(request.operation.schema_ref)
        policy = None if entry is None else self._policies.get(entry.schema.kind)
        match policy:
            case KeyedWrite():
                return await self._adopt_written(tag, request)
            case ClearedWork(cleaner):
                await cleaner.clear(tag, self._catalog.registry.decode(request.operation))
                return Cleared()
            case _:
                return Cleared()

    def _mark(self, tag: RequestId) -> OperationMark | None:
        """The request an earlier attempt began; a state directory that cannot be read proves nothing."""
        try:
            return self._store.load(_MARKS, "mark", tag.root, OperationMark)
        except OSError as error:
            raise ClearUnavailableError(tag, "the run's state directory") from error

    def _begin(self, tag: RequestId, request: ExecuteRegisteredOperation) -> bool:
        """Record the request before any effect; say whether an earlier attempt already did."""
        known = self._mark(tag) is not None
        self._store.replace(_MARKS, "mark", tag.root, OperationMark(request=request))
        return known

    async def _adopt_written(
        self, tag: RequestId, request: ExecuteRegisteredOperation
    ) -> Adopted | Cleared:
        entry = self._catalog.find(request.operation.schema_ref)
        if entry is None or entry.owner is None:
            return Cleared()
        decoded = self._catalog.registry.decode(request.operation)
        try:
            found = await entry.owner.inspect(decoded, self._context(request))
        except Exception as error:
            raise ClearUnavailableError(tag, entry.schema.kind) from error
        match found:
            case Applied(outcome):
                return Adopted(self._finished(request, entry, outcome))
            case NotApplied():
                return Cleared()
            case Indeterminate():
                raise ClearUnavailableError(tag, entry.schema.kind)

    async def _clean(self, tag: RequestId, request: ExecuteRegisteredOperation) -> None:
        """Remove what a cancelled attempt left, so a returned ``Cancelled`` leaves nothing."""
        entry = self._catalog.find(request.operation.schema_ref)
        policy = None if entry is None else self._policies.get(entry.schema.kind)
        if isinstance(policy, ClearedWork):
            await policy.cleaner.clear(tag, self._catalog.registry.decode(request.operation))

    def _context(self, request: ExecuteRegisteredOperation) -> ExecutionContext:
        now = self._clock.now()
        return ExecutionContext(
            fence=_NO_FENCE,
            now_at=now,
            payload_digest=hashlib.sha256(request.model_dump_json().encode()).hexdigest(),
            until=Until(at=now),
        )

    def _finished(
        self, request: ExecuteRegisteredOperation, entry: OperationEntry, outcome: object
    ) -> Finished:
        """The result for what the owner returned, checked against the operation's declared schema."""
        registry = self._catalog.registry
        try:
            model = (
                outcome
                if isinstance(outcome, BaseModel)
                else entry.registration.outcome_model.model_validate(outcome)
            )
            outcome_json = registry.encode_outcome(entry.schema, model)
        except (ValidationError, ContractError, TypeError) as error:
            facts = _facts(
                ObservationStatus.FAILED,
                f"outcome violates declared schema {entry.schema.outcome_schema.name}: {error}",
            )
            return self._report(request, facts)
        observed = RequestObserved(
            observation=self._observe(request, _facts(ObservationStatus.SUCCEEDED, "")),
            operation_schema=entry.schema,
            outcome_schema=entry.schema.outcome_schema,
            outcome=registry.decode_outcome(entry.schema, outcome_json),
        )
        report = registry.validate_event(observed)
        return Finished(observation=report.observation, observed=report)

    def _refused(
        self, request: ExecuteRegisteredOperation, entry: OperationEntry | None
    ) -> Finished:
        if entry is None:
            kind = request.operation.schema_ref.kind
            detail = f"operation {kind!r} is not in the catalog"
        else:
            detail = f"declared but refused ({entry.refusal}): {entry.refusal_detail}"
        return self._report(request, _facts(ObservationStatus.REJECTED, detail))

    def _report(self, request: ExecuteRegisteredOperation, facts: ObservationFacts) -> Finished:
        return Finished(observation=self._observe(request, facts))

    def _observe(self, request: ExecuteRegisteredOperation, facts: ObservationFacts) -> Observation:
        return self._observations.observe(
            ObservationSubject.of(request), facts, observed_at=self._clock.now()
        )


def _facts(status: ObservationStatus, detail: str) -> ObservationFacts:
    accepted = status in (ObservationStatus.SUCCEEDED, ObservationStatus.FAILED)
    if status is ObservationStatus.REJECTED:
        return ObservationFacts.refused(detail)
    return ObservationFacts(
        status=status,
        accepted=accepted,
        released=accepted,
        children_complete=accepted,
        diagnostic=detail,
    )


def _operation(step: Request, tag: RequestId) -> ExecuteRegisteredOperation:
    if not isinstance(step, ExecuteRegisteredOperation):
        kind = type(step).__name__
        raise ContractError(("step", str(tag)), f"not a registered operation: {kind}")
    if core_identity(step) != tag:
        raise ContractError(("step", str(tag)), "the tag is not the step's request id")
    return step


def _why(cancel: CancelSignal) -> CancelReason:
    reason = cancel.reason
    if reason is None:
        message = "the owner was cancelled without a cancel signal"
        raise RuntimeError(message)
    return reason


def _check_policies(catalog: OperationCatalog, policies: Mapping[str, RerunPolicy]) -> None:
    owned = {entry.schema.kind: entry for entry in catalog.entries if entry.owner is not None}
    for kind in sorted(set(policies) - set(owned)):
        raise ContractError(("rerun", kind), "policy for an operation the catalog does not own")
    for kind, entry in owned.items():
        policy = policies.get(kind)
        lifecycle = entry.schema.lifecycle
        if policy is None:
            raise ContractError(("rerun", kind), "an owned operation declares its rerun policy")
        if not isinstance(policy, _FITS.get(lifecycle, ())):
            raise ContractError(
                ("rerun", kind),
                f"{type(policy).__name__} does not fit a {lifecycle} operation",
            )
