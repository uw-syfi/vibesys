"""D199: a capability-disabled sequential strategy drives kernel trace wiring.

Declared trace outputs stand in for wave-1 reducers only. This acceptance test
checks the shared kernel contract, not scheduler, cleanup or session algorithms.
"""

from __future__ import annotations

from typing import Literal

from vs_core.api import (
    Access,
    AdmissionControl,
    AdmitAttempt,
    AssessmentSubmitted,
    AttemptAdmitted,
    AttemptBudget,
    AttemptId,
    AttemptPhase,
    AttemptReady,
    AttemptRef,
    AttemptRequest,
    AttemptRequested,
    AttemptsChange,
    AttemptSettled,
    AttemptsState,
    AttemptView,
    CloseSession,
    CoreEvent,
    CoreState,
    DecisionId,
    DecisionSubmitted,
    DispatchTurn,
    EnsureWorkspace,
    EventId,
    InvocationId,
    InvocationRef,
    ItemId,
    Observation,
    ObservationStatus,
    Proposal,
    ReducerTrace,
    RequestTurn,
    RoleId,
    RunDrained,
    RunEnded,
    RunResultProposal,
    RunStatus,
    RunView,
    SchedulingChange,
    SchemaRef,
    Scope,
    SessionId,
    SessionPhase,
    SessionsChange,
    SessionSpec,
    SessionsState,
    SessionView,
    Settle,
    Settlement,
    SettlementChange,
    SettlementId,
    SettlementState,
    Slot,
    SlotReleased,
    StartAttempt,
    Stop,
    StrategyDeclaration,
    StrategyEvent,
    StrategyState,
    TraceFrame,
    Transition,
    TurnObserved,
    TurnRequested,
    TurnResult,
    TurnSpec,
    Value,
    Withdraw,
    WorkspaceMode,
    WorkspaceObserved,
    WorkspacePlan,
    WorkspaceRef,
    initial_state,
    project,
    step,
    trace_step,
)


class SequentialState(StrategyState):
    next_index: int = 0
    completed_ids: tuple[ItemId, ...] = ()
    phase: Literal["start", "turn", "wait", "settle", "finish"] = "start"


class Sequential(Value):
    state: SequentialState
    declaration: StrategyDeclaration
    items: tuple[ItemId, ...]

    def bind(self, state: SequentialState) -> Sequential:
        return self.model_copy(update={"state": state})

    def decide(self, view: RunView) -> Proposal[SequentialState]:
        scope = Scope(owner=view.run.run_id, generation=view.run.generation)
        index = self.state.next_index
        attempt = AttemptRef(attempt_id=AttemptId(root=f"attempt:{index}"), generation=0)
        if self.state.phase == "start":
            decision = StartAttempt(
                decision_id=DecisionId(root=f"start:{index}"),
                scope=scope,
                attempt_id=attempt.attempt_id,
                item_id=self.items[index],
                workspace=WorkspacePlan(
                    mode=WorkspaceMode.EXCLUSIVE_ROOT, base=view.facts.baseline
                ),
                budget=AttemptBudget(),
            )
        elif self.state.phase == "turn":
            session = SessionSpec(
                session_id=SessionId(root="planner-session"),
                role_id=RoleId(root="planner"),
                policy="reuse",
                lifetime="owner",
                access=Access.READ_ONLY,
            )
            turn = TurnSpec(
                session=session,
                invocation_id=InvocationId(root=f"invocation:{index}"),
                workspace=WorkspaceRef(
                    scope=Scope(owner=attempt.attempt_id, generation=0),
                    revision=view.facts.baseline,
                    mode=WorkspaceMode.EXCLUSIVE_ROOT,
                ),
                prompts=(),
                output_schema=SchemaRef(name="opaque-result", version=1),
                deadline_at=100.0,
                charge_class="paid",
            )
            decision = RequestTurn(
                decision_id=DecisionId(root=f"turn:{index}"),
                scope=Scope(owner=attempt.attempt_id, generation=0),
                turn=turn,
            )
        elif self.state.phase == "settle":
            decision = Withdraw(
                decision_id=DecisionId(root=f"settle:{index}"),
                scope=scope,
                target=attempt,
                disposition=Settle(
                    assessments=(), eligible=False, retention="discard", outcome="succeeded"
                ),
            )
        elif self.state.phase == "finish":
            decision = Stop(
                decision_id=DecisionId(root="finish"),
                scope=scope,
                mode="drain",
                result=RunResultProposal(outcome="success", reason="two opaque items completed"),
            )
        else:
            return Proposal(state=self.state, decisions=())
        return Proposal(state=self.state, decisions=(decision,))

    def on_event(self, view: RunView, event: StrategyEvent) -> SequentialState:
        del view
        if isinstance(event, AttemptReady):
            return self.state.model_copy(update={"phase": "turn"})
        if isinstance(event, TurnResult):
            return self.state.model_copy(update={"phase": "settle"})
        if isinstance(event, AttemptSettled):
            index = self.state.next_index + 1
            return self.state.model_copy(
                update={
                    "next_index": index,
                    "completed_ids": (*self.state.completed_ids, self.items[index - 1]),
                    "phase": "finish" if index == len(self.items) else "start",
                }
            )
        return self.state


