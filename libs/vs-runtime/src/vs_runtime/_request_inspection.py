"""Answer ``InspectRequest`` for any request kind from the one place that recorded it.

``InspectRequest`` names a prior request by identity and asks whether its effect
happened. Every receipt-backed executor records its request through
``ReceiptStore.run_once``: a ``begun`` marker before the effect and a sealed result
after it. So the store's ``history`` is the single source for the answer, whatever
the kind:

* sealed: the sealed result, translated to the target's terminal facts;
* begun without a result: the effect may have run, so Unknown, unless a kind-specific
  probe can re-observe an external effect (a registered operation's owner);
* nothing recorded: the effect never began, but only for kinds that run on
  ``run_once``. While any role still keeps its own receipts (``HAND_ROLLED_ROLES``) a
  missing record proves nothing, so the answer stays Unknown.

A probe is registered per sealed result type, not per request kind or backend name.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ValidationError

from vs_core.api import InspectRequest, ObservationStatus, TargetObservation
from vs_runtime._core_requests import HAND_ROLLED_ROLES, ExecutionResult
from vs_runtime._observation_factory import ObservationFacts, ObservationSubject
from vs_runtime._receipt_store import (
    BegunUnsealed,
    NeverBegun,
    ReceiptCorruptError,
    SealedExecution,
    result_type_name,
)
from vs_runtime._semantic_events import Published

if TYPE_CHECKING:
    from collections.abc import Mapping

    from vs_runtime._core_requests import ExecutionContext
    from vs_runtime._observation_factory import ObservationFactory
    from vs_runtime._receipt_store import ReceiptStore


class TargetProbe(Protocol):
    """Kind-specific answer for a result type whose effect is external or needs a codec.

    Called for a begun or sealed target whose record carries ``result_type``. It
    returns the target's facts, re-observing the external effect when necessary.
    """

    result_type: type[BaseModel]

    async def answer(
        self,
        request: InspectRequest,
        context: ExecutionContext,
        history: BegunUnsealed | SealedExecution,
    ) -> TargetObservation: ...


class RecordedRequestInspector:
    """The generic ``InspectRequest`` answer over the shared ``ReceiptStore``."""

    def __init__(
        self,
        store: ReceiptStore,
        observations: ObservationFactory,
        probes: tuple[TargetProbe, ...] = (),
    ) -> None:
        """Bind the store, the observation sequences and the registered probes."""
        self._store = store
        self._observations = observations
        self._probes: Mapping[str, TargetProbe] = {
            result_type_name(probe.result_type): probe for probe in probes
        }

    async def answer(self, request: InspectRequest, context: ExecutionContext) -> TargetObservation:
        """The target's facts: sealed, unknown, or proven never started."""
        try:
            history = self._store.history(request.target.root)
        except ReceiptCorruptError as error:
            return self._unknown(request, context, str(error))
        if isinstance(history, NeverBegun):
            return self._never_begun(request, context)
        probe = None if history.result_type is None else self._probes.get(history.result_type)
        if probe is not None:
            try:
                return await probe.answer(request, context, history)
            except ReceiptCorruptError as error:
                return self._unknown(request, context, str(error))
        if isinstance(history, BegunUnsealed):
            return self._unknown(request, context, "the effect began and has no recorded result")
        return self._sealed(request, context, history)

    def _never_begun(self, request: InspectRequest, context: ExecutionContext) -> TargetObservation:
        if HAND_ROLLED_ROLES:
            return self._unknown(
                request, context, "no execution record, and some executors keep their own receipts"
            )
        return self._target(
            request,
            context,
            ObservationFacts(
                status=ObservationStatus.REJECTED,
                diagnostic="no begun marker: the effect never started",
            ),
        )

    def _sealed(
        self, request: InspectRequest, context: ExecutionContext, history: SealedExecution
    ) -> TargetObservation:
        if history.result_type == result_type_name(ExecutionResult):
            try:
                result = ExecutionResult.model_validate_json(history.result_json)
            except ValidationError:
                return self._unknown(request, context, "sealed result is unreadable")
            observed = result.observation
            return TargetObservation(
                observation=observed.observation,
                setup_failure=observed.setup_failure,
                revision=observed.revision,
                evidence=observed.evidence,
                progress=observed.progress,
                measurement_failure=observed.measurement_failure,
                evaluation_result=observed.evaluation_result,
                suspension=observed.suspension,
            )
        if history.result_type == result_type_name(Published):
            return self._target(
                request,
                context,
                ObservationFacts(status=ObservationStatus.SUCCEEDED, accepted=True, released=True),
            )
        return self._unknown(
            request, context, f"no inspection for sealed result type {history.result_type}"
        )

    def _unknown(
        self, request: InspectRequest, context: ExecutionContext, detail: str
    ) -> TargetObservation:
        return self._target(
            request,
            context,
            ObservationFacts(status=ObservationStatus.UNKNOWN, terminal=False, diagnostic=detail),
        )

    def _target(
        self, request: InspectRequest, context: ExecutionContext, facts: ObservationFacts
    ) -> TargetObservation:
        observation = self._observations.observe(
            ObservationSubject.of(request, request_id=request.target),
            facts,
            observed_at=context.now_at,
        )
        return TargetObservation(observation=observation)


__all__ = ["RecordedRequestInspector", "TargetProbe"]
