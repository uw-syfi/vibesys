"""Substitutable effects required by the evaluation coordinator."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from types import TracebackType

    from vs_evaluation.models import (
        AvailabilitySnapshot,
        EvaluationLifecycleEvent,
        EvaluationRequest,
        ExecutorObservation,
        ResourceRequirements,
        StoredEvaluation,
    )


class ExecutorSubmissionError(RuntimeError):
    """Submission response was unavailable; acceptance may be ambiguous."""

    def __init__(self, cause: Exception) -> None:
        """Retain the underlying transport or executor error."""
        super().__init__(str(cause))
        self.__cause__ = cause


class ExecutorCancellationUnknownError(RuntimeError):
    """No provider identity can prove termination of a possibly dispatched operation."""

    def __init__(self, handle_id: str) -> None:
        """Retain the stable logical identity that still needs reconciliation."""
        super().__init__(
            f"evaluation {handle_id!r} has unknown external identity; cancellation is unresolved"
        )
        self.handle_id = handle_id


class ExecutorRejectedError(ValueError):
    """The executor refused the request before accepting it; a retry cannot succeed.

    Raise it for a request the executor can never run, such as an evidence
    kind it does not produce. The coordinator records the handle as failed
    with this reason instead of retrying the submission.
    """


class Clock(Protocol):
    """Monotonic time source used for bounded waits and observations."""

    def monotonic(self) -> float:
        """Return monotonic seconds."""
        ...


class EvaluationEventSink(Protocol):
    """Receive lifecycle facts after their authoritative store write."""

    def __call__(self, event: EvaluationLifecycleEvent, /) -> None:
        """Publish one fact; reconciliation may repeat a revision."""
        ...


class NullEvaluationEventSink:
    """No-op sink used when no lifecycle observer is attached."""

    def __call__(self, event: EvaluationLifecycleEvent, /) -> None:
        """Discard one lifecycle fact."""
        del event


NULL_EVALUATION_EVENT_SINK = NullEvaluationEventSink()


class DeadlineScope(Protocol):
    """Cancelable async scope that reports whether its deadline expired."""

    async def __aenter__(self) -> object:
        """Begin the bounded operation."""
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        """Translate deadline cancellation into TimeoutError."""
        ...

    def expired(self) -> bool:
        """Distinguish deadline expiry from a provider TimeoutError."""
        ...


class EvaluationExecutor(Protocol):
    """Idempotent execution port addressed by stable handle ID."""

    async def availability(self, requirements: ResourceRequirements) -> AvailabilitySnapshot:
        """Observe capacity for generic resource requirements."""
        ...

    async def submit(self, request: EvaluationRequest, *, handle_id: str) -> None:
        """Ensure work for handle_id exists, idempotently across resume.

        Wrap a submission failure in ExecutorSubmissionError. A raised error
        does not imply that the executor rejected the request. Raise
        ExecutorRejectedError only when the executor definitely did not
        accept the request and never will.
        """
        ...

    async def inspect(self, handle_id: str) -> ExecutorObservation | None:
        """Return current execution state, or None when it was not submitted."""
        ...

    async def wait_for_change(self, handle_id: str, timeout_s: float) -> None:
        """Wait at most timeout_s for state to change; may return spuriously.

        Notifications must remain visible if a change lands between inspect
        and this call. A sticky event or generation counter satisfies this
        contract; clearing an event before checking it does not.
        """
        ...

    async def cancel(self, handle_id: str) -> None:
        """Request cancellation; inspect the resulting stage to confirm it."""
        ...


class EvaluationStore(Protocol):
    """Durable, atomic storage for idempotency keys and lifecycle records."""

    async def claim(self, request: EvaluationRequest, *, handle_id: str) -> StoredEvaluation:
        """Insert an accepted record or return the record already owning its key."""
        ...

    async def get(self, handle_id: str) -> StoredEvaluation | None:
        """Read a record by stable handle ID."""
        ...

    async def get_by_key(self, key: str) -> StoredEvaluation | None:
        """Read the record owning an idempotency key."""
        ...

    async def compare_and_set(
        self, record: StoredEvaluation, *, expected_revision: int
    ) -> StoredEvaluation:
        """Replace a record iff its current revision matches expected_revision."""
        ...

    async def records(self) -> tuple[StoredEvaluation, ...]:
        """Return every durable lifecycle record without refreshing execution."""
        ...

    async def nonterminal(self) -> tuple[StoredEvaluation, ...]:
        """Return records that may need resume reconciliation."""
        ...