def consume_trace(state: CoreState, event: CoreEvent, frames: list[TraceFrame]) -> Transition:
    """Kernel input is immutable and every replay yields the same transition."""
    before = state.model_dump_json()
    trace = ReducerTrace(frames=tuple(frames))
    result = trace_step(state, event, trace)
    assert result == trace_step(state, event, trace)
    assert state.model_dump_json() == before
    assert result.state.revision == state.revision + 1
    assert len(result.state.scheduling.slots) <= 1
    return result


def deliver(strategy: Sequential, result: Transition) -> Sequential:
    """The shell owns the separate strategy callback and decision calls."""
    for event in result.events:
        strategy = strategy.bind(strategy.on_event(project(result.state), event))
    return strategy


def exercise_item(
    state: CoreState, strategy: Sequential, index: int
) -> tuple[CoreState, Sequential]:
    """Replay one ordinary lifecycle item through declared area outputs."""
    decision = strategy.decide(project(state)).decisions[0]
    assert isinstance(decision, StartAttempt)
    request = AttemptRequest(
        decision_id=decision.decision_id,
        attempt_id=decision.attempt_id,
        item_id=decision.item_id,
        generation=0,
        admission_charge=1,
    )
    attempt = AttemptRef(attempt_id=decision.attempt_id, generation=0)
    slot = Slot(attempt=attempt)
    owned = AttemptView(
        attempt_id=decision.attempt_id,
        item_id=decision.item_id,
        generation=0,
        phase=AttemptPhase.ACTIVE,
        workspace=decision.workspace,
        budget=decision.budget,
    )
    admitted = AttemptAdmitted(
        request=request, workspace=decision.workspace, budget=decision.budget
    )
    proposed = EnsureWorkspace(
        scope=Scope(owner=attempt.attempt_id, generation=0),
        deadline_at=100.0,
        attempt=attempt,
        plan=decision.workspace,
    )
    scheduled = state.scheduling.model_copy(update={"slots": (slot,), "charged": index + 1})
    active = AttemptsState(attempts=(*state.attempts.attempts, owned))
    result = consume_trace(
        state,
        DecisionSubmitted(decision=decision, expected_revision=state.revision),
        [
            TraceFrame(
                signal=AttemptRequested(request=request),
                change=SchedulingChange(state=scheduled, signals=(AdmitAttempt(request=request),)),
            ),
            TraceFrame(signal=admitted, change=AttemptsChange(state=active, requests=(proposed,))),
        ],
    )
    strategy = deliver(strategy, result)
    state = type(state).model_validate_json(result.state.model_dump_json())
    workspace_request = result.requests[0]
    assert state.intents.intents[-1].request_id == workspace_request.request_id
    ready = AttemptReady(attempt=attempt)
    observation = Observation(
        event_id=EventId(root=f"workspace:{index}"),
        request_id=workspace_request.request_id,
        scope=workspace_request.scope,
        sequence=1,
        observed_at=1.0,
        status=ObservationStatus.SUCCEEDED,
        accepted=True,
        terminal=True,
    )
    event = WorkspaceObserved(
        attempt=attempt, observation=observation, revision=state.run.facts.baseline
    )
    result = consume_trace(
        state,
        event,
        [
            TraceFrame(
                signal=event,
                change=AttemptsChange(state=active, signals=(ready,), events=(ready,)),
            ),
            TraceFrame(signal=ready, change=SchedulingChange(state=scheduled)),
        ],
    )
    state, strategy = result.state, deliver(strategy, result)
    decision = strategy.decide(project(state)).decisions[0]
    assert isinstance(decision, RequestTurn)
    requested = TurnRequested(scope=decision.scope, turn=decision.turn)
    session = SessionView(
        spec=decision.turn.session,
        scope=Scope(owner=state.run.run_id, generation=0),
        generation=0,
        phase=SessionPhase.EXECUTING,
        invocation=decision.turn.invocation_id,
    )
    result = consume_trace(
        state,
        DecisionSubmitted(decision=decision, expected_revision=state.revision),
        [
            TraceFrame(
                signal=requested,
                change=SessionsChange(
                    state=SessionsState(sessions=(session,)),
                    requests=(
                        DispatchTurn(scope=decision.scope, deadline_at=100.0, turn=decision.turn),
                    ),
                ),
            )
        ],
    )
    state = result.state
    duplicate = step(state, DecisionSubmitted(decision=decision, expected_revision=0))
    assert duplicate.requests == duplicate.events == ()
    request = result.requests[0]
    assert request.request_id is not None
    invocation = InvocationRef(
        session_id=decision.turn.session.session_id,
        invocation_id=decision.turn.invocation_id,
        generation=0,
    )
    observation = Observation(
        event_id=EventId(root=f"turn:{index}"),
        request_id=request.request_id,
        scope=request.scope,
        sequence=1,
        observed_at=2.0,
        status=ObservationStatus.SUCCEEDED,
        accepted=True,
        terminal=True,
        released=True,
    )
    observed = TurnObserved(invocation=invocation, observation=observation)
    result = consume_trace(
        state,
        observed,
        [
            TraceFrame(
                signal=observed,
                change=SessionsChange(
                    state=SessionsState(
                        sessions=(session.model_copy(update={"phase": SessionPhase.IDLE}),)
                    ),
                    events=(TurnResult(invocation=invocation, observation=observation),),
                ),
            )
        ],
    )
    state, strategy = result.state, deliver(strategy, result)
    decision = strategy.decide(project(state)).decisions[0]
    assert isinstance(decision, Withdraw)
    settlement = Settlement(
        settlement_id=SettlementId(root=f"settlement:settle:{index}"),
        attempt=attempt,
        candidate=None,
        assessments=(),
        eligible=False,
        retention="discard",
        outcome="succeeded",
    )
    released = SlotReleased(attempt=attempt)
    result = consume_trace(
        state,
        DecisionSubmitted(decision=decision, expected_revision=state.revision),
        [
            TraceFrame(
                signal=AssessmentSubmitted(settlement=settlement),
                change=SettlementChange(
                    state=SettlementState(settlements=(*state.settlement.settlements, settlement)),
                    signals=(released,),
                    events=(AttemptSettled(settlement=settlement),),
                ),
            ),
            TraceFrame(
                signal=released,
                change=SchedulingChange(state=scheduled.model_copy(update={"slots": ()})),
            ),
        ],
    )
    state, strategy = result.state, deliver(strategy, result)
    return state, strategy


