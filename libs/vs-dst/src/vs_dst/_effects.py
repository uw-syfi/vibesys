"""The effects ledger: what the executors' world did, kept across restarts of the host.

A host that dies loses its memory; the effects it caused do not go away. `RecoverableExecutors`
seal what each request's effect did, so a restarted host that inspects a request learns its
result instead of running the effect again.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from vs_core.api import (
    ContractError,
    EventId,
    InspectRequest,
    Observation,
    ObservationStatus,
    ReissueProof,
    RequestObserved,
    TargetObservation,
)
from vs_core.api.testing import Succeeded
from vs_runtime.api.core import ExecutionResult
from vs_runtime.api.testing import ScriptedExecutors

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from pydantic import BaseModel

    from vs_core.api import CoreState, OperationRegistry, Request, SchemaRef
    from vs_core.api.testing import Journal
    from vs_runtime.api.core import ExecutionContext, OwnerEvent
    from vs_runtime.api.testing import Script


@dataclass
class Effects:
    """What the executors' world did, kept across restarts of the host."""

    sealed: dict[str, ExecutionResult] = field(default_factory=dict)
    """The result each request's effect left behind: what a restarted host inspects."""
    executed: list[str] = field(default_factory=list)
    """Requests whose effect ran, in order; a request in it twice ran twice."""
    sequences: dict[str, int] = field(default_factory=dict)
    """Observation sequences issued per request, which a restart continues."""


def _target_fields(observed: RequestObserved) -> dict[str, object]:
    """Every fact of an executor's observation that a target observation carries."""
    skipped = {"kind", "target", "outcome"}
    return {
        name: getattr(observed, name)
        for name in RequestObserved.model_fields
        if name not in skipped
    }


class RecoverableExecutors(ScriptedExecutors):
    """Scripted executors that answer an inspection from what a request's effect sealed."""

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-994708 [PLR0913]; the base class takes the script's five collaborators and this one adds the ledger it records into.
        # > Bundling them would change the `TranslatorFactory` signature every `ScriptedExecutors` subclass shares.
        self,
        script: Script,
        core: Callable[[], CoreState],
        registry: OperationRegistry,
        schemas: Mapping[SchemaRef, type[BaseModel]],
        journal: Journal,
        effects: Effects,
    ) -> None:
        """Bind the script and the shared record of effects."""
        super().__init__(script, core, registry, schemas, journal)
        self._effects = effects
        self._registry = registry

    def _next(self, key: str) -> int:
        """The next observation sequence of ``key``, continued across restarts of the host."""
        sequences = self._effects.sequences
        sequences[key] = sequences.get(key, -1) + 1
        return sequences[key]

    async def execute(self, request: Request, context: ExecutionContext) -> ExecutionResult:
        """Perform the request once and seal its result; answer an inspection from the seal."""
        if isinstance(request, InspectRequest):
            return self._inspect(request, context)
        if request.request_id is None:
            raise ContractError(("request_id",), "a dispatched request carries its identity")
        identity = request.request_id.root
        self._effects.executed.append(identity)
        result = await super().execute(request, context)
        self._effects.sealed[identity] = result
        return result

    def _target(self, observed: RequestObserved) -> TargetObservation:
        """The target facts of a sealed observation; a typed outcome needs the operation codec."""
        return TargetObservation.model_validate(
            _target_fields(observed), context={"operation_registry": self._registry}
        )

    def _inspect(self, request: InspectRequest, context: ExecutionContext) -> ExecutionResult:
        """What a restarted host learns of a request: its sealed result, or that it never ran."""
        target = request.target
        sealed = self._effects.sealed.get(target.root)
        owner_events: tuple[OwnerEvent, ...] = ()
        if sealed is not None:
            facts = self._target(sealed.observation)
            owner_events = sealed.owner_events
        else:
            sequence = self._next(target.root)
            observation = Observation(
                event_id=EventId(root=f"{target.root}:observation:{sequence}"),
                request_id=target,
                scope=request.scope,
                admission_id=request.admission_id,
                sequence=sequence,
                observed_at=context.now_at,
                status=ObservationStatus.REJECTED,
                accepted=False,
                terminal=True,
                released=False,
                children_complete=False,
            )
            facts = TargetObservation(observation=observation, reissue=ReissueProof.NEVER_BEGAN)
        own = self._observed(request, Succeeded(), context.now_at)
        return ExecutionResult(
            observation=own.model_copy(update={"target": facts}), owner_events=owner_events
        )
