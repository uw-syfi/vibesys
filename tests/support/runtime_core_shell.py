"""Public kernel trace fixtures for runtime durability tests, without I/O policy."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_core.api import (
    Access,
    ClockAdvanced,
    ContractError,
    CoreEvent,
    CoreState,
    DecisionId,
    DispatchAuthorized,
    EnsureSession,
    InputRecord,
    IntentPhase,
    IntentsChange,
    IntentsContext,
    Proposal,
    ProposalSubmitted,
    RecoveryStarted,
    ReducerTrace,
    Rejected,
    RejectionCode,
    RequestObserved,
    RoleId,
    RunControlEvent,
    RunView,
    SchedulingChange,
    Scope,
    SessionId,
    SessionInputReceived,
    SessionsChange,
    SessionSpec,
    StrategyEvent,
    StrategyState,
    TraceFrame,
    Transition,
    initial_state,
    recover,
    trace_step,
)
from vs_project.api import Committed, FakeStateStore, Unknown
from vs_runtime.api.core import CoreRuntime, CoreRuntimeBindings

if TYPE_CHECKING:
    from vs_project.api import CommitOutcome, StateStore, StoredEnvelope, StoreFence


class CounterState(StrategyState):
    callbacks: int = 0
    proposals: int = 0


class CounterStrategy:
    def __init__(self, state: CounterState | None = None) -> None:
        self.state = state or CounterState(schema_version=1)
        self.declaration = initial_state().run.declaration

    def bind(self, state: CounterState) -> CounterStrategy:
        return CounterStrategy(state)

    def on_event(self, view: RunView, event: StrategyEvent) -> CounterState:
        del view, event
        return self.state.model_copy(update={"callbacks": self.state.callbacks + 1})

    def decide(self, view: RunView) -> Proposal[CounterState]:
        del view
        return Proposal[CounterState](
            state=self.state.model_copy(update={"proposals": self.state.proposals + 1}),
            decisions=(),
        )


class ShellTraceTransitions:
    """Declared leaf outputs exercise the production kernel, not a fake kernel.

    Recovery uses the production reducer. Scheduling's missing leaf supplies
    an explicit no-work frame for these capacity-disabled runs only.
    """

    def __init__(self, *, with_requests: bool = False) -> None:
        self.with_requests = with_requests

    def step(self, state: CoreState, event: CoreEvent) -> Transition:
        frames = []
        if isinstance(event, RecoveryStarted):
            change = recover(
                state.intents,
                IntentsContext(
                    run=state.run,
                    registry=state.registry,
                    attempts=state.attempts,
                    sessions=state.sessions,
                    evaluation=state.evaluation,
                ),
                event,
            )
            frames.append(
                TraceFrame(signal=event, change=IntentsChange(**change.model_dump(mode="python")))
            )
            if change.signals:
                frames.append(
                    TraceFrame(
                        signal=ClockAdvanced(now_at=event.now_at),
                        change=SchedulingChange(state=state.scheduling),
                    )
                )
        elif isinstance(event, ClockAdvanced):
            feedback = Rejected(
                decision_id=DecisionId(root=f"notice-{event.now_at}"),
                code=RejectionCode.IDENTITY_CONFLICT,
                path=("fixture",),
                detail="durable shell callback fixture",
            )
            frames.append(
                TraceFrame(
                    signal=event,
                    change=SchedulingChange(
                        state=state.scheduling,
                        events=(feedback,),
                        requests=(
                            EnsureSession(
                                scope=Scope(
                                    owner=state.run.run_id, generation=state.run.generation
                                ),
                                deadline_at=100,
                                spec=SessionSpec(
                                    session_id=SessionId(root=f"session-{event.now_at}"),
                                    role_id=RoleId(root="worker"),
                                    policy="reuse",
                                    lifetime="owner",
                                    access=Access.WRITE_ARTIFACTS,
                                ),
                            ),
                        )
                        if self.with_requests
                        else (),
                    ),
                )
            )
        elif isinstance(event, DispatchAuthorized):
            records = tuple(
                row.model_copy(update={"phase": IntentPhase.DISPATCHED})
                if row.request_id == event.request_id
                else row
                for row in state.intents.intents
            )
            frames.append(
                TraceFrame(
                    signal=event,
                    change=IntentsChange(
                        state=state.intents.model_copy(update={"intents": records})
                    ),
                )
            )
        elif isinstance(event, RequestObserved):
            records = tuple(
                row.model_copy(
                    update={
                        "phase": IntentPhase.RECONCILING,
                        "observation": event.observation,
                        "sequence": event.observation.sequence,
                    }
                )
                if row.request_id == event.observation.request_id
                else row
                for row in state.intents.intents
            )
            frames.append(
                TraceFrame(
                    signal=event,
                    change=IntentsChange(
                        state=state.intents.model_copy(update={"intents": records})
                    ),
                )
            )
        elif isinstance(event, SessionInputReceived):
            return self._input_occurrence(state, event)
        elif not isinstance(event, ProposalSubmitted | RunControlEvent):
            message = f"unsupported shell trace input {event.kind}"
            raise TypeError(message)
        return trace_step(state, event, ReducerTrace(frames=tuple(frames)))

    @staticmethod
    def _input_occurrence(state: CoreState, event: SessionInputReceived) -> Transition:
        """Declared pending-occurrence storage; no reservation/delivery policy."""
        existing = next(
            (row for row in state.sessions.inputs if row.input.input_id == event.input.input_id),
            None,
        )
        if existing is not None and existing.input != event.input:
            raise ContractError(("input_id",), "occurrence identity payload conflict")
        records = (
            state.sessions.inputs
            if existing is not None
            else (*state.sessions.inputs, InputRecord(input=event.input))
        )
        change = SessionsChange(state=state.sessions.model_copy(update={"inputs": records}))
        return trace_step(
            state, event, ReducerTrace(frames=(TraceFrame(signal=event, change=change),))
        )


def runtime(store: StateStore) -> CoreRuntime[CounterState]:
    return CoreRuntime(
        store,
        CounterStrategy(),
        initial_state(),
        bindings=CoreRuntimeBindings(transitions=ShellTraceTransitions()),
    )


class LostAcknowledgementStateStore(FakeStateStore):
    """Actual whole-record commit, then deterministic acknowledgement loss."""

    def __init__(self, unknown_revision: int) -> None:
        super().__init__()
        self.unknown_revision = unknown_revision

    def commit(
        self, expected_revision: int | None, envelope: StoredEnvelope, fence: StoreFence, now: float
    ) -> CommitOutcome:
        result = super().commit(expected_revision, envelope, fence, now)
        if isinstance(result, Committed) and envelope.revision == self.unknown_revision:
            return Unknown(revision=envelope.revision)
        return result
