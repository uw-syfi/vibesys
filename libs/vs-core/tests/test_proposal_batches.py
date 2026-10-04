"""Ordered proposal batches retain their one observed view revision."""

from typing import ClassVar, Literal

from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel

import vs_core.api as core


class Outcome(core.Value):
    status: Literal["succeeded"] = "succeeded"


class Query(core.OperationRequest):
    kind: Literal["test.query"] = "test.query"
    lifecycle: Literal[core.LifecycleClass.QUERY] = core.LifecycleClass.QUERY
    outcome_model: ClassVar[type[BaseModel]] = Outcome
    value: int


@given(st.integers(min_value=2, max_value=8))
def test_ordered_batch_validates_revision_once_and_preserves_partial_acceptance(count: int) -> None:
    descriptor = core.OperationDescriptor(
        kind="test.query",
        lifecycle=core.LifecycleClass.QUERY,
        request_schema=core.SchemaRef(name="query", version=1),
        outcome_schema=core.SchemaRef(name="outcome", version=1),
    )
    codec = core.OperationRegistry(
        (
            core.OperationRegistration(
                descriptor=descriptor, request_model=Query, outcome_model=Outcome
            ),
        )
    )
    state = core.initial_state()
    state = state.model_copy(
        update={
            "registry": codec.descriptors,
            "run": state.run.model_copy(
                update={"capabilities": core.Capabilities(operations=codec.descriptors)}
            ),
        }
    )
    scope = core.Scope(owner=state.run.run_id, generation=0)
    decisions = tuple(
        codec.validate_decision(
            core.Operation(
                decision_id=core.DecisionId(root=f"d:{index}"),
                scope=scope,
                request=Query(value=index),
                deadline_at=100.0,
            )
        )
        for index in range(count)
    )
    rejected = core.Stop(
        decision_id=core.DecisionId(root="rejected"),
        scope=scope,
        mode="drain",
        result=core.RunResultProposal(outcome="success", reason="no work"),
    )
    ordered = (decisions[0], rejected, *decisions[1:])
    if hasattr(core, "ProposalSubmitted"):
        frames = tuple(
            core.operation_trace(state, decision)
            .frames[0]
            .model_copy(update={"change": core.IntentsChange(state=state.intents)})
            for decision in decisions
        )
        result = core.trace_step(
            state,
            core.ProposalSubmitted(decisions=ordered, expected_revision=0),
            core.ReducerTrace(frames=frames),
        )
    else:
        events = []
        for decision in ordered:
            result = core.step(
                state, core.DecisionSubmitted(decision=decision, expected_revision=0)
            )
            state = result.state
            events.extend(result.events)
        result = result.model_copy(update={"events": tuple(events)})
    assert [event.kind for event in result.events] == [
        "accepted",
        "rejected",
        *(["accepted"] * (count - 1)),
    ]
    assert result.state.revision == 1
    assert len(result.state.run.receipts) == count + 1
    assert len(result.state.intents.intents) == 0


def start_fixture(state: core.CoreState, identity: str) -> core.StartAttempt:
    return core.StartAttempt(
        decision_id=core.DecisionId(root=identity),
        scope=core.Scope(owner=state.run.run_id, generation=0),
        attempt_id=core.AttemptId(root=f"attempt:{identity}"),
        item_id=core.ItemId(root=f"item:{identity}"),
        workspace=core.WorkspacePlan(
            mode=core.WorkspaceMode.EXCLUSIVE_ROOT, base=state.run.facts.baseline
        ),
        budget=core.AttemptBudget(),
    )


def start_signal(start: core.StartAttempt) -> core.AttemptRequested:
    return core.AttemptRequested(
        request=core.AttemptRequest(
            decision_id=start.decision_id,
            attempt_id=start.attempt_id,
            item_id=start.item_id,
            generation=0,
            admission_charge=1,
        )
    )


def test_batch_replay_suppresses_callbacks_before_stale_view_validation() -> None:
    state = core.initial_state()
    start = start_fixture(state, "start")
    event = core.ProposalSubmitted(decisions=(start,), expected_revision=0)
    result = core.trace_step(
        state,
        event,
        core.ReducerTrace(
            frames=(
                core.TraceFrame(
                    signal=start_signal(start), change=core.SchedulingChange(state=state.scheduling)
                ),
            )
        ),
    )
    assert isinstance(result.events[0], core.Accepted)
    replay = core.step(result.state, event)
    assert replay.events == ()
    assert replay.requests == ()
    conflict = start.model_copy(update={"attempt_id": core.AttemptId(root="changed")})
    changed = core.step(
        result.state, core.ProposalSubmitted(decisions=(conflict,), expected_revision=0)
    )
    assert isinstance(changed.events[0], core.Rejected)
    assert changed.events[0].code == core.RejectionCode.IDENTITY_CONFLICT


def test_stale_batch_rejections_are_persisted_and_not_redelivered() -> None:
    state = core.initial_state().model_copy(update={"revision": 5})
    start = start_fixture(state, "stale")
    event = core.ProposalSubmitted(decisions=(start,), expected_revision=0)
    result = core.step(state, event)
    assert isinstance(result.events[0], core.Rejected)
    assert result.events[0].code == core.RejectionCode.STALE_VIEW
    assert len(result.state.run.receipts) == 1
    assert core.step(result.state, event).events == ()


def test_leaf_rejection_rolls_back_only_its_decision_in_an_ordered_batch() -> None:
    state = core.initial_state()
    accepted = start_fixture(state, "accepted")
    rejected = start_fixture(state, "rejected")
    committed = state.scheduling.model_copy(update={"charged": 1})
    rejection = core.Rejected(
        decision_id=rejected.decision_id,
        code=core.RejectionCode.BUDGET,
        path=("max_attempts",),
        detail="exhausted",
    )
    request = core.InspectRequest(
        scope=rejected.scope, deadline_at=100.0, target=core.RequestId(root="target")
    )
    result = core.trace_step(
        state,
        core.ProposalSubmitted(decisions=(accepted, rejected), expected_revision=0),
        core.ReducerTrace(
            frames=(
                core.TraceFrame(
                    signal=start_signal(accepted), change=core.SchedulingChange(state=committed)
                ),
                core.TraceFrame(
                    signal=start_signal(rejected),
                    change=core.SchedulingChange(
                        state=committed.model_copy(update={"charged": 2}),
                        requests=(request,),
                        events=(rejection,),
                    ),
                ),
            )
        ),
    )
    assert result.state.scheduling == committed
    assert result.requests == ()
    assert [event.kind for event in result.events] == ["accepted", "rejected"]
    assert isinstance(result.state.run.receipts[1].feedback, core.Rejected)
