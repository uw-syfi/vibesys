"""`SingleStrategy`: the single-agent hypothesis search as a pure vs-core `Strategy`.

`decide` runs each subject in turn (baseline, designer, open round, run selection) over one
`Draft`; `on_event` folds one core feedback into the state. Both are pure functions of the
strategy state, the `RunView` and the static `SingleConfig`: no clock, no I/O, no hidden
counters.
"""

from dataclasses import dataclass, field, replace

from vibesys.candidates import api as candidates
from vibesys.orchestration.single.strategy import _baseline as baseline
from vibesys.orchestration.single.strategy import _planner as planner
from vibesys.orchestration.single.strategy import _round as round_
from vibesys.orchestration.single.strategy import _round_events as events
from vibesys.orchestration.single.strategy import _run as run
from vibesys.orchestration.single.strategy._config import SingleConfig
from vibesys.orchestration.single.strategy._draft import Draft
from vibesys.orchestration.single.strategy._ids import decision_of_operation
from vibesys.orchestration.single.strategy._operations import (
    CAPTURE_KIND,
    INTERPRET_KIND,
    OPERATIONS,
    RENDER_KIND,
    RETAIN_KIND,
    VERIFY_KIND,
    CommandCapture,
    EvidenceReadings,
    RenderedArtifacts,
)
from vibesys.orchestration.single.strategy._schemas import STRATEGY_ID
from vibesys.orchestration.single.strategy._state import (
    STATE_SCHEMA,
    RunPhase,
    SingleStrategyState,
    Step,
)
from vs_core.api import (
    AdoptionFailed,
    AdoptionResult,
    AttemptExhausted,
    AttemptReady,
    AttemptSettled,
    ControlChanged,
    IntentBlocked,
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
)


