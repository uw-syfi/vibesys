"""Shared fixtures for the tests of waits: one registered operation and helpers to drive it.

The pure core with no real time: a test moves the clock by stepping ``ClockAdvanced``.
"""

from typing import ClassVar, Literal

from pydantic import BaseModel

import vs_core.api as core

RUN_DEADLINE = 10_000.0

OPEN = (core.RequestPhase.PREPARED, core.RequestPhase.DISPATCHED, core.RequestPhase.RECONCILING)
"""The phases of a request that has no result yet."""


class Written(core.Value):
    done: bool


class Write(core.OperationRequest):
    kind: Literal["test.until.write"] = "test.until.write"
    lifecycle: Literal[core.LifecycleClass.IDEMPOTENT_WRITE] = core.LifecycleClass.IDEMPOTENT_WRITE
    outcome_model: ClassVar[type[BaseModel]] = Written


def operation_codec() -> core.OperationRegistry:
    descriptor = core.OperationDescriptor(
        kind="test.until.write",
        lifecycle=core.LifecycleClass.IDEMPOTENT_WRITE,
        request_schema=core.SchemaRef(name="write", version=1),
        outcome_schema=core.SchemaRef(name="written", version=1),
    )
    return core.OperationRegistry(
        (
            core.OperationRegistration(
                descriptor=descriptor, request_model=Write, outcome_model=Written
            ),
        )
    )


def fresh() -> tuple[core.CoreState, core.OperationRegistry]:
    codec = operation_codec()
    state = core.initial_state()
    state = state.model_copy(
        update={
            "registry": codec.descriptors,
            "run": state.run.model_copy(
                update={
                    "deadline_at": RUN_DEADLINE,
                    "capabilities": core.Capabilities(operations=codec.descriptors),
                }
            ),
        }
    )
    return state, codec


def wait(codec: core.OperationRegistry, name: str, until: core.Within) -> core.Wait:
    work = codec.validate_decision(
        core.Operation(
            decision_id=core.DecisionId(root=name),
            scope=core.Scope(owner=core.initial_state().run.run_id, generation=0),
            request=Write(),
        )
    )
    return core.Wait(request=work, until=until)


def submit(state: core.CoreState, *waits: core.Wait) -> core.Transition:
    return core.step(
        state, core.ProposalSubmitted(decisions=waits, expected_revision=state.revision)
    )


def dispatch_all(state: core.CoreState) -> core.CoreState:
    for record in state.records.records:
        if record.phase is core.RequestPhase.PREPARED:
            state = core.step(state, core.DispatchAuthorized(request_id=record.request_id)).state
    return state


def operations(state: core.CoreState) -> list[core.RequestRecord]:
    return [
        r for r in state.records.records if isinstance(r.request, core.ExecuteRegisteredOperation)
    ]


def open_records(state: core.CoreState) -> list[core.RequestRecord]:
    """The operations that have no result yet."""
    return [r for r in operations(state) if r.phase in OPEN]


def failures(transition: core.Transition) -> list[core.Failed]:
    return [e for e in transition.events if isinstance(e, core.Failed)]
