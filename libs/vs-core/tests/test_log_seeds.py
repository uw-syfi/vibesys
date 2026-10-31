"""What a log starts from, and what a cache of its fold stands in for.

A log starts from `RunStarted` (a new run) or `CoreSnapshot` (a run an older version saved, appended
once by a migration). The snapshot is a seed the way the worlds of `recovery_worlds.py` are: steps of
every kind, in the standing a log of an earlier version leaves them, under an epoch the host counted
that the log's own numbering may be behind. Folding it must never refuse such a seed, and `decide`
must still ask for every open step.

The cache of a fold is `Folded.encode`: the fold of the first records of a log, which a start reads
and then folds the rest of the log over. Whatever the log and wherever it is cut, that is the fold of
the whole log.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api.recovery import (
    Folded,
    Run,
    SavedRun,
    at_rest,
    decide,
    decode_snapshot,
    fold_run,
    input_message,
    seed_message,
    snapshot_message,
)
from vs_proto.api import EpochStarted, Oneof, Record, Timestamp
from vs_sim.api.testing import examples

from .recovery_loop import MOVES, OPEN, Loop, Move, invariants, play, settle
from .recovery_worlds import Standing, StepSpec, World, build, worlds


def record(seq: int, epoch: int, body: Oneof, clock: float = 0.0) -> Record:
    return Record(seq=seq, epoch=epoch, at=Timestamp(seconds=seq), clock=clock, body=body)


def header(epoch: int) -> Record:
    return record(1, epoch, Oneof("epoch_started", EpochStarted(epoch=epoch)))


def saved_run(state: core.CoreState, *inputs: core.CoreEvent) -> SavedRun:
    return SavedRun(
        state=state, envelope_version=core.ENVELOPE_SCHEMA_VERSION, strategy=b"{}", inputs=inputs
    )


def snapshot_loop(world: World, epoch: int) -> Loop:
    """A run resumed from a snapshot of ``world``, in the log's epoch ``epoch``."""
    state, registry = build(world)
    snapshot = record(2, epoch, Oneof("core_snapshot", snapshot_message(saved_run(state))))
    records = [header(epoch), snapshot]
    folded = fold_run(records, registry=registry)
    return Loop(folded.state, registry, records=records, epoch=epoch, clock=snapshot.clock)


EPOCHS = st.integers(1, 4)
"""The epoch of the log the snapshot lands in; the host counted its own, which may be ahead."""


def test_a_snapshot_round_trips_what_it_holds() -> None:
    state, registry = build(World(()))
    pause = core.RunControlEvent(
        control=core.ControlInput(
            control_id=core.ControlId(root="c"),
            action="pause",
            owner=core.PauseOwner.OPERATOR,
        ),
        now_at=0.0,
    )
    saved = SavedRun(
        state=state,
        envelope_version=core.ENVELOPE_SCHEMA_VERSION,
        strategy=b'{"search":"state"}',
        inputs=(pause,),
        wakes=(30.0, 40.0),
    )
    assert decode_snapshot(snapshot_message(saved), registry) == saved


@settings(max_examples=examples(pr=10, nightly=200), deadline=None)
@given(world=worlds(), epoch=EPOCHS)
def test_a_snapshot_folds_as_the_start_of_a_new_run_does(world: World, epoch: int) -> None:
    state, registry = build(world)
    started = record(2, epoch, Oneof("run_started", seed_message(state)))
    snapshot = record(2, epoch, Oneof("core_snapshot", snapshot_message(saved_run(state))))
    assert fold_run([header(epoch), snapshot], registry=registry) == fold_run(
        [header(epoch), started], registry=registry
    )


@settings(max_examples=examples(pr=10, nightly=200), deadline=None)
@given(world=worlds(), epoch=EPOCHS)
def test_the_inputs_of_a_snapshot_are_applied_in_order_as_input_records_are(
    world: World, epoch: int
) -> None:
    """A saved run's unapplied inputs fold as the same inputs appended one by one after the seed."""
    state, registry = build(world)
    pause = core.RunControlEvent(
        control=core.ControlInput(
            control_id=core.ControlId(root="c"),
            action="pause",
            owner=core.PauseOwner.OPERATOR,
        ),
        now_at=0.0,
    )
    inputs = (pause, core.ClockAdvanced(now_at=2.0))
    snapshot = record(2, epoch, Oneof("core_snapshot", snapshot_message(saved_run(state, *inputs))))
    separately = [
        record(2, epoch, Oneof("run_started", seed_message(state))),
        *(
            record(3 + index, epoch, Oneof("core_input", input_message(event)))
            for index, event in enumerate(inputs)
        ),
    ]
    assert fold_run([header(epoch), snapshot], registry=registry) == fold_run(
        [header(epoch), *separately], registry=registry
    )


