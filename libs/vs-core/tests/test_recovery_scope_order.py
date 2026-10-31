"""A snapshot never reads a half-written tree: `decide` orders the steps of one scope by effect.

A snapshot step runs only when every writer of its scope has a result, an interrupted writer
included (it has no result until it runs again and ends), and no writer starts while a snapshot of
its scope runs. States are generated per step (kind, scope, how its attempts stand), so every
interleaving of writers, snapshots and readers across scopes is covered, after a pause, a raise
or a crash alike.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api.recovery import (
    Run,
    StepAttempts,
    StepEffect,
    StepsState,
    decide,
    request_effect,
)
from vs_sim.api.testing import examples

from .test_owner_event_precedence import dispatched


class Kind(StrEnum):
    WRITER = "project.write"
    SNAPSHOT = "project.snapshot"
    READER = "project.read"


class Stand(StrEnum):
    """How the log leaves a step."""

    NEW = "new"
    RUNNING = "running"
    PAUSED = "paused"
    """Its attempt was interrupted by a pause: open, not charged."""
    CRASHED = "crashed"
    """Its attempt ended with its process: open, charged."""
    DONE = "done"


def descriptor(kind: Kind) -> core.OperationDescriptor:
    return core.OperationDescriptor(
        kind=kind.value,
        lifecycle=core.LifecycleClass.QUERY
        if kind is Kind.READER
        else core.LifecycleClass.IDEMPOTENT_WRITE,
        request_schema=core.SchemaRef(name=f"{kind.value}-request", version=1),
        outcome_schema=core.SchemaRef(name=f"{kind.value}-outcome", version=1),
        revision_authority=(
            core.RevisionAuthority.SNAPSHOT
            if kind is Kind.SNAPSHOT
            else core.RevisionAuthority.NONE
        ),
    )


DESCRIPTORS = tuple(descriptor(kind) for kind in Kind)


def seed() -> core.CoreState:
    state = core.initial_state()
    run = state.run.model_copy(update={"capabilities": core.Capabilities(operations=DESCRIPTORS)})
    return state.model_copy(update={"registry": DESCRIPTORS, "run": run})


SEED = seed()


def scope_of(index: int) -> core.Scope:
    return core.Scope(owner=SEED.run.run_id, generation=index)


def request_of(
    name: str, kind: Kind, scope: int, depends_on: tuple[core.RequestId, ...] = ()
) -> core.ExecuteRegisteredOperation:
    return core.ExecuteRegisteredOperation(
        request_id=core.RequestId(root=name),
        scope=scope_of(scope),
        depends_on=depends_on,
        operation_id=core.OperationId(root=f"operation-{name}"),
        operation=core.OperationWire(
            schema_ref=core.OperationSchemaRef(
                kind=kind.value,
                request_schema=core.SchemaRef(name=f"{kind.value}-request", version=1),
                outcome_schema=core.SchemaRef(name=f"{kind.value}-outcome", version=1),
                lifecycle=descriptor(kind).lifecycle,
            ),
            payload_json="{}",
        ),
        retry_limit=5,
    )


@dataclass(frozen=True)
class Spec:
    kind: Kind
    scope: int
    stand: Stand

    @property
    def open(self) -> bool:
        return self.stand is not Stand.DONE

    @property
    def running(self) -> bool:
        return self.stand is Stand.RUNNING


def state_of(
    specs: list[Spec], depends: dict[int, tuple[int, ...]] | None = None
) -> tuple[core.CoreState, list[core.ExecuteRegisteredOperation]]:
    depends = depends or {}
    requests = [
        request_of(
            f"step-{index}",
            spec.kind,
            spec.scope,
            tuple(core.RequestId(root=f"step-{other}") for other in depends.get(index, ())),
        )
        for index, spec in enumerate(specs)
    ]
    records = []
    rows = []
    for request, spec in zip(requests, specs, strict=True):
        assert request.request_id is not None
        started = spec.stand is not Stand.NEW
        phase = core.RequestPhase.PREPARED
        if spec.stand is Stand.DONE:
            phase = core.RequestPhase.COMPLETED
        elif started:
            phase = core.RequestPhase.DISPATCHED
        records.append(
            core.RequestRecord(
                request_id=request.request_id,
                request=request,
                payload_digest=request.request_id.root,
                lifecycle=descriptor(spec.kind).lifecycle,
                phase=phase,
                until=core.Until(at=100.0),
                addressed_by=(),
            )
        )
        if started and spec.stand is not Stand.DONE:
            rows.append(
                StepAttempts(
                    tag=request.request_id,
                    started=1,
                    ended=spec.stand in (Stand.PAUSED, Stand.CRASHED),
                    uncharged=1 if spec.stand is Stand.PAUSED else 0,
                )
            )
    state = SEED.model_copy(
        update={
            "records": SEED.records.model_copy(update={"records": tuple(records)}),
            "steps": StepsState(epoch=1, attempts=tuple(rows)),
        }
    )
    return state, requests


SPECS = st.lists(
    st.builds(
        Spec,
        kind=st.sampled_from(Kind),
        scope=st.integers(0, 1),
        stand=st.sampled_from(Stand),
    ),
    min_size=1,
    max_size=7,
)


def runs(state: core.CoreState) -> set[core.RequestId]:
    return {action.tag for action in decide(state, 1.0) if isinstance(action, Run)}


def open_in_scope(specs: list[Spec], index: int, kind: Kind) -> list[int]:
    return [
        other
        for other, spec in enumerate(specs)
        if other != index and spec.scope == specs[index].scope and spec.kind is kind and spec.open
    ]


@settings(max_examples=examples(pr=40, nightly=400))
@given(specs=SPECS)
def test_a_snapshot_runs_only_when_every_writer_of_its_scope_has_a_result(
    specs: list[Spec],
) -> None:
    state, requests = state_of(specs)
    started = runs(state)
    for index, (spec, request) in enumerate(zip(specs, requests, strict=True)):
        if spec.kind is Kind.SNAPSHOT and request.request_id in started:
            assert not open_in_scope(specs, index, Kind.WRITER), (index, specs)


@settings(max_examples=examples(pr=40, nightly=400))
@given(specs=SPECS)
def test_no_writer_starts_while_a_snapshot_of_its_scope_runs(specs: list[Spec]) -> None:
    state, requests = state_of(specs)
    started = runs(state)
    for index, (spec, request) in enumerate(zip(specs, requests, strict=True)):
        if spec.kind is Kind.WRITER and request.request_id in started:
            running = [o for o in open_in_scope(specs, index, Kind.SNAPSHOT) if specs[o].running]
            assert not running, (index, specs)


@settings(max_examples=examples(pr=40, nightly=400))
@given(specs=SPECS)
def test_a_step_waits_for_nothing_but_the_steps_of_its_own_scope(specs: list[Spec]) -> None:
    """Nothing is held back without a reason: the rule is liveness as well as safety."""
    state, requests = state_of(specs)
    started = runs(state)
    for index, (spec, request) in enumerate(zip(specs, requests, strict=True)):
        if not spec.open or spec.running:
            continue
        blocked = (
            bool(open_in_scope(specs, index, Kind.WRITER))
            if spec.kind is Kind.SNAPSHOT
            else any(specs[o].running for o in open_in_scope(specs, index, Kind.SNAPSHOT))
            if spec.kind is Kind.WRITER
            else False
        )
        assert (request.request_id in started) is (not blocked), (index, specs)


@settings(max_examples=examples(pr=40, nightly=400))
@given(specs=SPECS)
def test_an_interrupted_writer_still_holds_its_scope_back_until_it_has_a_result(
    specs: list[Spec],
) -> None:
    """A pause or a crash leaves the writer's files half written; only its result clears it."""
    interrupted = [Spec(Kind.WRITER, 0, stand) for stand in (Stand.PAUSED, Stand.CRASHED)]
    state, requests = state_of([Spec(Kind.SNAPSHOT, 0, Stand.NEW), *interrupted, *specs[:2]])
    assert requests[0].request_id not in runs(state)
    finished = [Spec(Kind.SNAPSHOT, 0, Stand.NEW), *[Spec(Kind.WRITER, 0, Stand.DONE)] * 2]
    state, requests = state_of(finished)
    assert requests[0].request_id in runs(state)


