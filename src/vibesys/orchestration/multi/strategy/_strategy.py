"""`MultiStrategy`: the multi-agent hypothesis search as a pure vs-core `Strategy`.

`decide` runs each subject in turn (baseline, the design steps, the open round, run selection)
over one `Draft`; `on_event` folds one core feedback into the state. Both are pure functions of
the strategy state, the `RunView` and the static `MultiConfig`: no clock, no I/O, no hidden
counters.
"""

from dataclasses import dataclass, field, replace

from vibesys.candidates import api as candidates
from vibesys.orchestration.multi.strategy import _baseline as baseline
from vibesys.orchestration.multi.strategy import _gates as gates
from vibesys.orchestration.multi.strategy import _round as round_
from vibesys.orchestration.multi.strategy import _round_events as round_events
from vibesys.orchestration.multi.strategy import _run as run
from vibesys.orchestration.multi.strategy import _tracks as tracks
from vibesys.orchestration.multi.strategy import _turn_events as turn_events
from vibesys.orchestration.multi.strategy._config import MultiConfig
from vibesys.orchestration.multi.strategy._draft import Draft
from vibesys.orchestration.multi.strategy._ids import decision_of_operation
from vibesys.orchestration.multi.strategy._operations import (
    CAPTURE_KIND,
    INTERPRET_KIND,
    OPERATIONS,
    RENDER_KIND,
    RETAIN_KIND,
    VALIDATE_LOCAL_KIND,
    VERIFY_KIND,
    CommandCapture,
    EvidenceReadings,
    RenderedArtifacts,
)
from vibesys.orchestration.multi.strategy._schemas import STRATEGY_ID
from vibesys.orchestration.multi.strategy._state import (
    STATE_SCHEMA,
    MultiStrategyState,
    RunPhase,
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
    YieldPublicationFailed,
)


