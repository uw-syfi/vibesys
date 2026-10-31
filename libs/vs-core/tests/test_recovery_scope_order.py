"""A snapshot never reads a half-written tree: `decide` orders the steps of one scope by effect.

A snapshot step runs only when every writer of its scope has a result, an interrupted writer
included (it has no result until it runs again and ends), and no writer starts while a snapshot of
its scope runs. States are generated per step (kind, scope, how its attempts stand), so every
interleaving of writers, snapshots and readers across scopes is covered, after a pause, a raise
or a crash alike.

A scope that is closing (its ``CloseAttemptScope`` is open) withdraws the work its kind declares
withdrawable and waits for the rest: the close never waits on a measurement nobody will read, and
never cancels the cleanup it depends on.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api.recovery import (
    Cancel,
    Cancelled,
    CancelReason,
    ClosePolicy,
    Run,
    Settle,
    StepAttempts,
    StepEffect,
    StepsState,
    decide,
    request_close_policy,
    request_effect,
)
from vs_sim.api.testing import examples

from .proof_digest import value_digest
from .test_owner_event_precedence import dispatched


class Kind(StrEnum):
    WRITER = "project.write"
    SNAPSHOT = "project.snapshot"
    READER = "project.read"
    EPHEMERAL = "project.ephemeral"
    """A registered operation whose descriptor declares that a closing scope withdraws it."""
    MEASURE = "measure"
    """``SubmitMeasurement``: its kind declares that a closing scope withdraws it."""
    CLEANUP = "cleanup"
    """``DiscardWorkspace``: cleanup the close depends on, so the close waits for it."""
    CLOSE = "close"
    """``CloseAttemptScope``: closes the scope."""


REGISTERED = (Kind.WRITER, Kind.SNAPSHOT, Kind.READER, Kind.EPHEMERAL)
ORDERED = (Kind.WRITER, Kind.SNAPSHOT, Kind.READER)
"""The kinds the ordering of writers, snapshots and readers is stated over."""
WITHDRAWABLE = (Kind.EPHEMERAL, Kind.MEASURE)


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
        on_close=ClosePolicy.WITHDRAW if kind is Kind.EPHEMERAL else ClosePolicy.WAIT,
    )


DESCRIPTORS = tuple(descriptor(kind) for kind in REGISTERED)


def seed() -> core.CoreState:
    state = core.initial_state()
    run = state.run.model_copy(update={"capabilities": core.Capabilities(operations=DESCRIPTORS)})
    return state.model_copy(update={"registry": DESCRIPTORS, "run": run})


SEED = seed()


def scope_of(index: int) -> core.Scope:
    return core.Scope(owner=SEED.run.run_id, generation=index)


def request_of(
    name: str, kind: Kind, scope: int, depends_on: tuple[core.RequestId, ...] = ()
) -> core.Request:
    request_id = core.RequestId(root=name)
    attempt = core.AttemptRef(attempt_id=core.AttemptId(root="attempt"), generation=scope)
    match kind:
        case Kind.MEASURE:
            return core.SubmitMeasurement(
                request_id=request_id,
                scope=scope_of(scope),
                depends_on=depends_on,
                plan=core.MeasurementPlan(
                    purpose="official",
                    candidate=core.RevisionRef(revision_id=core.RevisionId(root=name), digest=name),
                    evaluator_digest="eval",
                    workload_digest="work",
                    environment_digest="env",
                    stages=(),
                    policy="ordered",
                    recipe=core.ArtifactRef(artifact_id=core.ArtifactId(root="recipe"), digest="r"),
                    submitted_at=0.0,
                    queue_allowance=1.0,
                    accuracy_gated=(),
                ),
            )
        case Kind.CLEANUP:
            return core.DiscardWorkspace(
                request_id=request_id,
                scope=scope_of(scope),
                depends_on=depends_on,
                attempt=attempt,
            )
        case Kind.CLOSE:
            return core.CloseAttemptScope(
                request_id=request_id,
                scope=scope_of(scope),
                depends_on=depends_on,
                attempt=attempt,
            )
    return registered_request_of(name, kind, scope, depends_on)


def registered_request_of(
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


def lifecycle_of(kind: Kind) -> core.LifecycleClass:
    match kind:
        case Kind.MEASURE:
            return core.LifecycleClass.OWNED_JOB
        case Kind.CLEANUP | Kind.CLOSE:
            return core.LifecycleClass.IDEMPOTENT_WRITE
    return descriptor(kind).lifecycle


def succeeded(request: core.Request) -> core.Observation:
    """The conclusive, released observation a step that has a result holds."""
    assert request.request_id is not None
    return core.Observation(
        event_id=core.EventId(root=f"{request.request_id.root}:observation:0"),
        request_id=request.request_id,
        scope=request.scope,
        admission_id=request.admission_id,
        sequence=0,
        observed_at=0.0,
        status=core.ObservationStatus.SUCCEEDED,
        accepted=True,
        terminal=True,
        released=True,
        children_complete=True,
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
) -> tuple[core.CoreState, list[core.Request]]:
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
                payload_digest=value_digest(request),
                lifecycle=lifecycle_of(spec.kind),
                phase=phase,
                until=core.Until(at=100.0),
                addressed_by=(),
                observation=succeeded(request) if spec.stand is Stand.DONE else None,
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


def has_a_state(spec: Spec) -> bool:
    """Whether the states built here can show the step so.

    A finished job step holds its scope until the release of its job is proven, from the job's
    row in the evaluation state, which these states do not build. A step that is done is covered
    by the registered operation that is also withdrawn by its scope.
    """
    return not (spec.kind is Kind.MEASURE and spec.stand is Stand.DONE)


def specs_of(kinds: tuple[Kind, ...]) -> st.SearchStrategy[list[Spec]]:
    return st.lists(
        st.builds(
            Spec,
            kind=st.sampled_from(kinds),
            scope=st.integers(0, 1),
            stand=st.sampled_from(Stand),
        ).filter(has_a_state),
        min_size=1,
        max_size=7,
    )


SPECS = specs_of(ORDERED)
CLOSING_SPECS = specs_of((*REGISTERED, Kind.MEASURE, Kind.CLEANUP, Kind.CLOSE))


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


# A scope is closing while its ``CloseAttemptScope`` is open. The step kinds declare what that
# means for the rest of the scope: the close withdraws work only the scope would read (a
# measurement, an operation that says so) and waits for everything else.


def closing(specs: list[Spec], index: int) -> bool:
    """Whether the scope of step ``index`` has an open close beside it."""
    return any(
        other != index
        and spec.scope == specs[index].scope
        and spec.kind is Kind.CLOSE
        and spec.open
        for other, spec in enumerate(specs)
    )


def actions_of(state: core.CoreState) -> dict[core.RequestId, core.Action]:
    return {
        action.tag: action
        for action in decide(state, 1.0)
        if isinstance(action, Cancel | Settle | Run)
    }


def is_withdrawal(action: core.Action | None) -> bool:
    match action:
        case Cancel(reason=CancelReason.WITHDRAWN):
            return True
        case Settle(result=Cancelled(reason=CancelReason.WITHDRAWN)):
            return True
    return False


@pytest.mark.parametrize("kind", [*Kind])
def test_every_step_kind_declares_what_a_closing_scope_does_with_it(kind: Kind) -> None:
    expected = ClosePolicy.WITHDRAW if kind in WITHDRAWABLE else ClosePolicy.WAIT
    assert request_close_policy(request_of("step", kind, 0), DESCRIPTORS) is expected


def test_an_operation_nobody_declared_is_waited_for() -> None:
    request = request_of("step", Kind.EPHEMERAL, 0)
    assert request_close_policy(request, ()) is ClosePolicy.WAIT


@settings(max_examples=examples(pr=60, nightly=600))
@given(specs=CLOSING_SPECS)
def test_a_closing_scope_never_waits_on_a_step_it_may_withdraw(specs: list[Spec]) -> None:
    """Each open withdrawable step of a closing scope is ended by this very decision."""
    state, requests = state_of(specs)
    actions = actions_of(state)
    for index, (spec, request) in enumerate(zip(specs, requests, strict=True)):
        if spec.kind not in WITHDRAWABLE or not spec.open or not closing(specs, index):
            continue
        action = actions.get(request.request_id)
        assert is_withdrawal(action), (index, specs, action)
        assert isinstance(action, Cancel) is spec.running, (index, specs, action)


@settings(max_examples=examples(pr=60, nightly=600))
@given(specs=CLOSING_SPECS)
def test_a_closing_scope_never_withdraws_a_step_it_must_wait_for(specs: list[Spec]) -> None:
    """Only a declared step is withdrawn, and only by a scope that is closing."""
    state, requests = state_of(specs)
    actions = actions_of(state)
    for index, (spec, request) in enumerate(zip(specs, requests, strict=True)):
        if not is_withdrawal(actions.get(request.request_id)):
            continue
        assert spec.kind in WITHDRAWABLE, (index, specs)
        assert spec.open, (index, specs)
        assert closing(specs, index), (index, specs)


@settings(max_examples=examples(pr=60, nightly=600))
@given(specs=CLOSING_SPECS)
def test_a_close_runs_only_when_every_other_step_of_its_scope_has_a_result(
    specs: list[Spec],
) -> None:
    """The close is held back while any step of its scope is open, withdrawn ones included."""
    state, requests = state_of(specs)
    started = runs(state)
    for index, (spec, request) in enumerate(zip(specs, requests, strict=True)):
        if spec.kind is not Kind.CLOSE or not spec.open or spec.running:
            continue
        siblings = [
            other
            for other, row in enumerate(specs)
            if other != index and row.scope == spec.scope and row.open
        ]
        assert (request.request_id in started) is (not siblings), (index, specs)


WITHDRAWN_ONLY = st.lists(
    st.builds(
        Spec,
        kind=st.sampled_from(WITHDRAWABLE),
        scope=st.just(0),
        stand=st.sampled_from(Stand),
    ).filter(has_a_state),
    max_size=5,
)


@settings(max_examples=examples(pr=60, nightly=600))
@given(withdrawable=WITHDRAWN_ONLY)
def test_a_scope_holding_only_withdrawable_work_closes_once_it_is_withdrawn(
    withdrawable: list[Spec],
) -> None:
    """Liveness: the close waits for the withdrawals to be recorded, and then it runs."""
    specs = [Spec(Kind.CLOSE, 0, Stand.NEW), *withdrawable]
    state, requests = state_of(specs)
    actions = actions_of(state)
    assert requests[0].request_id in runs(state) or any(row.open for row in withdrawable)
    ended = [
        replace(spec, stand=Stand.DONE) if is_withdrawal(actions.get(request.request_id)) else spec
        for spec, request in zip(specs, requests, strict=True)
    ]
    after, later = state_of(ended)
    assert all(not row.open for row in ended[1:])
    assert later[0].request_id in runs(after)


def test_a_step_that_waits_for_the_close_is_not_withdrawn_by_it() -> None:
    """A withdrawable step that follows the close cannot end before it: it is the close's to run."""
    specs = [Spec(Kind.CLOSE, 0, Stand.NEW), Spec(Kind.MEASURE, 0, Stand.NEW)]
    state, requests = state_of(specs, depends={1: (0,)})
    assert not is_withdrawal(actions_of(state).get(requests[1].request_id))
    assert requests[0].request_id in runs(state)
