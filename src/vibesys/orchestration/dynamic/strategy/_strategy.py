"""`DynamicStrategy`: the dynamic search as a pure vs-core `Strategy`.

`decide` runs each subject in turn (baseline, workstreams, planner, run
selection) over one `Draft`; `on_event` folds one core feedback into the state.
Both are pure functions of the strategy state, the `RunView` and the static
`DynamicConfig`: no clock, no I/O, no hidden counters.
"""

from dataclasses import dataclass, field, replace

from vibesys.orchestration.dynamic.strategy import _attempt_events as attempt_events
from vibesys.orchestration.dynamic.strategy import _attempts as attempts
from vibesys.orchestration.dynamic.strategy import _baseline as baseline
from vibesys.orchestration.dynamic.strategy import _planner as planner
from vibesys.orchestration.dynamic.strategy import _run as run
from vibesys.orchestration.dynamic.strategy._config import DynamicConfig
from vibesys.orchestration.dynamic.strategy._draft import Draft
from vibesys.orchestration.dynamic.strategy._ids import decision_of_operation
from vibesys.orchestration.dynamic.strategy._operations import (
    INTERPRET_KIND,
    RENDER_KIND,
    RETAIN_KIND,
    VERIFY_KIND,
    EvidenceReadings,
    RenderedArtifacts,
    schema_ref,
)
from vibesys.orchestration.dynamic.strategy._schemas import STRATEGY_ID
from vibesys.orchestration.dynamic.strategy._state import (
    STATE_SCHEMA,
    DynamicStrategyState,
    RunPhase,
)
from vs_core.api import (
    AdoptionResult,
    AttemptExhausted,
    AttemptReady,
    AttemptSettled,
    ControlChanged,
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
)


@dataclass(frozen=True, slots=True)
class DynamicStrategy:
    """Scientific policy of the dynamic search, parameterized by its static config."""

    config: DynamicConfig
    state: DynamicStrategyState = field(default_factory=DynamicStrategyState)

    @property
    def declaration(self) -> StrategyDeclaration:
        """Operations and lifecycle capabilities the run must (or may) offer."""
        return StrategyDeclaration(
            strategy_id=STRATEGY_ID,
            state_schema=STATE_SCHEMA,
            required_operations=(
                schema_ref(RENDER_KIND),
                schema_ref(VERIFY_KIND),
                schema_ref(INTERPRET_KIND),
            ),
            optional_operations=(schema_ref(RETAIN_KIND),),
            optional=frozenset({"suspend"}),
        )

    def bind(self, state: DynamicStrategyState) -> "DynamicStrategy":
        """The same policy over a different persisted state."""
        return replace(self, state=state)

    def decide(self, view: RunView) -> Proposal[DynamicStrategyState]:
        """Propose this revision's ordered decisions and the state that accounts for them."""
        if self.state.phase is RunPhase.FINISHED:
            return Proposal(state=self.state, decisions=())
        draft = Draft(view=view, config=self.config, state=self.state)
        baseline.decide(draft)
        attempts.decide(draft)
        planner.decide(draft)
        run.decide(draft)
        return Proposal(state=draft.state, decisions=tuple(draft.decisions))

    def on_event(self, view: RunView, event: StrategyEvent) -> DynamicStrategyState:
        """Fold one core feedback into the scientific state."""
        state = self.state
        if isinstance(event, Rejected):
            return self._rejected(state, event)
        if isinstance(event, OperationResult):
            return self._operation(state, view, event)
        if isinstance(event, TurnResult):
            return self._turn(state, view, event)
        if isinstance(event, MeasurementResult):
            if event.scope.owner.kind == "run":
                return baseline.on_measurement(state, event, self.config)
            return attempt_events.on_measurement(state, event)
        return self._lifecycle(state, view, event)

    def _turn(
        self, state: DynamicStrategyState, view: RunView, event: TurnResult
    ) -> DynamicStrategyState:
        if planner.owns_turn(state, event):
            return planner.on_turn(state, view, self.config, event)
        return attempt_events.on_turn(state, view, self.config, event)

    def _lifecycle(
        self, state: DynamicStrategyState, view: RunView, event: StrategyEvent
    ) -> DynamicStrategyState:
        """Fold lifecycle feedback; other feedback needs no state change here."""
        if isinstance(event, AttemptReady):
            return attempt_events.on_ready(state, event)
        if isinstance(event, AttemptSettled):
            return attempt_events.on_settled(state, view, self.config, event)
        if isinstance(event, ResumeAuthorized):
            return attempt_events.on_resume(state, event)
        if isinstance(event, AttemptExhausted):
            return attempt_events.on_exhausted(state, event)
        return self._run_feedback(state, event)

    def _run_feedback(
        self, state: DynamicStrategyState, event: StrategyEvent
    ) -> DynamicStrategyState:
        if isinstance(event, ControlChanged):
            stop = state.stopping or event.control.action == "stop"
            return state.model_copy(update={"stopping": stop})
        if isinstance(event, AdoptionResult):
            return run.on_adoption(state, event)
        if isinstance(event, RunEnded):
            return state.model_copy(update={"phase": RunPhase.FINISHED})
        return state

    def _rejected(self, state: DynamicStrategyState, event: Rejected) -> DynamicStrategyState:
        detail = f"decision rejected: {event.code.value}: {event.detail}"
        root = event.decision_id
        if state.planner.awaiting == root:
            return planner.on_rejected(state, detail)
        if state.baseline.awaiting == root:
            return baseline.on_rejected(state, detail)
        return attempt_events.on_rejected(state, event)

    def _operation(
        self, state: DynamicStrategyState, view: RunView, event: OperationResult
    ) -> DynamicStrategyState:
        decision = decision_of_operation(event.operation_id.root)
        outcome = event.outcome
        if (
            isinstance(outcome, RenderedArtifacts)
            and state.planner.awaiting is not None
            and state.planner.awaiting.root == decision
        ):
            return planner.on_rendered(state, outcome)
        if (
            isinstance(outcome, EvidenceReadings)
            and state.baseline.awaiting is not None
            and state.baseline.awaiting.root == decision
        ):
            return baseline.on_readings(state, outcome)
        return attempt_events.on_operation(state, view, event)
