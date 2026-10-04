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
