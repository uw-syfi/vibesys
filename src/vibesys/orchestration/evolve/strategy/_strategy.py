"""`EvolveStrategy`: the evolutionary search as a pure vs-core `Strategy`.

`decide` runs each subject in turn (settled replies, the opening of a cold start or generation,
the open slots, run selection) over one `Draft`; `on_event` folds one core feedback into the
state. Both are pure functions of the strategy state, the `RunView` and the static
`EvolveConfig`: no clock, no I/O, no randomness beyond the generator state the immutable
population carries.
"""

from dataclasses import dataclass, field, replace

from vibesys.orchestration.evolve.strategy import _gates as gates
from vibesys.orchestration.evolve.strategy import _generation as generation
from vibesys.orchestration.evolve.strategy import _run as run
from vibesys.orchestration.evolve.strategy import _slot_events as slot_events
from vibesys.orchestration.evolve.strategy import _slots as slots
from vibesys.orchestration.evolve.strategy import _turn_events as turn_events
from vibesys.orchestration.evolve.strategy._config import EvolveConfig
from vibesys.orchestration.evolve.strategy._draft import Draft
from vibesys.orchestration.evolve.strategy._operations import (
    EXPORT_PATCH_KIND,
    INTERPRET_KIND,
    OPERATIONS,
    RENDER_KIND,
    RETAIN_KIND,
    VERIFY_KIND,
)
from vibesys.orchestration.evolve.strategy._schemas import STRATEGY_ID
from vibesys.orchestration.evolve.strategy._state import (
    STATE_SCHEMA,
    EvolveStrategyState,
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
class EvolveStrategy:
    """Search policy of the evolutionary loop, parameterized by its static config."""

    config: EvolveConfig
    state: EvolveStrategyState = field(default_factory=EvolveStrategyState)

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
        export = OPERATIONS.schema_ref(EXPORT_PATCH_KIND)
        needs_code = self.config.needs_code
        return StrategyDeclaration(
            strategy_id=STRATEGY_ID,
            state_schema=STATE_SCHEMA,
            required_operations=(*core, retain, export) if needs_code else core,
            optional_operations=() if needs_code else (retain, export),
            optional=frozenset({"suspend"}),
        )

    def bind(self, state: EvolveStrategyState) -> "EvolveStrategy":
        """The same policy over a different persisted state."""
        return replace(self, state=state)

    def decide(self, view: RunView) -> Proposal[EvolveStrategyState]:
        """Propose this revision's ordered decisions and the state that accounts for them."""
        if self.state.phase is RunPhase.FINISHED:
            return Proposal(state=self.state, decisions=())
        state = turn_events.settle_retained(self.state, view)
        draft = Draft(view=view, config=self.config, state=state)
        generation.decide(draft)
        slots.decide(draft)
        run.decide(draft)
        return Proposal(state=draft.state, decisions=tuple(draft.decisions), wake_at=draft.wake_at)

    def on_event(self, view: RunView, event: StrategyEvent) -> EvolveStrategyState:
        """Fold one core feedback into the search state."""
        state = self.state
        if isinstance(event, Rejected):
            return slot_events.on_rejected(state, event)
        if isinstance(event, OperationResult):
            return slot_events.on_operation(state, view, self.config, event)
        if isinstance(event, MeasurementResult):
            return gates.on_measurement(state, event, self.config)
        if isinstance(event, TurnResult):
            return self._turn(state, view, event)
        return self._lifecycle(state, event)

    def _turn(
        self, state: EvolveStrategyState, view: RunView, event: TurnResult
    ) -> EvolveStrategyState:
        """Fold a reply into the slot that asked, if any."""
        slot = turn_events.turn_slot(state, event)
        if slot is None:
            return state
        return turn_events.on_turn(state, view, self.config, slot, event)

    def _lifecycle(self, state: EvolveStrategyState, event: StrategyEvent) -> EvolveStrategyState:
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
            return slot_events.on_exhausted(state, event)
        if isinstance(event, IntentBlocked):
            return slot_events.on_blocked(state, event)
        return self._run_feedback(state, event)

    def _attempt_feedback(
        self,
        state: EvolveStrategyState,
        event: AttemptReady
        | AttemptSettled
        | TurnSuspended
        | YieldPublicationFailed
        | ResumeAuthorized,
    ) -> EvolveStrategyState:
        if isinstance(event, AttemptReady):
            return slot_events.on_ready(state, event)
        if isinstance(event, AttemptSettled):
            settled = slot_events.on_settled(state, event)
            return generation.admit_ready(settled, self.config)
        if isinstance(event, TurnSuspended):
            return turn_events.on_suspended(state, event)
        if isinstance(event, YieldPublicationFailed):
            return turn_events.on_yield_failed(state, event)
        return turn_events.on_resume(state, event)

    @staticmethod
    def _run_feedback(state: EvolveStrategyState, event: StrategyEvent) -> EvolveStrategyState:
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


__all__ = ["EvolveStrategy"]
