"""Public deterministic profiler provision for service integration tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from vs_evaluation.profiler_models import ProfilerAgentResult, ProfilerResultOutcome


@dataclass(frozen=True, slots=True)
class FakeProfilerTurn:
    """One profiler turn observed by the fake provision."""

    session_id: str
    operation_id: str
    request: str
    scope_id: str | None
    candidate_snapshot_id: str


class FakeProfilerTurnProvision:
    """Controllable provision that faithfully preserves conversation IDs."""

    def __init__(self, identity: str = "fake-profiler-v1") -> None:
        """Create an empty controllable provision."""
        self._identity = identity
        self.turns: list[FakeProfilerTurn] = []
        self.canceled: list[str] = []
        self.canceled_scopes: list[str] = []
        self.active: set[str] = set()
        self.max_active = 0
        self._release: dict[str, asyncio.Event] = {}
        self._started: dict[str, asyncio.Event] = {}
        self._results: dict[str, ProfilerAgentResult] = {}

    @property
    def identity(self) -> str:
        """Return the configured provision identity."""
        return self._identity

    async def run_turn(
        self,
        *,
        session_id: str,
        operation_id: str,
        request: str,
        scope_id: str | None,
        candidate_snapshot_id: str,
    ) -> ProfilerAgentResult:
        """Record and block one profiler turn until explicitly completed."""
        self.turns.append(
            FakeProfilerTurn(
                session_id=session_id,
                operation_id=operation_id,
                request=request,
                scope_id=scope_id,
                candidate_snapshot_id=candidate_snapshot_id,
            )
        )
        self.active.add(operation_id)
        self._started.setdefault(operation_id, asyncio.Event()).set()
        self.max_active = max(self.max_active, len(self.active))
        try:
            await self._release.setdefault(operation_id, asyncio.Event()).wait()
            return self._results[operation_id]
        finally:
            self.active.discard(operation_id)

    async def cancel(self, operation_id: str) -> None:
        """Record and release one canceled turn."""
        self.canceled.append(operation_id)
        self._release.setdefault(operation_id, asyncio.Event()).set()

    async def cancel_scope(self, scope_id: str) -> None:
        """Record retirement of one owner scope."""
        self.canceled_scopes.append(scope_id)

    def complete(
        self,
        operation_id: str,
        *,
        narrative: str = "advisory profile interpretation",
        evidence_ids: tuple[str, ...] = (),
    ) -> None:
        """Release a turn with an advisory result."""
        self._results[operation_id] = ProfilerAgentResult(
            outcome=ProfilerResultOutcome.OBSERVED,
            narrative=narrative,
            evidence_ids=evidence_ids,
        )
        self._release.setdefault(operation_id, asyncio.Event()).set()

    def unsupported(
        self, operation_id: str, reason: str, *, evidence_ids: tuple[str, ...] = ()
    ) -> None:
        """Release a turn with a typed request-specific unsupported result."""
        self._results[operation_id] = ProfilerAgentResult(
            outcome=ProfilerResultOutcome.UNSUPPORTED,
            narrative="The requested measurement is unavailable in this environment.",
            evidence_ids=evidence_ids,
            unsupported_reason=reason,
        )
        self._release.setdefault(operation_id, asyncio.Event()).set()

    async def wait_started(self, operation_id: str) -> None:
        """Yield until ``operation_id`` reaches the provision."""
        await self._started.setdefault(operation_id, asyncio.Event()).wait()


__all__ = ["FakeProfilerTurn", "FakeProfilerTurnProvision"]
