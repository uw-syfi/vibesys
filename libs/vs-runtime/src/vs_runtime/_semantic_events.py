"""Committed blocked-intent diagnostics, deduplicated by request identity.

``BlockIntent`` reports that a target request is blocked and says why. The core
publication journal carries only strategy events and has no diagnostic variant,
so these diagnostics go to a runtime-owned journal in the same Project namespace.
Delivery is an exact replay: the same request identity with the same payload is
acknowledged without a second row, and a different payload is a REJECTED observation.
The acknowledgement covers the command only. The target stays owned and blocked.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, assert_never

from pydantic import BaseModel, ConfigDict, Field

from vs_core.api import (
    BlockIntent,
    ContractError,
    ObservationStatus,
    RequestObserved,
    Scope,
)
from vs_runtime._core_requests import ExecutionContext, ExecutionResult
from vs_runtime._observation_factory import (
    ObservationFactory,
    ObservationFacts,
    ObservationSubject,
)
from vs_runtime._receipt_store import (
    Conflict,
    Declined,
    Performed,
    Replayed,
    Settled,
    Transient,
    owner_key,
)

if TYPE_CHECKING:
    from vs_project.api import StateNamespace
    from vs_runtime._receipt_store import ReceiptStore

_JOURNAL = "block-diagnostics"


class BlockDiagnostic(BaseModel):
    """One published diagnostic. The sequence is its position in the journal."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    sequence: int = Field(ge=1)
    request_id: str = Field(min_length=1)
    target: str = Field(min_length=1)
    scope: Scope
    diagnostic: str


class Published(BaseModel):
    """The sealed result of one request: the journal position its diagnostic holds."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    sequence: int = Field(ge=1)


class JournalSemanticEvents:
    """The SEMANTIC_EVENTS role over a Project state namespace.

    The journal is append-only: row N is the file ``block-diagnostics/<N>.json``,
    written once and never rewritten, so an append costs one small write.
    """

    def __init__(self, store: ReceiptStore, namespace: StateNamespace) -> None:
        """Bind the shared receipt store and the namespace that holds the journal rows."""
        self._store = store
        self._namespace = namespace
        self._observations = ObservationFactory(store)

    def read(self) -> tuple[BlockDiagnostic, ...]:
        """Every published diagnostic in order."""
        return tuple(
            self._namespace.load(f"{_JOURNAL}/{name}", BlockDiagnostic)
            for name in self._namespace.entries(_JOURNAL)
        )

    async def execute(self, request: BlockIntent, context: ExecutionContext) -> ExecutionResult:
        """Publish once per request identity, then acknowledge that command only."""
        if request.request_id is None:
            raise ContractError(("request_id",), "canonical identity required")
        request_id = request.request_id

        async def publish(*, resumed: bool) -> Settled[Published] | Transient[Published]:
            with self._store.exclusive():
                rows = self.read() if resumed else ()
                prior = next((row for row in rows if row.request_id == request_id.root), None)
                if prior is not None:
                    return Settled(Published(sequence=prior.sequence))
                sequence = len(self._namespace.entries(_JOURNAL)) + 1
                self._namespace.save(
                    f"{_JOURNAL}/{sequence:012d}.json",
                    BlockDiagnostic(
                        sequence=sequence,
                        request_id=request_id.root,
                        target=request.target.root,
                        scope=request.scope,
                        diagnostic=request.diagnostic,
                    ),
                )
                return Settled(Published(sequence=sequence))

        execution = await self._store.run_once(
            request_id.root,
            owner=owner_key(request),
            context=context,
            result_type=Published,
            perform=publish,
        )
        match execution:
            case Replayed() | Performed():
                facts = ObservationFacts(ObservationStatus.SUCCEEDED, accepted=True)
            case Conflict():
                facts = ObservationFacts(
                    ObservationStatus.REJECTED,
                    diagnostic="same request identity with another payload",
                )
            case Declined(reason):
                facts = ObservationFacts(
                    ObservationStatus.UNKNOWN, terminal=False, diagnostic=reason
                )
            case _:
                assert_never(execution)
        return ExecutionResult(
            observation=RequestObserved(
                observation=self._observations.observe(
                    ObservationSubject.of(request), facts, observed_at=context.now_at
                )
            )
        )