def test_sequential_strategy_has_no_optional_capabilities_or_product_state() -> None:
    state = initial_state()
    strategy = Sequential(
        state=SequentialState(schema_version=1),
        declaration=state.run.declaration,
        items=(ItemId(root="planner-item"), ItemId(root="opaque-item")),
    )
    assert strategy.declaration.required == strategy.declaration.optional == frozenset()
    assert state.run.capabilities.lifecycle == frozenset()
    for index in range(2):
        state, strategy = exercise_item(state, strategy, index)
    assert strategy.state.completed_ids == strategy.items
    decision = strategy.decide(project(state)).decisions[0]
    assert isinstance(decision, Stop)
    terminal = CloseSession(
        scope=Scope(owner=state.run.run_id, generation=0),
        deadline_at=100.0,
        session_id=SessionId(root="planner-session"),
    )
    result = consume_trace(
        state,
        DecisionSubmitted(decision=decision, expected_revision=state.revision),
        [
            TraceFrame(
                signal=AdmissionControl(action="drain"),
                change=SchedulingChange(
                    state=state.scheduling.model_copy(update={"admission_closed": True}),
                    requests=(terminal,),
                    signals=(RunDrained(),),
                ),
            )
        ],
    )
    assert result.state.run.status == RunStatus.TERMINAL
    assert len(result.requests) == 1
    assert sum(isinstance(event, RunEnded) for event in result.events) == 1
    assert (
        step(result.state, DecisionSubmitted(decision=decision, expected_revision=0)).requests == ()
    )
