"""Generic, crash-safe executor for registered operations, inspection and cancellation.

``execute`` is idempotent by request identity. It records a record receipt, runs
the owner, validates the outcome against the declared schema and records a result
receipt. A restarted host that finds a record without a result asks the owner to
inspect: it only repeats the effect when the owner proves it never happened, and
otherwise reports a typed Unknown. Nothing here performs an effect twice.

``InspectRequest`` is answered by the generic ``RecordedRequestInspector`` over
the shared store, for every request kind. This executor registers itself as the
probe for operation results, because only it can decode an operation outcome and
re-observe an operation's external effect through its owner.

An inspection that would prove an operation never ran first closes the operation's effect
fence (``vs_runtime._effect_fence``), which every delivery of the operation enters with its
record and again just before the owner runs it. So once the inspection seals "never ran",
no delivery runs it later, and a delivery still inside the fence makes the answer Unknown.
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
from vs_runtime._core_identity import CoreRequestId, core_identity, core_named
from vs_runtime._core_requests import ExecutionContext, ExecutionResult
from vs_runtime._effect_fence import (
    EffectFences,
    Entered,
    Fenced,
    FenceKey,
    FenceKind,
    NothingHeld,
    OwnerNotApplied,
)
from vs_runtime._observation_factory import ObservationFacts, ObservationSubject
from vs_runtime._operation_catalog import (
    Applied,
    CancellableOwner,
    Indeterminate,
    NotApplied,
    OperationCatalog,
    OperationEntry,
)
from vs_runtime._operation_receipts import RecordReceipt, ResultReceipt
from vs_runtime._receipt_store import (
    BegunUnsealed,
    Conflict,
    Declined,
    Performed,
    Replayed,
    SealedExecution,
    Settled,
    Transient,
    owner_key,
)
from vs_runtime._request_inspection import RecordedRequestInspector

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
        self._inspector = RecordedRequestInspector(receipts.store, observations, (self,))
        self._fences = EffectFences(receipts.store)

    result_type = ResultReceipt

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
        subject: CoreRequestId | None = None,
        resource_id: ResourceId | None = None,
        sealed: ResultReceipt | None = None,
    ) -> Observation:
        """Observation of *subject* (default: the request itself), in the request's episode.

        ``own_effect`` is true when a receipt proves the subject's own outcome. Only
        a proven terminal result claims acceptance and release, because Unknown never
        does. A terminal rejection that names no resource proves the effect never began
        and never will, so it holds nothing. Each is one of: a self-refusal before the owner
        was reached (an operation outside the catalog, or declared but refused); a delivery
        a closed fence stopped before its owner ran (``_withdrawn``); or an inspection's
        "never ran", sealed only with the fence's ``NothingHeld`` (``_seal_never_ran``). A
        cancel names the resource it leaves owned and never claims its release. The sequence
        is per subject, so an Inspect of a target continues the target's own history.

        An answer about another request (``subject``) holds nothing only when it reports that
        request's ``sealed`` result: an inspection's rejection by itself proves nothing about
        what its target created.
        """
        terminal = own_effect and status is not ObservationStatus.UNKNOWN
        accepted = terminal and status in (ObservationStatus.SUCCEEDED, ObservationStatus.FAILED)
        about_itself = subject is None or (sealed is not None and sealed.request_id == subject.root)
        if (
            terminal
            and status is ObservationStatus.REJECTED
            and resource_id is None
            and about_itself
        ):
            # Each of the three is sealed under the subject's own identity, so it never acts.
            facts = ObservationFacts.refused(detail)
        else:
            facts = ObservationFacts(
                status=status,
                terminal=terminal,
                accepted=accepted,
                released=accepted,
                children_complete=accepted,
                resource_id=resource_id,
                diagnostic=detail,
            )
        return self._observations.observe(
            ObservationSubject.of(request, request_id=subject), facts, observed_at=context.now_at
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

    def _conflict(self, request: RequestBase, context: ExecutionContext) -> ExecutionResult:
        """The refusal of a reused identity: it proves nothing about what the identity holds."""
        return ExecutionResult(
            observation=RequestObserved(
                observation=self._observations.observe(
                    ObservationSubject.of(request),
                    ObservationFacts.payload_conflict(),
                    observed_at=context.now_at,
                ),
                setup_failure=_setup_failure(ObservationStatus.REJECTED),
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

        async def perform(*, repeat: bool) -> Settled[ResultReceipt] | Transient[ResultReceipt]:
            return await self._perform(request, context, entry, repeat=repeat)

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
                return self._conflict(request, context)
            case Declined(reason):
                return self._result(request, context, ObservationStatus.UNKNOWN, reason)
            case _:
                assert_never(execution)

    async def _perform(
        self,
        request: ExecuteRegisteredOperation,
        context: ExecutionContext,
        entry: OperationEntry,
        *,
        repeat: bool,
    ) -> Settled[ResultReceipt] | Transient[ResultReceipt]:
        """The effect, run under the store's begun marker. ``repeat`` means inspect first."""
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
        fence, by = operation_fence(request_id), core_identity(request)
        with self._receipts.store.exclusive():
            admission = self._fences.enter(fence, by=by, host=context.fence)
            if isinstance(admission, Entered):
                self._receipts.record_record(
                    RecordReceipt(
                        request_id=request_id.root,
                        payload_digest=context.payload_digest,
                        operation=request.operation,
                    )
                )
        try:
            return await self._run_owner(
                request, context, entry, owner, repeat=repeat, admission=admission
            )
        finally:
            self._fences.leave(fence, by=by)

    async def _run_owner(  # noqa: PLR0913  # lint-waiver: LW-410009 [PLR0913]; each argument is an independent fact of the one delivery the owner runs for.
        self,
        request: ExecuteRegisteredOperation,
        context: ExecutionContext,
        entry: OperationEntry,
        owner: OperationOwner,
        *,
        repeat: bool,
        admission: Entered | Fenced,
    ) -> Settled[ResultReceipt] | Transient[ResultReceipt]:
        """Run the owner inside the operation's fence, inspecting first when it may have run."""
        decoded = self._catalog.registry.decode(request.operation)
        if isinstance(admission, Fenced) and not admission.entered_before:
            # An inspection proved it never ran and closed the fence first: it never will.
            return Settled(self._withdrawn(request, context, entry))
        if repeat or isinstance(admission, Fenced):
            match await _inspect(owner, decoded, context):
                case Applied(outcome):
                    return Settled(self._complete(request, context, entry, outcome))
                case Indeterminate(reason):
                    return Transient(
                        self._receipt(request, context, entry, ObservationStatus.UNKNOWN, reason)
                    )
                case NotApplied():
                    pass
        fence = operation_fence(_identity(request))
        if not isinstance(
            self._fences.enter(fence, by=core_identity(request), host=context.fence), Entered
        ):
            # The owner has not run it, and the fence closed: it never runs.
            return Settled(self._withdrawn(request, context, entry))
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

    def _withdrawn(
        self, request: ExecuteRegisteredOperation, context: ExecutionContext, entry: OperationEntry
    ) -> ResultReceipt:
        """The result of a delivery a closed fence stopped before the owner ran it.

        The inspection that closed the fence may have sealed the operation's result already;
        that result stands, so a replay answers what the inspection answered.
        """
        sealed = self._receipts.result(_identity(request).root)
        if sealed is not None:
            return sealed
        return self._receipt(
            request,
            context,
            entry,
            ObservationStatus.REJECTED,
            "withdrawn: an inspection proved the operation never ran",
        )

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
        inspected = await self._inspector.answer(request, context)
        observed = RequestObserved(
            observation=self._observe(request, context, ObservationStatus.SUCCEEDED, ""),
            target=inspected.target,
        )
        return ExecutionResult(
            observation=self._catalog.registry.validate_event(observed),
            owner_events=inspected.owner_events,
        )

    async def answer(
        self,
        request: InspectRequest,
        context: ExecutionContext,
        history: BegunUnsealed | SealedExecution,
    ) -> TargetObservation:
        """Probe for operation targets: decode a sealed result, or ask the owner when begun."""
        del history  # the receipts below are the same records, decoded as operation receipts
        target_id = request.target
        sealed = self._receipts.result(target_id.root)
        record = self._receipts.record(target_id.root)
        schema = (
            sealed.schema_ref
            if sealed is not None
            else record.operation.schema_ref
            if record is not None
            else None
        )
        entry = None if schema is None else self._catalog.find(schema)
        if schema is None or entry is None:
            return self._unknown(request, context, "no operation receipt for the target")
        if sealed is None:
            if entry.owner is None or record is None:
                return self._unknown(
                    request, context, "target declared but refused, no result recorded"
                )
            decoded = self._catalog.registry.decode(record.operation)
            inspected = await _inspect(entry.owner, decoded, context)
            if isinstance(inspected, NotApplied):
                inspected = await self._fence_not_applied(request, context, entry.owner, decoded)
            match inspected:
                case Applied(outcome):
                    sealed = self._seal_inspected(target_id, record, entry, outcome)
                case NothingHeld() as proof:
                    sealed = self._seal_never_ran(record, entry, proof)
                case ResultReceipt():
                    sealed = inspected
                case Indeterminate(reason):
                    return self._unknown(request, context, reason)
        outcome = (
            None
            if sealed.outcome_json is None
            else self._catalog.registry.decode_outcome(entry.schema, sealed.outcome_json)
        )
        return TargetObservation(
            observation=self._observe(
                request,
                context,
                sealed.status,
                sealed.detail,
                subject=core_named(target_id),
                sealed=sealed,
            ),
            setup_failure=_setup_failure(sealed.status),
            operation_schema=entry.schema if outcome is not None else None,
            outcome_schema=entry.schema.outcome_schema if outcome is not None else None,
            outcome=outcome,
        )

    async def _fence_not_applied(
        self,
        request: InspectRequest,
        context: ExecutionContext,
        owner: OperationOwner,
        decoded: OperationRequest,
    ) -> Applied | Indeterminate | NothingHeld | ResultReceipt:
        """Make the owner's "not applied" final: close the fence, then confirm it.

        A delivery may have entered the fence and run the owner after the first inspection,
        so the answer is the target's seal if it has one by now, Unknown while a delivery is
        still inside, and otherwise a second inspection: nothing starts once the fence is
        closed, so its "not applied" holds for good.
        """
        with self._receipts.store.exclusive():
            closure = self._fences.close(
                operation_fence(request.target), by=core_identity(request), host=context.fence
            )
            sealed = self._receipts.result(request.target.root)
        if sealed is not None:
            return sealed
        if isinstance(closure, NothingHeld):
            return closure
        if closure.live:
            return Indeterminate("a delivery of the operation may still be running it")
        match await self._fences.ask_owner(closure, lambda: _inspect(owner, decoded, context)):
            case OwnerNotApplied() as evidence:
                return self._fences.cleared(closure, evidence)
            case confirmed:
                return confirmed

    def _seal_never_ran(
        self, record: RecordReceipt, entry: OperationEntry, proof: NothingHeld
    ) -> ResultReceipt:
        """Seal that the operation never ran and never will; only a closed fence proves it."""
        del proof  # its existence is the proof
        sealed = ResultReceipt(
            request_id=record.request_id,
            payload_digest=record.payload_digest,
            schema_ref=entry.schema,
            status=ObservationStatus.REJECTED,
            detail="owner proved the effect did not happen",
        )
        self._receipts.record_result(sealed)
        return sealed

    def _seal_inspected(
        self, target_id: RequestId, record: RecordReceipt, entry: OperationEntry, outcome: object
    ) -> ResultReceipt:
        try:
            model = (
                outcome
                if isinstance(outcome, BaseModel)
                else entry.registration.outcome_model.model_validate(outcome)
            )
            receipt = ResultReceipt(
                request_id=target_id.root,
                payload_digest=record.payload_digest,
                schema_ref=entry.schema,
                status=ObservationStatus.SUCCEEDED,
                outcome_json=self._catalog.registry.encode_outcome(entry.schema, model),
            )
        except (ValidationError, ContractError, TypeError) as error:
            receipt = ResultReceipt(
                request_id=target_id.root,
                payload_digest=record.payload_digest,
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
                subject=core_named(request.target),
            )
        )

    # cancel

    async def _cancel(
        self, request: CancelOwnedResource, context: ExecutionContext
    ) -> ExecutionResult:
        request_id = _identity(request)
        record = self._receipts.record(request.target.root)
        entry = None if record is None else self._catalog.find(record.operation.schema_ref)
        if record is None or entry is None:
            return self._result(
                request,
                context,
                ObservationStatus.UNKNOWN,
                "no operation receipt for the cancel target",
            )
        target_entry, operation = entry, record.operation

        async def perform(*, repeat: bool) -> Settled[ResultReceipt] | Transient[ResultReceipt]:
            del repeat  # cancellation is idempotent by contract, so a repeat repeats it
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
                return self._conflict(request, context)
            case Declined(reason):
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


def operation_fence(request_id: RequestId) -> FenceKey:
    """The fence of one operation's effect: its deliveries enter it, a "never ran" proof closes it."""
    return FenceKey(FenceKind.OPERATION, request_id.root)


def _identity(request: RequestBase) -> RequestId:
    if request.request_id is None:
        raise ContractError(("request_id",), "canonical identity required")
    return request.request_id


def _setup_failure(status: ObservationStatus) -> SetupFailureKind:
    del status
    return SetupFailureKind.UNKNOWN
