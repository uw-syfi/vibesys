"""`IssueQueueStrategy`: the issue queue as a pure vs-core `Strategy`.

`decide` runs the open job, or the cursor between jobs, and then the run's ending, over one
`Draft`; `on_event` folds one core feedback into the state. Both are pure functions of the
strategy state, the `RunView` and the static `IssueQueueConfig`: no clock, no I/O, no hidden
counters.
"""

from dataclasses import dataclass, field, replace

from vibesys.orchestration.issue_queue.strategy import _flow as flow
from vibesys.orchestration.issue_queue.strategy import _job as job_decisions
from vibesys.orchestration.issue_queue.strategy import _job_events as job_events
from vibesys.orchestration.issue_queue.strategy import _run as run
from vibesys.orchestration.issue_queue.strategy._config import IssueQueueConfig
from vibesys.orchestration.issue_queue.strategy._draft import Draft
from vibesys.orchestration.issue_queue.strategy._ids import decision_of_operation
from vibesys.orchestration.issue_queue.strategy._operations import (
    APPLY_KIND,
    INTERPRET_KIND,
    LOG_KIND,
    OPERATIONS,
    READ_KIND,
    RENDER_KIND,
    RETAIN_KIND,
    VERIFY_KIND,
    board_schema_ref,
)
from vibesys.orchestration.issue_queue.strategy._schemas import STRATEGY_ID
from vibesys.orchestration.issue_queue.strategy._state import (
    STATE_SCHEMA,
    IssueQueueStrategyState,
    Phase,
    RunPhase,
)
from vs_core.api import (
    AdoptionFailed,
    AdoptionResult,
    AttemptExhausted,
    AttemptReady,
    AttemptSettled,
    ControlChanged,
    IntentBlocked,
    IntentPhase,
    MeasurementResult,
    OperationResult,
    Proposal,
    Rejected,
    ResumeAuthorized,
    RunEnded,
    RunView,
    StrategyDeclaration,
    StrategyEvent,
    TurnResult,
    TurnSuspended,
    YieldPublicationFailed,
)


