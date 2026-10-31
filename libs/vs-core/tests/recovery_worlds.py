"""Runs as a restart finds them, for the recovery properties: every kind of step, in scopes.

A world is a seed state the way a host hands it to the fold: open steps of the four kinds the rule
treats differently (a query that may run twice, a job that outlives its process, a writer, a
snapshot that must not read a half-written tree), spread over scopes, in the standing a log of an
earlier version leaves them (dispatched or reconciling with no attempt row, reconciliation
requests still open), under the epoch number the host counted. A step's limit is its own.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import ClassVar, Literal

from hypothesis import strategies as st

import vs_core.api as core

from .proof_digest import value_digest


class Kind(StrEnum):
    """The step kinds the recovery rule orders or releases differently."""

    QUERY = "test.query"
    JOB = "test.job"
    WRITE = "test.write"
    SNAPSHOT = "test.snapshot"


LIFECYCLE = {
    Kind.QUERY: core.LifecycleClass.QUERY,
    Kind.JOB: core.LifecycleClass.OWNED_JOB,
    Kind.WRITE: core.LifecycleClass.IDEMPOTENT_WRITE,
    Kind.SNAPSHOT: core.LifecycleClass.IDEMPOTENT_WRITE,
}


class Outcome(core.Value):
    status: Literal["succeeded"] = "succeeded"


class Query(core.OperationRequest):
    kind: Literal["test.query"] = "test.query"
    lifecycle: Literal[core.LifecycleClass.QUERY] = core.LifecycleClass.QUERY
    outcome_model: ClassVar[type[Outcome]] = Outcome
    value: int


class Job(core.OperationRequest):
    kind: Literal["test.job"] = "test.job"
    lifecycle: Literal[core.LifecycleClass.OWNED_JOB] = core.LifecycleClass.OWNED_JOB
    outcome_model: ClassVar[type[Outcome]] = Outcome
    value: int


class Write(core.OperationRequest):
    kind: Literal["test.write"] = "test.write"
    lifecycle: Literal[core.LifecycleClass.IDEMPOTENT_WRITE] = core.LifecycleClass.IDEMPOTENT_WRITE
    outcome_model: ClassVar[type[Outcome]] = Outcome
    value: int


class Snapshot(core.OperationRequest):
    kind: Literal["test.snapshot"] = "test.snapshot"
    lifecycle: Literal[core.LifecycleClass.IDEMPOTENT_WRITE] = core.LifecycleClass.IDEMPOTENT_WRITE
    outcome_model: ClassVar[type[Outcome]] = Outcome
    value: int


MODELS: dict[Kind, type[core.OperationRequest]] = {
    Kind.QUERY: Query,
    Kind.JOB: Job,
    Kind.WRITE: Write,
    Kind.SNAPSHOT: Snapshot,
}


def descriptor(kind: Kind) -> core.OperationDescriptor:
    return core.OperationDescriptor(
        kind=kind.value,
        lifecycle=LIFECYCLE[kind],
        request_schema=core.SchemaRef(name=f"{kind.value}-request", version=1),
        outcome_schema=core.SchemaRef(name=f"{kind.value}-outcome", version=1),
        resource_pool=core.PoolId(root="jobs") if kind is Kind.JOB else None,
        revision_authority=(
            core.RevisionAuthority.SNAPSHOT
            if kind is Kind.SNAPSHOT
            else core.RevisionAuthority.NONE
        ),
    )


class Standing(StrEnum):
    """How a log of an earlier version leaves a step: no attempt row, but in flight."""

    PREPARED = "prepared"
    DISPATCHED = "dispatched"
    RECONCILING = "reconciling"


class Aux(StrEnum):
    """The requests of reconciliation an earlier version leaves open beside the steps."""

    INSPECT = "inspect"
    CANCEL_RESOURCE = "cancel_resource"
    BLOCK = "block"


@dataclass(frozen=True)
class StepSpec:
    limit: float
    kind: Kind = Kind.QUERY
    scope: int = 0
    standing: Standing = Standing.PREPARED
    after_close: bool = False
    """The step waits for the close of its scope (the world's first close) to end."""


@dataclass(frozen=True)
class World:
    steps: tuple[StepSpec, ...]
    retries: int = 3
    host_epoch: int = 0
    """The epoch the host counted when it took the seed, which the log's numbering starts after."""
    aux: tuple[Aux, ...] = ()
    closes: tuple[int, ...] = ()
    """The scopes whose close is open beside their steps."""
    retains: tuple[int, ...] = ()
    """The scopes whose snapshot-and-retain, which a drain lets through, is open beside their steps."""


def build(world: World) -> tuple[core.CoreState, core.OperationRegistry]:
    """The seed state of ``world`` and the registry that decodes the outcomes of its steps."""
    registry = core.OperationRegistry(
        tuple(
            core.OperationRegistration(
                descriptor=descriptor(kind), request_model=MODELS[kind], outcome_model=Outcome
            )
            for kind in Kind
        )
    )
    state = core.initial_state()
    bound = state.run.limits.request_retries.model_copy(update={"limit": world.retries})
    run = state.run.model_copy(
        update={
            "capabilities": core.Capabilities(operations=registry.descriptors),
            "limits": state.run.limits.model_copy(update={"request_retries": bound}),
        }
    )
    state = state.model_copy(update={"registry": registry.descriptors, "run": run})
    scope = core.Scope(owner=state.run.run_id, generation=0)
    for index, spec in enumerate(world.steps):
        decision = registry.validate_decision(
            core.Operation(
                decision_id=core.DecisionId(root=f"d:{index}"),
                scope=scope,
                request=Query(value=index),
            )
        )
        submitted = core.DecisionSubmitted(
            decision=core.Wait(request=decision, until=core.Within(limit=spec.limit)),
            expected_revision=state.revision,
        )
        state = core.step(state, submitted).state
    records = tuple(_reshaped(state, row, spec) for row, spec in _zip(state, world))
    records = (
        *records,
        *_aux(state, records, world),
        *_closes(state, world),
        *_retains(state, world),
    )
    recovery = state.records.recovery.model_copy(update={"epoch": world.host_epoch})
    return (
        state.model_copy(
            update={
                "records": state.records.model_copy(
                    update={"records": records, "recovery": recovery}
                )
            }
        ),
        registry,
    )


def _zip(state: core.CoreState, world: World) -> list[tuple[core.RequestRecord, StepSpec]]:
    assert len(state.records.records) == len(world.steps)
    return list(zip(state.records.records, world.steps, strict=True))


def _reshaped(state: core.CoreState, row: core.RequestRecord, spec: StepSpec) -> core.RequestRecord:
    request = row.request
    assert isinstance(request, core.ExecuteRegisteredOperation)
    wire = request.operation
    declared = descriptor(spec.kind)
    reference = wire.schema_ref.model_copy(
        update={
            "kind": spec.kind.value,
            "lifecycle": declared.lifecycle,
            "request_schema": declared.request_schema,
            "outcome_schema": declared.outcome_schema,
        }
    )
    waits = (core.RequestId(root="close-0"),) if spec.after_close else ()
    request = request.model_copy(
        update={
            "depends_on": waits,
            "scope": core.Scope(owner=state.run.run_id, generation=spec.scope),
            "operation": wire.model_copy(update={"schema_ref": reference}),
        }
    )
    phase = {
        Standing.PREPARED: core.RequestPhase.PREPARED,
        Standing.DISPATCHED: core.RequestPhase.DISPATCHED,
        Standing.RECONCILING: core.RequestPhase.RECONCILING,
    }[spec.standing]
    reshaped = row.model_copy(
        update={
            "request": request,
            "payload_digest": value_digest(request),
            "lifecycle": LIFECYCLE[spec.kind],
            "phase": phase,
        }
    )
    if spec.standing is Standing.PREPARED:
        return reshaped
    # A log of an earlier version dispatched the step: its limit counted from then.
    return reshaped.model_copy(update={"dispatched_at": 0.0, "until": core.Until(at=spec.limit)})


def _aux(
    state: core.CoreState, steps: tuple[core.RequestRecord, ...], world: World
) -> tuple[core.RequestRecord, ...]:
    if not world.aux:
        return ()
    target = steps[0]
    scope = target.request.scope
    made: list[core.RequestRecord] = []
    for index, aux in enumerate(world.aux):
        request_id = core.RequestId(root=f"aux-{index}")
        request: core.Request
        match aux:
            case Aux.INSPECT:
                request = core.InspectRequest(
                    request_id=request_id, scope=scope, target=target.request_id
                )
            case Aux.CANCEL_RESOURCE:
                request = core.CancelOwnedResource(
                    request_id=request_id,
                    target=target.request_id,
                    resource_id=core.ResourceId(root=f"resource-{index}"),
                    scope=scope,
                )
            case Aux.BLOCK:
                request = core.BlockRequest(
                    request_id=request_id,
                    target=target.request_id,
                    reason=core.FailureReason.TIMED_OUT,
                    scope=scope,
                )
        made.append(
            core.RequestRecord(
                request_id=request_id,
                request=request,
                payload_digest=request_id.root,
                lifecycle=core.LifecycleClass.QUERY,
                phase=core.RequestPhase.DISPATCHED,
                until=core.Until(at=state.run.now_at + 100.0),
                addressed_by=(),
            )
        )
    return tuple(made)


def _closes(state: core.CoreState, world: World) -> tuple[core.RequestRecord, ...]:
    made = []
    for index, scope in enumerate(world.closes):
        request_id = core.RequestId(root=f"close-{index}")
        request = core.CloseAttemptScope(
            request_id=request_id,
            scope=core.Scope(owner=state.run.run_id, generation=scope),
            attempt=core.AttemptRef(
                attempt_id=core.AttemptId(root=f"attempt-{index}"), generation=scope
            ),
        )
        made.append(
            core.RequestRecord(
                request_id=request_id,
                request=request,
                payload_digest=value_digest(request),
                lifecycle=core.LifecycleClass.IDEMPOTENT_WRITE,
                phase=core.RequestPhase.PREPARED,
                until=core.Until(at=100.0),
                addressed_by=(),
            )
        )
    return tuple(made)


def _retains(state: core.CoreState, world: World) -> tuple[core.RequestRecord, ...]:
    made = []
    for index, scope in enumerate(world.retains):
        request_id = core.RequestId(root=f"retain-{index}")
        request = core.SnapshotAndRetain(
            request_id=request_id,
            scope=core.Scope(owner=state.run.run_id, generation=scope),
            attempt=core.AttemptRef(
                attempt_id=core.AttemptId(root=f"attempt-r{index}"), generation=scope
            ),
            retention="wip",
        )
        made.append(
            core.RequestRecord(
                request_id=request_id,
                request=request,
                payload_digest=value_digest(request),
                lifecycle=core.LifecycleClass.IDEMPOTENT_WRITE,
                phase=core.RequestPhase.PREPARED,
                until=core.Until(at=100.0),
                addressed_by=(),
            )
        )
    return tuple(made)


RETAINING = World((StepSpec(2.0),), retries=0, retains=(0,))
"""A query step and a snapshot-and-retain, which a drain lets through."""
CLOSING = World((StepSpec(2.0),), retries=0, closes=(1,))
"""A query step and the close of a scope with no other step, which a drain lets through."""
CLOSING_ITS_STEP = World((StepSpec(2.0, scope=1),), closes=(0,))
"""A query step and the close of another scope than its own."""
IN_FLIGHT = World((StepSpec(2.0, standing=Standing.RECONCILING),), retries=0)
"""A step an earlier version left reconciling: the log gives it an ended first attempt."""
DISPATCHED = World((StepSpec(2.0, standing=Standing.DISPATCHED),), retries=0)
"""A step an earlier version left dispatched."""
ONE_QUERY = World((StepSpec(2.0),), retries=0)


def query_world(limits: tuple[float, ...], retries: int) -> World:
    """One query step per limit: the world the first recovery properties ran in."""
    return World(tuple(StepSpec(limit) for limit in limits), retries)


def seeded(
    limits: tuple[float, ...], retries: int
) -> tuple[core.CoreState, core.OperationRegistry]:
    return build(query_world(limits, retries))


LIMITS = st.sampled_from([2.0, 10.0, 100.0])


def worlds() -> st.SearchStrategy[World]:
    """Worlds of every kind of step, in two scopes, as a fresh run or a restart finds them."""
    spec = st.builds(
        StepSpec,
        limit=LIMITS,
        kind=st.sampled_from(list(Kind)),
        scope=st.integers(0, 1),
        standing=st.sampled_from(list(Standing)),
    )
    return st.builds(
        World,
        steps=st.lists(spec, min_size=1, max_size=3).map(tuple),
        retries=st.integers(0, 3),
        host_epoch=st.sampled_from([0, 0, 3]),
        aux=st.lists(st.sampled_from(list(Aux)), max_size=2).map(tuple),
        closes=st.lists(st.integers(0, 1), max_size=1).map(tuple),
        retains=st.lists(st.integers(0, 1), max_size=1).map(tuple),
    )


class Staging(StrEnum):
    """What a staged world always holds: an effect a drain lets through, or a step in flight."""

    RETAIN = "retain"
    CLOSE = "close"
    IN_FLIGHT = "in_flight"


def staged_worlds(*stagings: Staging) -> st.SearchStrategy[World]:
    """Worlds that always hold what a pause, a kill or a restart treats differently.

    Each holds one of ``stagings`` (every kind unless some are named).

    A drain lets a snapshot-and-retain and the close of a scope through, and an earlier version
    leaves a step in flight with no attempt in this log: the places the end of an epoch meets a
    pause, a park or an attempt that is still live.
    """

    def staged(world: World, staging: Staging, scope: int, standing: Standing) -> World:
        match staging:
            case Staging.RETAIN:
                return replace(world, retains=(*world.retains, scope))
            case Staging.CLOSE:
                return replace(world, closes=(*world.closes, scope))
            case Staging.IN_FLIGHT:
                first, *rest = world.steps
                return replace(world, steps=(replace(first, standing=standing), *rest))

    return st.builds(
        staged,
        worlds(),
        st.sampled_from(stagings or tuple(Staging)),
        st.integers(0, 1),
        st.sampled_from([Standing.DISPATCHED, Standing.RECONCILING]),
    )