def test_a_writer_that_waits_for_the_snapshot_does_not_hold_it_back() -> None:
    """The writer's own dependency decides: it cannot end before the snapshot, so it must not wait."""
    specs = [Spec(Kind.SNAPSHOT, 0, Stand.NEW), Spec(Kind.WRITER, 0, Stand.NEW)]
    state, requests = state_of(specs, depends={1: (0,)})
    assert requests[0].request_id in runs(state)
    assert requests[1].request_id not in runs(state)
    through = state_of(
        [
            Spec(Kind.SNAPSHOT, 0, Stand.NEW),
            Spec(Kind.READER, 0, Stand.NEW),
            Spec(Kind.WRITER, 0, Stand.NEW),
        ],
        depends={2: (1,), 1: (0,)},
    )[0]
    assert core.RequestId(root="step-0") in runs(through)


@pytest.mark.parametrize("access", list(core.Access))
def test_a_turn_writes_the_files_of_its_scope_unless_it_is_read_only(access: core.Access) -> None:
    _, request = dispatched()
    session = request.turn.session.model_copy(update={"access": access})
    turn = request.turn.model_copy(update={"session": session})
    for turn_request in (
        request.model_copy(update={"turn": turn}),
        core.ResumeSessionTurn(
            **request.model_copy(update={"turn": turn}).model_dump(exclude={"kind"}),
            continuation_id=core.ContinuationId(root="continuation"),
        ),
    ):
        expected = StepEffect.NEITHER if access is core.Access.READ_ONLY else StepEffect.WRITES
        assert request_effect(turn_request, ()) is expected


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (Kind.WRITER, StepEffect.WRITES),
        (Kind.SNAPSHOT, StepEffect.SNAPSHOTS),
        (Kind.READER, StepEffect.NEITHER),
    ],
)
def test_a_registered_operation_is_judged_by_its_descriptor(
    kind: Kind, expected: StepEffect
) -> None:
    assert request_effect(request_of("step", kind, 0), DESCRIPTORS) is expected


def test_an_operation_nobody_declared_is_a_writer() -> None:
    assert request_effect(request_of("step", Kind.READER, 0), ()) is StepEffect.WRITES
