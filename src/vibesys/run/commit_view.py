"""What the run's screens learn from the log: the committed state after each record.

The step loop keeps core's state as a fold of the run.log, and nothing in it shows a screen
anything. The host's commit observer does: it hears each committed state, publishes the
committed-view hint, and turns the difference between two states into the run's events (a round
finished, a winner selected, a comparison read). ``ObservedInputs`` is the step log the loop runs
on, and after every record that changes the fold it hands the observer the state and the search
beside it, in the shape the observer already reads (a ``RuntimeRecord``). The record is a view
of the fold: nothing writes it, nothing reads it back.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from vs_core.api import (
    ENVELOPE_SCHEMA_VERSION,
    EventCursor,
    HostFence,
    HostId,
    RunEnvelope,
)
from vs_core.api.recovery import Finished
from vs_runtime.api.core import RUNTIME_SCHEMA_VERSION, RuntimeRecord

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from vibesys.run.run_clock import RunClock
    from vs_core.api import CoreEvent, CoreState, RequestId, SearchDeclaration, Transition
    from vs_core.api.recovery import (
        End,
        Interruption,
        OpenStep,
        RecordRefusal,
        Run,
        StepResult,
    )
    from vs_runtime.api.core import CommitObserver
    from vs_runtime.api.step_runner import CoreInputs, SearchRun, StepRecord


class RecordView:
    """The fold and the search beside it, in the shape of the record the observer reads."""

    def __init__(
        self,
        search: SearchRun[Any, Any],
        declaration: SearchDeclaration,
        *,
        epoch: int,
        run_id: str,
    ) -> None:
        """View the run ``run_id`` that ``search`` runs, written by the process of ``epoch``."""
        self._search = search
        self._declaration = declaration
        self._fence = HostFence(host_id=HostId(root=f"run:{run_id}"), epoch=epoch)

    def of(self, state: CoreState) -> RuntimeRecord[Any]:
        """The record that shows ``state`` and the search as it stands.

        The state was validated when the log folded it, so the view is built, not validated again.
        """
        progress = self._search.progress
        if progress is None:
            raise SearchNotAdvancedError
        envelope = RunEnvelope[Any].model_construct(
            schema_version=ENVELOPE_SCHEMA_VERSION,
            fence=self._fence,
            strategy_id=self._declaration.strategy_id,
            state_schema=self._declaration.state_schema,
            core=state,
            strategy=progress.state,
            event_cursor=EventCursor(sequence=0),
        )
        return RuntimeRecord[Any].model_construct(
            schema_version=RUNTIME_SCHEMA_VERSION,
            envelope=envelope,
            pending_publications=(),
            delivery_cursor=0,
            next_publication_sequence=1,
            pending_inputs=(),
            pending_decisions=(),
            wakes=(),
        )


class ObservedInputs:
    """The step log of the run, which tells the commit observer what each record changed."""

    def __init__(
        self,
        inputs: CoreInputs,
        view: RecordView,
        observer: CommitObserver,
        notes: Callable[[], Sequence[str]],
        clock: RunClock,
    ) -> None:
        """Show ``view`` of ``inputs`` to ``observer``; ``notes`` adds to a stalled run's report.

        ``clock`` is the clock the log stamps its records with.
        """
        self._clock = clock
        self._inputs = inputs
        self._view = view
        self._observer = observer
        self._notes = notes
        self._shown: CoreState | None = None
        self._record: RuntimeRecord[Any] | None = None

    def begin(self, *, resumed: bool) -> None:
        """Announce the state the log held when this process took it over.

        A run that had none is announced from nothing; a resumed run's earlier states were
        announced by the process that wrote them, so the observer is told only what this one adds.
        """
        self._shown = self._inputs.state
        current = self._view.of(self._inputs.state)
        self._record = current
        self._observer.committed(current if resumed else None, current)

    @property
    def state(self) -> CoreState:
        """The state the log folds to."""
        return self._inputs.state

    @property
    def admitted_core(self) -> CoreState:
        """The state, including every input a tool admitted."""
        return self._inputs.state

    def open_step(self, tag: RequestId) -> OpenStep | None:
        """The step ``tag`` as the log has it, while it has no result."""
        return self._inputs.open_step(tag)

    async def step_started(self, run: Run) -> Run | RecordRefusal:
        """Record that an attempt is about to start, or say why the rule would not run it now."""
        try:
            return await self._inputs.step_started(run)
        finally:
            self._observe()

    async def step_finished(self, tag: RequestId, attempt: int, result: StepResult) -> StepRecord:
        """Record the one result of a step, dated by the instant it is recorded."""
        try:
            return await self._inputs.step_finished(
                tag, attempt, _recorded_at(result, self._clock.now())
            )
        finally:
            self._observe()

    async def attempt_interrupted(self, tag: RequestId, attempt: int, why: Interruption) -> None:
        """Record that an attempt ended and its step did not."""
        try:
            await self._inputs.attempt_interrupted(tag, attempt, why)
        finally:
            self._observe()

    async def run_ended(self, end: End) -> None:
        """Record the end of the run."""
        try:
            await self._inputs.run_ended(end)
        finally:
            self._observe()

    async def clock_advanced(self, at: float) -> None:
        """Record that the run clock reached ``at``, which core asked for with ``Tick``."""
        try:
            await self._inputs.clock_advanced(at)
        finally:
            self._observe()

    async def next_input(self) -> None:
        """Return once an input was appended."""
        try:
            await self._inputs.next_input()
        finally:
            self._observe()

    def stalled(self) -> str | None:
        """Why the run can never go on, with what the host adds to it, or None."""
        try:
            reason = self._inputs.stalled()
        finally:
            self._observe()
        if reason is None:
            return None
        return "; ".join((reason, *self._notes()))

    def admit(self, event: CoreEvent, *, now_at: float) -> Transition:
        """Commit an input a tool admitted mid-turn, and return core's answer to it."""
        del now_at
        try:
            return self._inputs.admit(event)
        finally:
            self._observe()

    def _observe(self) -> None:
        """Tell the observer the state, if the fold moved since it was last told."""
        state = self._inputs.state
        if state is self._shown:
            return
        self._shown = state
        self._tell(self._record)

    def _tell(self, previous: RuntimeRecord[Any] | None) -> None:
        current = self._view.of(self._inputs.state)
        self._record = current
        self._observer.committed(previous, current)


def _recorded_at(result: StepResult, at: float) -> StepResult:
    """``result`` with its report and owner events dated ``at``, the instant the log records them.

    An executor dates what it reports when its step began, which may be long before the loop
    records it, and the fold refuses an event dated before the record that holds it.
    """
    if not isinstance(result, Finished):
        return result
    observed = (
        None if result.observed is None else result.observed.model_copy(update={"now_at": at})
    )
    events = tuple(event.model_copy(update={"now_at": at}) for event in result.owner_events)
    return result.model_copy(update={"observed": observed, "owner_events": events})


class SearchNotAdvancedError(RuntimeError):
    """The run's state was asked for before its search advanced: the log was opened without it."""

    def __init__(self) -> None:
        """Say what happened."""
        super().__init__("the search has not advanced, so the run has no state to show")
