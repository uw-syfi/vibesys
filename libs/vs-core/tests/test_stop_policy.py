"""One table says what a stop does with each kind of step (#3475).

A Pause-now cancels the work a turn did not finish and lets through what frees what the run holds
and what a turn already did; a run ending by a cancel lets through only what frees what the run
holds. Both answers come from ``request_stop_policy``, one exhaustive match, so the two stops
cannot disagree about a kind: a step that frees what the run holds is never cancelled by either.
"""

from __future__ import annotations

from dataclasses import replace
from types import UnionType
from typing import Annotated, Literal, TypeAliasType, Union, get_args, get_origin

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import BaseModel

import vs_core.api as core
from vs_core._run_park import dispatch_allowed, pause_cancels
from vs_core.api.recovery import StopPolicy, request_stop_policy
from vs_core.types import records
from vs_sim.api.testing import examples

from .recovery_worlds import Kind, Standing, StepSpec, World, build, worlds


def pause_now(state: core.CoreState) -> core.CoreState:
    paused = core.step(
        state,
        core.RunControlEvent(
            control=core.ControlInput(control_id=core.ControlId(root="pause"), action="pause_now"),
            now_at=state.run.now_at,
        ),
    ).state
    assert paused.run.status is core.RunStatus.DRAINING
    return paused


def discarding(state: core.CoreState, kind: Kind) -> core.CoreState:
    """``state`` with the operation ``kind`` declaring that it discards a workspace."""
    registry = tuple(
        row.model_copy(update={"revision_authority": core.RevisionAuthority.DISCARD})
        if row.kind == kind.value
        else row
        for row in state.registry
    )
    return state.model_copy(update={"registry": registry})


def test_a_pause_now_lets_an_operation_that_discards_a_workspace_run_on() -> None:
    """A cancel of the discard would leave the workspace held, as an ending's would."""
    state, _ = build(
        World(steps=(StepSpec(limit=10.0, kind=Kind.WRITE, standing=Standing.DISPATCHED),))
    )
    paused = pause_now(discarding(state, Kind.WRITE))
    [row] = paused.records.records
    assert not pause_cancels(paused, row.request)
    assert dispatch_allowed(paused, row.request)


@pytest.mark.parametrize(
    "authority", [core.RevisionAuthority.NONE, core.RevisionAuthority.SNAPSHOT]
)
def test_a_pause_now_cancels_an_operation_that_discards_nothing(
    authority: core.RevisionAuthority,
) -> None:
    state, _ = build(
        World(steps=(StepSpec(limit=10.0, kind=Kind.WRITE, standing=Standing.DISPATCHED),))
    )
    registry = tuple(
        row.model_copy(update={"revision_authority": authority}) for row in state.registry
    )
    paused = pause_now(state.model_copy(update={"registry": registry}))
    [row] = paused.records.records
    assert pause_cancels(paused, row.request)


@settings(max_examples=examples(pr=20, nightly=300), deadline=None)
@given(
    world=worlds().map(
        lambda world: World(
            steps=tuple(
                replace(step, standing=Standing.DISPATCHED)
                for step in world.steps
                if step.kind is not Kind.JOB
            )
            or (StepSpec(limit=10.0, standing=Standing.DISPATCHED),),
            aux=world.aux,
            closes=world.closes,
            retains=world.retains,
        )
    ),
    discards=st.sampled_from([None, Kind.QUERY, Kind.WRITE, Kind.SNAPSHOT]),
)
def test_a_pause_now_cancels_exactly_the_steps_a_stop_policy_calls_work(
    world: World, discards: Literal[Kind.QUERY, Kind.WRITE, Kind.SNAPSHOT] | None
) -> None:
    state, _ = build(world)
    if discards is not None:
        state = discarding(state, discards)
    paused = pause_now(state)
    for row in paused.records.records:
        policy = request_stop_policy(row.request, paused.registry)
        assert pause_cancels(paused, row.request) is (policy is StopPolicy.WORK), (
            row.request.kind,
            policy,
        )


RELEASES = {"CloseSession", "CancelTurn", "DiscardWorkspace", "CloseAttemptScope", "CancelOwnedJob"}
KEEPS = {
    "SnapshotAndRetain",
    "SnapshotAndRetainRun",
    "RetainRevision",
    "CollectEvidence",
    "BlockRequest",
}


def request_classes() -> list[type[BaseModel]]:
    """Every class of the ``Request`` union, flattened from its alias."""

    def flatten(tp: object) -> list[type[BaseModel]]:
        if isinstance(tp, TypeAliasType):
            return flatten(tp.__value__)
        if get_origin(tp) is Annotated:
            return flatten(get_args(tp)[0])
        if get_origin(tp) in (Union, UnionType):
            return [cls for arg in get_args(tp) for cls in flatten(arg)]
        assert isinstance(tp, type) and issubclass(tp, BaseModel), tp
        return [tp]

    return flatten(records.Request.__value__)


def test_every_kind_of_step_has_the_stop_policy_the_design_gives_it() -> None:
    """The table, written out: a stop frees what the run holds, keeps what a turn did, drops the rest."""
    classes = request_classes()
    assert len(classes) >= 20, "the union was not flattened"
    assert {cls.__name__ for cls in classes} >= RELEASES | KEEPS
    for cls in classes:
        if cls is core.ExecuteRegisteredOperation:
            continue
        expected = (
            StopPolicy.RELEASE
            if cls.__name__ in RELEASES
            else StopPolicy.KEEP
            if cls.__name__ in KEEPS
            else StopPolicy.WORK
        )
        assert request_stop_policy(cls.model_construct(), ()) is expected, cls.__name__
