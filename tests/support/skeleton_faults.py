"""Host faults at the two boundaries of the skeleton's shell, driven by one ``FaultPlan``.

``FaultingExecutors`` wraps the real executors at their single dispatch point and
``FaultingStore`` wraps the durable run store at its atomic write. Both are generic over what
crosses them: the executor target is the request kind, the write target is ``commit``. They
share one ``FaultGate`` that outlives every simulated host process, so ordinals count across
restarts. With an empty plan both are pass-throughs that record each crossing, which is how a
test enumerates the crash points of a run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vs_faults.api import Boundary, FaultGate
from vs_runtime.api.core import RequestExecutors

if TYPE_CHECKING:
    from vs_core.api import Request
    from vs_project.api import (
        CommitOutcome,
        QuarantinedEnvelope,
        StateStore,
        StoredEnvelope,
        StoreFence,
        StoreRecord,
    )
    from vs_runtime.api.core import ExecutionContext, ExecutionOutcome

COMMIT = "commit"


@dataclass(frozen=True)
class FaultingExecutors(RequestExecutors):
    """The real executors, with the gate's executor-request faults at their dispatch point."""

    gate: FaultGate | None = None

    @classmethod
    def around(cls, real: RequestExecutors, gate: FaultGate) -> FaultingExecutors:
        """Wrap ``real``; every role is the real executor."""
        return cls(
            workspaces=real.workspaces,
            sessions=real.sessions,
            evaluation=real.evaluation,
            operations=real.operations,
            semantic_events=real.semantic_events,
            gate=gate,
        )

    async def dispatch(self, request: Request, context: ExecutionContext) -> ExecutionOutcome:
        """Run the effect, then crash the host if the plan says so (before its observation)."""
        assert self.gate is not None
        return await self.gate.around_async(
            Boundary.EXECUTOR_REQUEST,
            request.kind,
            lambda: RequestExecutors.dispatch(self, request, context),
        )


class FaultingStore:
    """A ``StateStore`` whose commits are crash points: the write lands, then the host dies."""

    def __init__(self, inner: StateStore, gate: FaultGate) -> None:
        """Wrap ``inner``; ``gate`` counts the writes."""
        self._inner = inner
        self._gate = gate

    def load(self) -> StoreRecord | None:
        """The inner store's record."""
        return self._inner.load()

    def commit(
        self, expected_revision: int | None, envelope: StoredEnvelope, fence: StoreFence, now: float
    ) -> CommitOutcome:
        """One durable write; a scheduled crash comes after it is visible to the next host."""
        return self._gate.around(
            Boundary.DURABLE_WRITE,
            COMMIT,
            lambda: self._inner.commit(expected_revision, envelope, fence, now),
        )

    def quarantine(
        self,
        expected_revision: int | None,
        envelope: QuarantinedEnvelope,
        fence: StoreFence,
        now: float,
    ) -> CommitOutcome:
        """The inner store's quarantine."""
        return self._inner.quarantine(expected_revision, envelope, fence, now)

    def acquire(self, host_id: str, now: float, duration: float) -> StoreFence | None:
        """The inner store's lease acquisition."""
        return self._inner.acquire(host_id, now, duration)

    def renew(self, fence: StoreFence, now: float, duration: float) -> StoreFence | None:
        """The inner store's lease renewal."""
        return self._inner.renew(fence, now, duration)

    def verify(self, fence: StoreFence, now: float) -> bool:
        """The inner store's fence check."""
        return self._inner.verify(fence, now)