@settings(max_examples=examples(pr=10, nightly=300), deadline=None)
@given(world=worlds(), epoch=EPOCHS, moves=st.lists(MOVES, max_size=12), lag=st.booleans())
def test_a_run_resumed_from_a_snapshot_decides_without_refusal(
    world: World, epoch: int, moves: list[Move], *, lag: bool
) -> None:
    """Every action `decide` returns for a saved run is recordable, and the run moves to rest."""
    loop = snapshot_loop(world, epoch)
    loop.open_epoch()
    for move in moves:
        play(loop, move)
        invariants(loop)
        if not lag or move[0] == "tick":
            loop.tick()
            invariants(loop)


@settings(max_examples=examples(pr=10, nightly=200), deadline=None)
@given(world=worlds(), epoch=EPOCHS)
def test_decide_runs_every_open_step_of_a_saved_run_to_a_result(world: World, epoch: int) -> None:
    """Whatever the standing of a saved run's steps, runners that cooperate leave none open."""
    loop = snapshot_loop(world, epoch)
    loop.open_epoch()
    settle(loop)
    rest = at_rest(loop.state)
    paused = loop.state.run.status is core.RunStatus.PAUSED
    open_steps = [row for row in loop.state.records.records if row.phase in OPEN]
    assert rest is not None or paused or not open_steps, "idle with a step open and nothing running"


@pytest.mark.xfail(
    strict=True, reason="seams: Run has no clear_first fact yet, and a step in flight needs one"
)
@pytest.mark.parametrize("standing", [Standing.DISPATCHED, Standing.RECONCILING])
@pytest.mark.parametrize("epoch", [1, 3])
def test_a_step_in_flight_in_a_saved_run_is_cleared_before_it_runs_again(
    standing: Standing, epoch: int
) -> None:
    """The saved run's process may have started the step, so its first attempt here clears first."""
    world = World((StepSpec(100.0, standing=standing),), host_epoch=3)
    loop = snapshot_loop(world, epoch)
    loop.open_epoch()
    (run,) = [a for a in decide(loop.state, loop.clock) if isinstance(a, Run)]
    assert vars(run)["clear_first"]


def _log_of(world: World, moves: list[Move], *, snapshot: bool, epoch: int) -> tuple[Loop, int]:
    """The records a trajectory writes, and how many of them precede the loop's own."""
    if snapshot:
        loop = snapshot_loop(world, epoch)
    else:
        state, registry = build(world)
        loop = Loop(state, registry)
    loop.open_epoch()
    for move in moves:
        play(loop, move)
        loop.tick()
    return loop, 2 if snapshot else 0


@settings(max_examples=examples(pr=10, nightly=300), deadline=None)
@given(
    world=worlds(),
    moves=st.lists(MOVES, max_size=14),
    snapshot=st.booleans(),
    epoch=EPOCHS,
    data=st.data(),
)
def test_a_fold_started_from_its_cache_is_the_fold_of_the_whole_log(
    world: World, moves: list[Move], *, snapshot: bool, epoch: int, data: st.DataObject
) -> None:
    loop, _ = _log_of(world, moves, snapshot=snapshot, epoch=epoch)
    seed = None if snapshot else build(world)[0]
    records = loop.records
    cut = data.draw(st.integers(0, len(records)), label="records the cache covers")
    whole = fold_run(records, seed, registry=loop.registry)
    cached = fold_run(records[:cut], seed, registry=loop.registry)
    restored = Folded.decode(cached.encode(), loop.registry)
    assert restored == cached
    assert fold_run(records[cut:], restored, registry=loop.registry) == whole


def test_a_cache_written_by_another_encoding_or_damaged_is_refused() -> None:
    state, registry = build(World(()))
    encoded = Folded(state).encode()
    with pytest.raises(ValueError, match="encoding version 2"):
        Folded.decode(encoded.replace(b'"schema_version":1', b'"schema_version":2', 1), registry)
    with pytest.raises(ValueError, match=r"does not|invalid|JSON"):
        Folded.decode(encoded[: len(encoded) // 2], registry)