@dataclass(frozen=True, slots=True)
class SingleStrategy:
    """Scientific policy of the single-agent search, parameterized by its static config."""

    config: SingleConfig
    state: SingleStrategyState = field(default_factory=SingleStrategyState)

    @property
    def declaration(self) -> StrategyDeclaration:
        """Operations and lifecycle capabilities the run must (or may) offer."""
        # The order is the registry's: core selects the declared operations in declaration
        # order and requires the selection to equal the registry.
        core = (
            OPERATIONS.schema_ref(RENDER_KIND),
            OPERATIONS.schema_ref(VERIFY_KIND),
            OPERATIONS.schema_ref(INTERPRET_KIND),
        )
        rest = (OPERATIONS.schema_ref(RETAIN_KIND), OPERATIONS.schema_ref(CAPTURE_KIND))
        guided = self.config.attribution is not None
        return StrategyDeclaration(
            strategy_id=STRATEGY_ID,
            state_schema=STATE_SCHEMA,
            required_operations=(*core, *rest) if guided else core,
            optional_operations=() if guided else rest,
            optional=frozenset({"suspend"}),
        )

    def bind(self, state: SingleStrategyState) -> "SingleStrategy":
        """The same policy over a different persisted state."""
        return replace(self, state=state)

    def decide(self, view: RunView) -> Proposal[SingleStrategyState]:
        """Propose this revision's ordered decisions and the state that accounts for them."""
        if self.state.phase is RunPhase.FINISHED:
            return Proposal(state=self.state, decisions=())
        state = events.settle_retained(self.state, view, self.config)
        draft = Draft(view=view, config=self.config, state=state)
        baseline.decide(draft)
        planner.decide(draft)
        round_.decide(draft)
        run.decide(draft)
        return Proposal(state=draft.state, decisions=tuple(draft.decisions), wake_at=draft.wake_at)

    def on_event(self, view: RunView, event: StrategyEvent) -> SingleStrategyState:
        """Fold one core feedback into the scientific state."""
        state = self.state
        if isinstance(event, Rejected):
            return self._rejected(state, event)
        if isinstance(event, OperationResult):
            return self._operation(state, view, event)
        if isinstance(event, TurnResult | MeasurementResult):
            return self._answer(state, view, event)
        return self._lifecycle(state, event)

    def _answer(
        self, state: SingleStrategyState, view: RunView, event: TurnResult | MeasurementResult
    ) -> SingleStrategyState:
        """Fold an agent's reply or a measurement's result into the subject awaiting it."""
        if isinstance(event, MeasurementResult):
            if event.scope.owner.kind == "run":
                return state.model_copy(
                    update={
                        "baseline": candidates.on_measurement(
                            state.baseline, view, event, self.config
                        )
                    }
                )
            return events.on_measurement(state, event, self.config)
        if planner.owns_turn(state, event):
            return planner.on_turn(state, view, self.config, event)
        if events.owns_turn(state, event):
            return events.on_turn(state, view, self.config, event)
        return state

    def _lifecycle(self, state: SingleStrategyState, event: StrategyEvent) -> SingleStrategyState:
        """Fold lifecycle feedback; other feedback needs no state change here."""
        if isinstance(event, AttemptReady | AttemptSettled | TurnSuspended | ResumeAuthorized):
            return self._attempt_feedback(state, event)
        if isinstance(event, AttemptExhausted):
            return events.on_exhausted(state, event)
        if isinstance(event, IntentBlocked):
            return self._blocked(state, event)
        return self._run_feedback(state, event)

    def _attempt_feedback(
        self,
        state: SingleStrategyState,
        event: AttemptReady | AttemptSettled | TurnSuspended | ResumeAuthorized,
    ) -> SingleStrategyState:
        if isinstance(event, AttemptReady):
            return events.on_ready(state, event)
        if isinstance(event, AttemptSettled):
            return events.on_settled(state, self.config, event)
        if isinstance(event, TurnSuspended):
            return events.on_suspended(state, event)
        return events.on_resume(state, event)

    def _run_feedback(
        self, state: SingleStrategyState, event: StrategyEvent
    ) -> SingleStrategyState:
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

    def _blocked(self, state: SingleStrategyState, event: IntentBlocked) -> SingleStrategyState:
        """Route a blocked request to the subject awaiting it: designer, baseline or round."""
        detail = f"request blocked: {event.diagnostic}"
        decision = decision_of_operation(event.target.root)
        if self._designer_waits(state, event, decision):
            return planner.on_rejected(state, detail)
        baseline_waits = state.baseline.awaiting is not None and (
            decision == state.baseline.awaiting.root
            or (decision is None and event.scope.owner.kind == "run")
        )
        if baseline_waits:
            return state.model_copy(
                update={"baseline": candidates.on_rejected(state.baseline, detail)}
            )
        return events.on_blocked(state, event)

    @staticmethod
    def _designer_waits(
        state: SingleStrategyState, event: IntentBlocked, decision: str | None
    ) -> bool:
        waiting = state.planner
        by_decision = decision is not None and decision in {
            getattr(waiting.awaiting, "root", None),
            getattr(waiting.capture_awaiting, "root", None),
        }
        run_turn = (
            event.scope.owner.kind == "run"
            and decision is None
            and waiting.step is Step.AWAITING
            and waiting.active
        )
        return by_decision or run_turn

    def _rejected(self, state: SingleStrategyState, event: Rejected) -> SingleStrategyState:
        detail = f"decision rejected: {event.code.value}: {event.detail}"
        root = event.decision_id
        waiting = state.planner
        if root in (waiting.awaiting, waiting.capture_awaiting):
            return planner.on_rejected(state, detail)
        if state.baseline.awaiting == root:
            return state.model_copy(
                update={"baseline": candidates.on_rejected(state.baseline, detail)}
            )
        return events.on_rejected(state, event)

    def _operation(
        self, state: SingleStrategyState, view: RunView, event: OperationResult
    ) -> SingleStrategyState:
        decision = decision_of_operation(event.operation_id.root)
        outcome = event.outcome
        waiting = state.planner
        if (
            isinstance(outcome, RenderedArtifacts)
            and waiting.awaiting is not None
            and waiting.awaiting.root == decision
        ):
            return planner.on_rendered(state, outcome)
        if (
            isinstance(outcome, CommandCapture)
            and waiting.capture_awaiting is not None
            and waiting.capture_awaiting.root == decision
        ):
            return planner.on_captured(state, self.config, outcome)
        if state.baseline.awaiting is not None and state.baseline.awaiting.root == decision:
            return self._baseline_operation(state, view, outcome)
        return events.on_operation(state, view, self.config, event)

    @staticmethod
    def _baseline_operation(
        state: SingleStrategyState, view: RunView, outcome: object
    ) -> SingleStrategyState:

        if not isinstance(outcome, EvidenceReadings):
            return state
        return state.model_copy(
            update={
                "baseline": candidates.on_readings(
                    state.baseline, view, status=outcome.status, readings=outcome.readings
                )
            }
        )