@dataclass(frozen=True, slots=True)
class MultiStrategy:
    """Scientific policy of the multi-agent search, parameterized by its static config."""

    config: MultiConfig
    state: MultiStrategyState = field(default_factory=MultiStrategyState)

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
        retain = OPERATIONS.schema_ref(RETAIN_KIND)
        capture = OPERATIONS.schema_ref(CAPTURE_KIND)
        validate = OPERATIONS.schema_ref(VALIDATE_LOCAL_KIND)
        guided = self.config.attribution is not None
        return StrategyDeclaration(
            strategy_id=STRATEGY_ID,
            state_schema=STATE_SCHEMA,
            required_operations=(*core, retain, capture) if guided else core,
            optional_operations=(validate,) if guided else (retain, capture, validate),
            optional=frozenset({"suspend"}),
        )

    def bind(self, state: MultiStrategyState) -> "MultiStrategy":
        """The same policy over a different persisted state."""
        return replace(self, state=state)

    def decide(self, view: RunView) -> Proposal[MultiStrategyState]:
        """Propose this revision's ordered decisions and the state that accounts for them."""
        if self.state.phase is RunPhase.FINISHED:
            return Proposal(state=self.state, decisions=())
        state = turn_events.settle_retained(self.state, view, self.config)
        draft = Draft(view=view, config=self.config, state=state)
        # A round is done in an event (a start core refused) or in `round_.decide` (an unstarted
        # round a stop ends). Retiring follows from that state, never from the event kind.
        draft.state = round_events.retire(draft.state, self.config)
        baseline.decide(draft)
        tracks.decide(draft)
        round_.decide(draft)
        draft.state = round_events.retire(draft.state, self.config)
        run.decide(draft)
        return Proposal(state=draft.state, decisions=tuple(draft.decisions), wake_at=draft.wake_at)

    def on_event(self, view: RunView, event: StrategyEvent) -> MultiStrategyState:
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
        self, state: MultiStrategyState, view: RunView, event: TurnResult | MeasurementResult
    ) -> MultiStrategyState:
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
            return gates.on_measurement(state, event, self.config)
        if tracks.owns_turn(state, event):
            return tracks.on_turn(state, view, self.config, event)
        if turn_events.owns_turn(state, event):
            return turn_events.on_turn(state, view, self.config, event)
        return state

    def _lifecycle(self, state: MultiStrategyState, event: StrategyEvent) -> MultiStrategyState:
        """Fold lifecycle feedback; other feedback needs no state change here."""
        if isinstance(
            event,
            AttemptReady
            | AttemptSettled
            | TurnSuspended
            | YieldPublicationFailed
            | ResumeAuthorized,
        ):
            return self._attempt_feedback(state, event)
        if isinstance(event, AttemptExhausted):
            return round_events.on_exhausted(state, event)
        if isinstance(event, IntentBlocked):
            return self._blocked(state, event)
        return self._run_feedback(state, event)

    def _attempt_feedback(
        self,
        state: MultiStrategyState,
        event: AttemptReady
        | AttemptSettled
        | TurnSuspended
        | YieldPublicationFailed
        | ResumeAuthorized,
    ) -> MultiStrategyState:
        if isinstance(event, AttemptReady):
            return round_events.on_ready(state, event)
        if isinstance(event, AttemptSettled):
            return round_events.on_settled(state, self.config, event)
        if isinstance(event, TurnSuspended):
            return turn_events.on_suspended(state, event)
        if isinstance(event, YieldPublicationFailed):
            return turn_events.on_yield_failed(state, event)
        return turn_events.on_resume(state, event)

    def _run_feedback(self, state: MultiStrategyState, event: StrategyEvent) -> MultiStrategyState:
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

    def _blocked(self, state: MultiStrategyState, event: IntentBlocked) -> MultiStrategyState:
        """Route a blocked request to the subject awaiting it: design, baseline or round."""
        detail = f"request blocked: {event.diagnostic}"
        decision = decision_of_operation(event.target.root)
        design = state.design
        design_waits = tracks.awaits(state, decision) or (
            event.scope.owner.kind == "run"
            and decision is None
            and design.step is Step.AWAITING
            and design.active
        )
        if design_waits:
            return tracks.on_rejected(state, detail)
        baseline_waits = state.baseline.awaiting is not None and (
            decision == state.baseline.awaiting.root
            or (decision is None and event.scope.owner.kind == "run")
        )
        if baseline_waits:
            return state.model_copy(
                update={"baseline": candidates.on_rejected(state.baseline, detail)}
            )
        return round_events.on_blocked(state, event)

    def _rejected(self, state: MultiStrategyState, event: Rejected) -> MultiStrategyState:
        detail = f"decision rejected: {event.code.value}: {event.detail}"
        root = event.decision_id
        if tracks.awaits(state, root.root):
            return tracks.on_rejected(state, detail)
        if state.baseline.awaiting == root:
            return state.model_copy(
                update={"baseline": candidates.on_rejected(state.baseline, detail)}
            )
        return round_events.on_rejected(state, event)

    def _operation(
        self, state: MultiStrategyState, view: RunView, event: OperationResult
    ) -> MultiStrategyState:
        decision = decision_of_operation(event.operation_id.root)
        outcome = event.outcome
        if tracks.awaits(state, decision):
            if isinstance(outcome, RenderedArtifacts):
                return tracks.on_rendered(state, outcome)
            if isinstance(outcome, CommandCapture):
                return tracks.on_captured(state, self.config, outcome)
            return state
        if state.baseline.awaiting is not None and state.baseline.awaiting.root == decision:
            return self._baseline_operation(state, view, outcome)
        return round_events.on_operation(state, view, self.config, event)

    @staticmethod
    def _baseline_operation(
        state: MultiStrategyState, view: RunView, outcome: object
    ) -> MultiStrategyState:
        if not isinstance(outcome, EvidenceReadings):
            return state
        return state.model_copy(
            update={
                "baseline": candidates.on_readings(
                    state.baseline, view, status=outcome.status, readings=outcome.readings
                )
            }
        )
