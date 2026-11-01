"""A step that is a registered operation: its owner runs again, after what it left is cleared.

An operation runs in this process, so nothing of it outlives the host except its effect, and
the effect is what a rerun must not repeat. Each owned operation therefore declares a
``RerunPolicy`` when its owner enters the catalog (``vs_runtime._rerun_policy``), and the entry
refuses one that does not fit its lifecycle. The runner follows the entry's policy: a read-only
owner has nothing to clear, a keyed write is adopted when ``inspect`` reads it back, and a
command's work is handed to a ``WorkCleaner``.

Operations that own a job or a turn (``OWNED_JOB``, ``SESSION_TURN``) are not this runner's:
their runners are the evaluation and agent-turn runners, and their entries declare
``OtherRunner``. A step of one is refused by name.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from pydantic import BaseModel, ValidationError

from vs_core.api import (
    ContractError,
    ExecuteRegisteredOperation,
    ObservationStatus,
    RequestObserved,
    Until,
)
from vs_core.api.recovery import Finished
from vs_runtime._cancel_wait import cancelled_for, finished_before_cancel
from vs_runtime._core_identity import core_identity
from vs_runtime._core_requests import ExecutionContext, payload_digest_of
from vs_runtime._observation_factory import ObservationFacts, ObservationSubject
from vs_runtime._operation_catalog import Applied, Indeterminate, NotApplied
from vs_runtime._rerun_policy import ClearedWork, KeyedWrite, OtherRunner, ReadOnly
from vs_runtime._step_runner import Adopted, Cleared, ClearUnavailableError

if TYPE_CHECKING:
    from vs_core.api import Observation, Request, RequestId
    from vs_core.api.recovery import StepResult
    from vs_runtime._observation_factory import ObservationFactory
    from vs_runtime._operation_catalog import OperationCatalog, OperationEntry, OperationOwner
    from vs_runtime._step_runner import CancelSignal
    from vs_sim.api import Clock


class OperationStepRunner:
    """Runs ``ExecuteRegisteredOperation`` steps on the owners of an operation catalog."""

    def __init__(
        self, catalog: OperationCatalog, observations: ObservationFactory, *, clock: Clock
    ) -> None:
        """Run the owners of *catalog*, each under the rerun policy its entry declares."""
        self._catalog = catalog
        self._observations = observations
        self._clock = clock

    async def run(
        self,
        step: Request,
        tag: RequestId,
        attempt: int,
        deadline_at: float,
        cancel: CancelSignal,
    ) -> StepResult:
        """Run the operation's owner, or return the outcome a clear found it already wrote.

        An exception of the owner is "no result": it propagates, and the loop clears and
        runs the next attempt. A step of an operation another runner owns is refused by name.
        """
        request = _operation(step, tag)
        entry = self._catalog.find(request.operation.schema_ref)
        if entry is None or entry.owner is None:
            return self._refused(request, entry)
        _policy_of(entry)
        if attempt > 1:
            match await self.clear(step, tag):
                case Adopted(result) if result is not None:
                    return result
        decoded = self._catalog.registry.decode(request.operation)
        work = asyncio.ensure_future(
            entry.owner.execute(decoded, self._context(request, deadline_at, repeat=attempt > 1))
        )
        if not await finished_before_cancel(work, cancel):
            await self._clean(tag, request)
            return cancelled_for(cancel)
        return self._finished(request, entry, work.result())

    async def clear(self, step: Request, tag: RequestId) -> Adopted | Cleared:
        """Adopt the outcome of a write that landed, or remove what the attempt left."""
        request = _operation(step, tag)
        entry = self._catalog.find(request.operation.schema_ref)
        if entry is None or entry.owner is None:
            return Cleared()
        match _policy_of(entry):
            case KeyedWrite():
                return await self._adopt_written(tag, request, entry, entry.owner)
            case ClearedWork(cleaner):
                await cleaner.clear(tag, self._catalog.registry.decode(request.operation))
                return Cleared()
            case ReadOnly():
                return Cleared()

    async def _adopt_written(
        self,
        tag: RequestId,
        request: ExecuteRegisteredOperation,
        entry: OperationEntry,
        owner: OperationOwner,
    ) -> Adopted | Cleared:
        """Ask the owner whether the write landed; a write it cannot read back proves nothing."""
        decoded = self._catalog.registry.decode(request.operation)
        try:
            found = await owner.inspect(decoded, self._context(request, self._clock.now()))
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
        policy = None if entry is None else entry.rerun
        if isinstance(policy, ClearedWork):
            await policy.cleaner.clear(tag, self._catalog.registry.decode(request.operation))

    def _context(
        self, request: ExecuteRegisteredOperation, until_at: float, *, repeat: bool = False
    ) -> ExecutionContext:
        return ExecutionContext(
            now_at=self._clock.now(),
            payload_digest=payload_digest_of(request),
            until=Until(at=until_at),
            repeat=repeat,
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


def _policy_of(entry: OperationEntry) -> ReadOnly | KeyedWrite | ClearedWork:
    """The rerun policy this runner follows; an operation another runner owns is refused by name."""
    policy = entry.rerun
    if policy is None or isinstance(policy, OtherRunner):
        raise ContractError(("rerun", entry.schema.kind), "an operation another runner owns")
    return policy