@dataclass(frozen=True, slots=True)
class IssueQueueStrategy:
    """Policy of the issue queue, parameterized by its static config."""

    config: IssueQueueConfig
    state: IssueQueueStrategyState = field(default_factory=IssueQueueStrategyState)

    @property
    def declaration(self) -> StrategyDeclaration:
        """Operations and lifecycle capabilities the run must (or may) offer."""
        return StrategyDeclaration(
            strategy_id=STRATEGY_ID,
            state_schema=STATE_SCHEMA,
            required_operations=(
                OPERATIONS.schema_ref(RENDER_KIND),
                board_schema_ref(READ_KIND),
                board_schema_ref(APPLY_KIND),
                board_schema_ref(LOG_KIND),
            ),
            # The kit registers these with the render operation; the strategy never issues them.
            optional_operations=(
                OPERATIONS.schema_ref(VERIFY_KIND),
                OPERATIONS.schema_ref(INTERPRET_KIND),
                OPERATIONS.schema_ref(RETAIN_KIND),
            ),
            optional=frozenset({"suspend"}),
        )

    def bind(self, state: IssueQueueStrategyState) -> "IssueQueueStrategy":
        """The same policy over a different persisted state."""
        return replace(self, state=state)

    def decide(self, view: RunView) -> Proposal[IssueQueueStrategyState]:
        """Propose this revision's ordered decisions and the state that accounts for them."""
        if self.state.phase is RunPhase.FINISHED:
            return Proposal(state=self.state, decisions=())
        state = job_events.settle_retained(self.state, view, self.config)
        state = self._retry_blocked(state, view)
        draft = Draft(view=view, config=self.config, state=state)
        job_decisions.advance(draft)
        job = draft.state.job
        if job is not None and job.phase is Phase.DONE:
            draft.state = job_events.retire(draft.state)
        flow.decide(draft)
        run.decide(draft)
        return Proposal(state=draft.state, decisions=tuple(draft.decisions), wake_at=draft.wake_at)

    def _retry_blocked(
        self, state: IssueQueueStrategyState, view: RunView
    ) -> IssueQueueStrategyState:
        """Send again each board request core blocked, read from the view."""
        blocked = frozenset(
            decision_of_operation(item.operation_id.root)
            for item in view.operations
            if item.phase is IntentPhase.BLOCKED
        )
        if not blocked:
            return state
        state = flow.retry_blocked(state, blocked, self.config.max_board_retries)
        return job_events.retry_blocked(state, blocked, self.config)

    def on_event(self, view: RunView, event: StrategyEvent) -> IssueQueueStrategyState:
        """Fold one core feedback into the policy state."""
        state = self.state
        if isinstance(event, Rejected):
            return self._rejected(state, event)
        if isinstance(event, OperationResult):
            return self._operation(state, event)
        if isinstance(event, TurnResult):
            return job_events.on_turn(state, view, self.config, event)
        if isinstance(event, MeasurementResult):
            return run.on_measurement(state, event)
        return self._lifecycle(state, event)

    def _lifecycle(
        self, state: IssueQueueStrategyState, event: StrategyEvent
    ) -> IssueQueueStrategyState:
        """Fold lifecycle feedback; other feedback needs no state change here."""
        if isinstance(event, AttemptReady):
            return job_events.on_ready(state, event)
        if isinstance(event, AttemptSettled):
            return job_events.on_settled(state, event)
        if isinstance(
            event,
            TurnSuspended
            | YieldPublicationFailed
            | ResumeAuthorized
            | AttemptExhausted
            | IntentBlocked,
        ):
            return self._suspension(state, event)
        return self._run_feedback(state, event)

    def _suspension(
        self,
        state: IssueQueueStrategyState,
        event: TurnSuspended
        | YieldPublicationFailed
        | ResumeAuthorized
        | AttemptExhausted
        | IntentBlocked,
    ) -> IssueQueueStrategyState:
        """Fold the feedback that suspends, resumes, exhausts or blocks the open job."""
        if isinstance(event, TurnSuspended):
            return job_events.on_suspended(state, event)
        if isinstance(event, YieldPublicationFailed):
            return job_events.on_yield_failed(state, event)
        if isinstance(event, ResumeAuthorized):
            return job_events.on_resume(state, event)
        if isinstance(event, AttemptExhausted):
            return job_events.on_exhausted(state, event)
        return run.on_blocked(state, event) or job_events.on_blocked(state, event)

    def _run_feedback(
        self, state: IssueQueueStrategyState, event: StrategyEvent
    ) -> IssueQueueStrategyState:
        if isinstance(event, ControlChanged):
            stop = state.stopping or event.control.action == "stop"
            return state.model_copy(update={"stopping": stop})
        if isinstance(event, AdoptionResult):
            return run.on_adoption(state, event)
        if isinstance(event, AdoptionFailed):
            return run.on_adoption_failed(state, event)
        if isinstance(event, RunEnded):
            return state.model_copy(update={"phase": RunPhase.FINISHED})
        return state

    def _rejected(self, state: IssueQueueStrategyState, event: Rejected) -> IssueQueueStrategyState:
        detail = f"decision rejected: {event.code.value}: {event.detail}"
        own = flow.on_rejected(state, event.decision_id.root, detail)
        if own is not None:
            return own
        adoption = run.on_rejected(state, event)
        if adoption is not None:
            return adoption
        return job_events.on_rejected(state, event)

    def _operation(
        self, state: IssueQueueStrategyState, event: OperationResult
    ) -> IssueQueueStrategyState:
        decision = decision_of_operation(event.operation_id.root)
        own = flow.on_operation(state, event, decision)
        if own is not state:
            return own
        return job_events.on_operation(state, event, self.config)
