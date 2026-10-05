"""Answer ``InspectRequest`` for any request kind from the one place that recorded it.

``InspectRequest`` names a prior request by identity and asks whether its effect
happened. Every receipt-backed executor records its request through
``ReceiptStore.run_once``: a ``begun`` marker before the effect and a sealed result
after it. So the store's ``history`` is the single source for the answer, whatever
the kind:

* sealed: the whole sealed result: the target's terminal facts and the owner events the
  executor committed with it, so adopting it feeds core exactly what the live path did;
* begun without a result: the effect may have run, so Unknown, unless a kind-specific
  probe can re-observe an external effect (a registered operation's owner);
* nothing recorded: the effect never began, but only for kinds that run on
  ``run_once``. While any role still keeps its own receipts (``HAND_ROLLED_ROLES``) a
  missing record proves nothing, so the answer stays Unknown.

A probe is registered per sealed result type, not per request kind or backend name.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ValidationError

from vs_core.api import (
    InspectRequest,
    ObservationStatus,
    RequestObserved,
    TargetObservation,
)
from vs_runtime._core_requests import HAND_ROLLED_ROLES, ExecutionResult
from vs_runtime._observation_factory import ObservationFacts, ObservationSubject
from vs_runtime._receipt_store import (
    BegunUnsealed,
    NeverBegun,
    ReceiptCorruptError,
    SealedExecution,
    result_type_name,
)
from vs_runtime._semantic_events import PUBLISHED_FACTS, Published

if TYPE_CHECKING:
    from collections.abc import Mapping

    from vs_runtime._core_requests import ExecutionContext, OwnerEvent
    from vs_runtime._observation_factory import ObservationFactory
    from vs_runtime._receipt_store import ReceiptStore


# Fields of an executor's RequestObserved that are not a target's facts. ``target`` is the
# nested inspection answer itself; ``outcome`` is decoded from ``outcome_json`` at the codec
# boundary and never serialized.
NOT_TARGET_FACTS = frozenset({"kind", "target", "outcome"})


def as_target(observed: RequestObserved) -> TargetObservation:
    """The target's facts from an executor's observation: every field, none dropped.

    The projection is by field name, so a field added to ``RequestObserved`` reaches
    inspection with no edit here (and fails the completeness test if ``TargetObservation``
    cannot carry it).
    """
    names = (name for name in RequestObserved.model_fields if name not in NOT_TARGET_FACTS)
    return TargetObservation(**{name: getattr(observed, name) for name in names})


@dataclass(frozen=True)
class Inspected:
    """What a target left behind: its facts, and the owner events committed with them.

    A sealed result is replayed whole. The owner events travel on the inspecting request's
    own result, so the shell commits and applies them as it does for any executed request.
    """

    target: TargetObservation
    owner_events: tuple[OwnerEvent, ...] = ()


class TargetProbe(Protocol):
    """Kind-specific answer for a result type whose effect is external or needs a codec.

    Called for a begun or sealed target whose record carries ``result_type``. It
    returns the target's facts, re-observing the external effect when necessary.
    """

    @property
    def result_type(self) -> type[BaseModel]:
        """The sealed result type this probe answers for."""
        ...

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

    async def answer(self, request: InspectRequest, context: ExecutionContext) -> Inspected:
        """The target's result: sealed, unknown, or proven never started."""
        try:
            history = self._store.history(request.target.root)
        except ReceiptCorruptError as error:
            return Inspected(self._unknown(request, context, str(error)))
        if isinstance(history, NeverBegun):
            return Inspected(self._never_begun(request, context))
        probe = None if history.result_type is None else self._probes.get(history.result_type)
        if probe is not None:
            try:
                return Inspected(await probe.answer(request, context, history))
            except ReceiptCorruptError as error:
                return Inspected(self._unknown(request, context, str(error)))
        if isinstance(history, BegunUnsealed):
            return Inspected(
                self._unknown(request, context, "the effect began and has no recorded result")
            )
        return self._sealed(request, context, history)

    def _never_begun(self, request: InspectRequest, context: ExecutionContext) -> TargetObservation:
        if HAND_ROLLED_ROLES:
            # A role with its own receipts leaves no execution record, but its observation
            # row proves the request began. Re-report that row unchanged (core accepts an
            # identical replay); with no row, report Unknown.
            latest = self._observations.latest(request.target)
            if latest is not None:
                return TargetObservation(observation=latest)
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
    ) -> Inspected:
        if history.result_type == result_type_name(ExecutionResult):
            try:
                result = ExecutionResult.model_validate_json(history.result_json)
            except ValidationError:
                return Inspected(self._unknown(request, context, "sealed result is unreadable"))
            return Inspected(as_target(result.observation), result.owner_events)
        if history.result_type == result_type_name(Published):
            return Inspected(self._target(request, context, PUBLISHED_FACTS))
        return Inspected(
            self._unknown(
                request, context, f"no inspection for sealed result type {history.result_type}"
            )
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


__all__ = ["Inspected", "RecordedRequestInspector", "TargetProbe", "as_target"]
