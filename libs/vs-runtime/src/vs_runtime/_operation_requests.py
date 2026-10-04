"""Generic, crash-safe executor for registered operations, inspection and cancellation.

``execute`` is idempotent by request identity. It records an intent receipt, runs
the owner, validates the outcome against the declared schema and records a result
receipt. A restarted host that finds an intent without a result asks the owner to
inspect: it only repeats the effect when the owner proves it never happened, and
otherwise reports a typed Unknown. Nothing here performs an effect twice.

Inspection routes by target identity through the receipts. A target with no
receipt is reported Unknown, because this executor cannot tell whether the
target was ever started or belongs to another owner.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, assert_never

from pydantic import BaseModel, ValidationError

from vs_core.api import (
    CancelOwnedResource,
    ContractError,
    ExecuteRegisteredOperation,
    InspectRequest,
    Observation,
    ObservationStatus,
    RequestBase,
    RequestId,
    RequestObserved,
    ResourceId,
    SetupFailureKind,
    TargetObservation,
)
from vs_runtime._core_requests import ExecutionContext, ExecutionResult
from vs_runtime._observation_factory import ObservationFacts, ObservationSubject
from vs_runtime._operation_catalog import (
    Applied,
    CancellableOwner,
    Indeterminate,
    NotApplied,
    OperationCatalog,
    OperationEntry,
)
from vs_runtime._operation_receipts import IntentReceipt, ResultReceipt
from vs_runtime._receipt_store import (
    Conflict,
    Performed,
    Refused,
    Replayed,
    Settled,
    Transient,
    owner_key,
)

if TYPE_CHECKING:
    from vs_core.api import OperationRequest
    from vs_runtime._core_requests import OperationRoleRequest
    from vs_runtime._observation_factory import ObservationFactory
    from vs_runtime._operation_catalog import Inspection, OperationOwner
    from vs_runtime._operation_receipts import OperationReceipts


class RegisteredOperationRequests:
    """The OPERATIONS role: a closed catalog driven through durable receipts."""

    def __init__(
        self,
        catalog: OperationCatalog,
        receipts: OperationReceipts,
        observations: ObservationFactory,
    ) -> None:
        """Bind the validated catalog to its durable receipts and observation sequences."""
        self._catalog = catalog
        self._receipts = receipts
        self._observations = observations

    async def execute(
        self, request: OperationRoleRequest, context: ExecutionContext
    ) -> ExecutionResult:
        """Route one authorized request to its exact, closed handler."""
        match request:
            case ExecuteRegisteredOperation():
                return await self._execute(request, context)
            case InspectRequest():
                return await self._inspect(request, context)
            case CancelOwnedResource():
                return await self._cancel(request, context)
            case _:
                assert_never(request)

    def _observe(  # noqa: PLR0913  # lint-waiver: LW-410004 [PLR0913]; each argument is an independent fact of the observation.
        self,
        request: RequestBase,
        context: ExecutionContext,
        status: ObservationStatus,
        detail: str,
        *,
        own_effect: bool = True,
        subject: RequestId | None = None,
        resource_id: ResourceId | None = None,
    ) -> Observation:
        """Observation of *subject* (default: the request itself), in the request's episode.

        ``own_effect`` is true when a receipt proves the subject's own outcome. Only
        a proven terminal result claims acceptance and release, because Unknown never
        does. The sequence is per subject, so an Inspect of a target continues the
        target's own history.
        """
        terminal = own_effect and status is not ObservationStatus.UNKNOWN
        accepted = terminal and status in (ObservationStatus.SUCCEEDED, ObservationStatus.FAILED)
        return self._observations.observe(
            ObservationSubject.of(request, request_id=subject),
            ObservationFacts(
                status=status,
                terminal=terminal,
                accepted=accepted,
                released=accepted,
                children_complete=accepted,
                resource_id=resource_id,
                diagnostic=detail,
            ),
            observed_at=context.now_at,
        )

    def _result(
        self,
        request: RequestBase,
        context: ExecutionContext,
        status: ObservationStatus,
        detail: str,
    ) -> ExecutionResult:
        """A typed observation with no registered outcome. It records no receipt."""
        return ExecutionResult(
            observation=RequestObserved(
                observation=self._observe(
                    request,
                    context,
                    status,
                    detail,
                    own_effect=status is not ObservationStatus.UNKNOWN,
                ),
                setup_failure=_setup_failure(status),
            )
        )

    # execute

    async def _execute(
        self, request: ExecuteRegisteredOperation, context: ExecutionContext
    ) -> ExecutionResult:
        request_id = _identity(request)
        entry = self._catalog.find(request.operation.schema_ref)
        if entry is None:
            kind = request.operation.schema_ref.kind
            return self._result(
                request,
                context,
                ObservationStatus.REJECTED,
                f"operation {kind!r} is not in the catalog",
            )

        async def perform(*, resumed: bool) -> Settled[ResultReceipt] | Transient[ResultReceipt]:
            return await self._perform(request, context, entry, resumed=resumed)

        execution = await self._receipts.store.run_once(
            request_id.root,
            owner=owner_key(request),
            context=context,
            result_type=ResultReceipt,
            perform=perform,
        )
        match execution:
            case Replayed(receipt) | Performed(receipt):
                return self._replay(request, context, entry, receipt)
            case Conflict():
                return self._result(
                    request,
                    context,
                    ObservationStatus.REJECTED,
                    "same request identity with another payload",
                )
            case Refused(reason):
                return self._result(request, context, ObservationStatus.UNKNOWN, reason)
            case _:
                assert_never(execution)

    async def _perform(
        self,
        request: ExecuteRegisteredOperation,
        context: ExecutionContext,
        entry: OperationEntry,
        *,
        resumed: bool,
    ) -> Settled[ResultReceipt] | Transient[ResultReceipt]:
        """The effect, run under the store's begun marker. ``resumed`` means inspect first."""
        owner = entry.owner
        if owner is None:
            return Settled(
                self._receipt(
                    request,
                    context,
                    entry,
                    ObservationStatus.REJECTED,
                    f"declared but refused ({entry.refusal}): {entry.refusal_detail}",
                )
            )
        request_id = _identity(request)
        self._receipts.record_intent(
            IntentReceipt(
                request_id=request_id.root,
                payload_digest=context.payload_digest,
                operation=request.operation,
            )
        )
        decoded = self._catalog.registry.decode(request.operation)
        if resumed:
            match await _inspect(owner, decoded, context):
                case Applied(outcome):
                    return Settled(self._complete(request, context, entry, outcome))
                case Indeterminate(reason):
                    return Transient(
                        self._receipt(request, context, entry, ObservationStatus.UNKNOWN, reason)
                    )
                case NotApplied():
                    pass
        try:
            outcome = await owner.execute(decoded, context)
        except Exception as error:  # noqa: BLE001  # lint-waiver: LW-410001 [BLE001]; an owner failure after the begun marker leaves the effect unproven, and the contract is to report typed Unknown rather than halt or guess.
            return Transient(
                self._receipt(
                    request,
                    context,
                    entry,
                    ObservationStatus.UNKNOWN,
                    f"owner raised {type(error).__name__}: {error}",
                )
            )
        return Settled(self._complete(request, context, entry, outcome))

    def _complete(
        self,
        request: ExecuteRegisteredOperation,
        context: ExecutionContext,
        entry: OperationEntry,
        outcome: BaseModel | dict[str, object] | object,
    ) -> ResultReceipt:
        registration = entry.registration
        try:
            model = (
                outcome
                if isinstance(outcome, BaseModel)
                else registration.outcome_model.model_validate(outcome)
            )
            outcome_json = self._catalog.registry.encode_outcome(entry.schema, model)
        except (ValidationError, ContractError, TypeError) as error:
            return self._receipt(
                request,
                context,
                entry,
                ObservationStatus.FAILED,
                f"outcome violates declared schema {registration.descriptor.outcome_schema.name}: {error}",
            )
        return self._receipt(
            request, context, entry, ObservationStatus.SUCCEEDED, "", outcome_json=outcome_json
        )

    def _receipt(  # noqa: PLR0913  # lint-waiver: LW-410002 [PLR0913]; the receipt fields are independent facts of one result.
        self,
        request: ExecuteRegisteredOperation,
        context: ExecutionContext,
        entry: OperationEntry,
        status: ObservationStatus,
        detail: str,
        *,
        outcome_json: str | None = None,
    ) -> ResultReceipt:
        return ResultReceipt(
            request_id=_identity(request).root,
            payload_digest=context.payload_digest,
            schema_ref=entry.schema,
            status=status,
            outcome_json=outcome_json,
            detail=detail,
        )

    def _replay(
        self,
        request: ExecuteRegisteredOperation,
        context: ExecutionContext,
        entry: OperationEntry,
        receipt: ResultReceipt,
    ) -> ExecutionResult:
        """The one place a result becomes an observation, so replays are identical."""
        outcome = (
            None
            if receipt.outcome_json is None
            else self._catalog.registry.decode_outcome(entry.schema, receipt.outcome_json)
        )
        observed = RequestObserved(
            observation=self._observe(request, context, receipt.status, receipt.detail),
            operation_schema=entry.schema if outcome is not None else None,
            outcome_schema=entry.schema.outcome_schema if outcome is not None else None,
            outcome=outcome,
            setup_failure=_setup_failure(receipt.status),
        )
        return ExecutionResult(observation=self._catalog.registry.validate_event(observed))

    # inspect

    async def _inspect(self, request: InspectRequest, context: ExecutionContext) -> ExecutionResult:
        if request.resource_id is not None:
            return self._result(
                request,
                context,
                ObservationStatus.REJECTED,
                "registered operations own no child resources",
            )
        target = await self._target(request, context)
        observed = RequestObserved(
            observation=self._observe(request, context, ObservationStatus.SUCCEEDED, ""),
            target=target,
        )
        return ExecutionResult(observation=self._catalog.registry.validate_event(observed))

    async def _target(
        self, request: InspectRequest, context: ExecutionContext
    ) -> TargetObservation:
        target_id = request.target
        sealed = self._receipts.result(target_id.root)
        intent = self._receipts.intent(target_id.root)
        schema = (
            sealed.schema_ref
            if sealed is not None
            else intent.operation.schema_ref
            if intent is not None
            else None
        )
        entry = None if schema is None else self._catalog.find(schema)
        if schema is None or entry is None:
            return self._unknown(request, context, "no operation receipt for the target")
        if sealed is None:
            if entry.owner is None or intent is None:
                return self._unknown(
                    request, context, "target declared but refused, no result recorded"
                )
            decoded = self._catalog.registry.decode(intent.operation)
            match await _inspect(entry.owner, decoded, context):
                case Applied(outcome):
                    sealed = self._seal_inspected(target_id, intent, entry, outcome)
                case NotApplied():
                    sealed = ResultReceipt(
                        request_id=target_id.root,
                        payload_digest=intent.payload_digest,
                        schema_ref=entry.schema,
                        status=ObservationStatus.REJECTED,
                        detail="owner proved the effect did not happen",
                    )
                    self._receipts.record_result(sealed)
                case Indeterminate(reason):
                    return self._unknown(request, context, reason)
        outcome = (
            None
            if sealed.outcome_json is None
            else self._catalog.registry.decode_outcome(entry.schema, sealed.outcome_json)
        )
        return TargetObservation(
            observation=self._observe(
                request, context, sealed.status, sealed.detail, subject=target_id
            ),
            setup_failure=_setup_failure(sealed.status),
            operation_schema=entry.schema if outcome is not None else None,
            outcome_schema=entry.schema.outcome_schema if outcome is not None else None,
            outcome=outcome,
        )

    def _seal_inspected(
        self, target_id: RequestId, intent: IntentReceipt, entry: OperationEntry, outcome: object
    ) -> ResultReceipt:
        try:
            model = (
                outcome
                if isinstance(outcome, BaseModel)
                else entry.registration.outcome_model.model_validate(outcome)
            )
            receipt = ResultReceipt(
                request_id=target_id.root,
                payload_digest=intent.payload_digest,
                schema_ref=entry.schema,
                status=ObservationStatus.SUCCEEDED,
                outcome_json=self._catalog.registry.encode_outcome(entry.schema, model),
            )
        except (ValidationError, ContractError, TypeError) as error:
            receipt = ResultReceipt(
                request_id=target_id.root,
                payload_digest=intent.payload_digest,
                schema_ref=entry.schema,
                status=ObservationStatus.FAILED,
                detail=f"outcome violates declared schema: {error}",
            )
        self._receipts.record_result(receipt)
        return receipt

    def _unknown(
        self, request: InspectRequest | CancelOwnedResource, context: ExecutionContext, detail: str
    ) -> TargetObservation:
        return TargetObservation(
            observation=self._observe(
                request,
                context,
                ObservationStatus.UNKNOWN,
                detail,
                own_effect=False,
                subject=request.target,
            )
        )

    # cancel

    async def _cancel(
        self, request: CancelOwnedResource, context: ExecutionContext
    ) -> ExecutionResult:
        request_id = _identity(request)
        intent = self._receipts.intent(request.target.root)
        entry = None if intent is None else self._catalog.find(intent.operation.schema_ref)
        if intent is None or entry is None:
            return self._result(
                request,
                context,
                ObservationStatus.UNKNOWN,
                "no operation receipt for the cancel target",
            )
        target_entry, operation = entry, intent.operation

        async def perform(*, resumed: bool) -> Settled[ResultReceipt] | Transient[ResultReceipt]:
            del resumed  # cancellation is idempotent by contract, so a resume repeats it
            owner = target_entry.owner
            if not isinstance(owner, CancellableOwner):
                return Settled(
                    self._cancel_receipt(
                        request,
                        context,
                        target_entry,
                        ObservationStatus.REJECTED,
                        f"operation {target_entry.schema.kind!r} declares no cancellation",
                    )
                )
            try:
                cancelled = await owner.cancel(self._catalog.registry.decode(operation), context)
            except Exception as error:  # noqa: BLE001  # lint-waiver: LW-410003 [BLE001]; a failed cancel leaves the resource owned, so it is reported Unknown and the caller retries.
                return Transient(
                    self._cancel_receipt(
                        request,
                        context,
                        target_entry,
                        ObservationStatus.UNKNOWN,
                        f"owner raised {type(error).__name__}: {error}",
                    )
                )
            return Settled(
                self._cancel_receipt(
                    request, context, target_entry, ObservationStatus.CANCELLED, cancelled.detail
                )
            )

        execution = await self._receipts.store.run_once(
            request_id.root,
            owner=owner_key(request),
            context=context,
            result_type=ResultReceipt,
            perform=perform,
        )
        match execution:
            case Replayed(receipt) | Performed(receipt):
                return self._cancel_result(request, context, receipt.status, receipt.detail)
            case Conflict():
                return self._result(
                    request,
                    context,
                    ObservationStatus.REJECTED,
                    "same request identity with another payload",
                )
            case Refused(reason):
                return self._result(request, context, ObservationStatus.UNKNOWN, reason)
            case _:
                assert_never(execution)

    def _cancel_receipt(
        self,
        request: CancelOwnedResource,
        context: ExecutionContext,
        entry: OperationEntry,
        status: ObservationStatus,
        detail: str,
    ) -> ResultReceipt:
        return ResultReceipt(
            request_id=_identity(request).root,
            payload_digest=context.payload_digest,
            schema_ref=entry.schema,
            status=status,
            detail=detail,
        )

    def _cancel_result(
        self,
        request: CancelOwnedResource,
        context: ExecutionContext,
        status: ObservationStatus,
        detail: str,
    ) -> ExecutionResult:
        """Cancellation is acknowledged with the resource named and never marked released."""
        observation = self._observe(
            request, context, status, detail, resource_id=request.resource_id
        )
        return ExecutionResult(observation=RequestObserved(observation=observation))


async def _inspect(
    owner: OperationOwner, request: OperationRequest, context: ExecutionContext
) -> Inspection:
    """Owner inspection, where a failure to inspect is itself an indeterminate answer."""
    try:
        return await owner.inspect(request, context)
    except Exception as error:  # noqa: BLE001  # lint-waiver: LW-410008 [BLE001]; an inspection that cannot run proves nothing, so it is reported indeterminate instead of halting recovery.
        return Indeterminate(f"owner inspection raised {type(error).__name__}: {error}")


def _identity(request: RequestBase) -> RequestId:
    if request.request_id is None:
        raise ContractError(("request_id",), "canonical identity required")
    return request.request_id


def _setup_failure(status: ObservationStatus) -> SetupFailureKind:
    del status
    return SetupFailureKind.UNKNOWN
