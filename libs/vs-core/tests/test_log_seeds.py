"""What a log starts from, and what a cache of its fold stands in for.

A log starts from `RunStarted` (a new run) or a snapshot (a run an older version saved, appended once
by a migration). The log's reader decodes the snapshot record into a `SavedRun`, which core folds as
`apply_saved` (the record's wire form is `vs_runtime`'s, and tested there). A saved run is a seed the
way the worlds of `recovery_worlds.py` are: steps of every kind, in the standing a log of an earlier
version leaves them, under an epoch the host counted that the log's own numbering may be behind.
Folding it must never refuse such a seed, and `decide` must still ask for every open step.

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
    apply_record,
    apply_saved,
    at_rest,
    decide,
    decode_saved,
    encode_saved,
    fold_run,
    input_message,
    seed_message,
)
from vs_proto.api import CoreSnapshot, EpochStarted, Oneof, Record, Timestamp
from vs_sim.api.testing import examples

from .recovery_loop import MOVES, OPEN, Loop, Move, invariants, play, settle
from .recovery_worlds import Standing, StepSpec, World, build, worlds


def record(seq: int, epoch: int, body: Oneof | None, clock: float = 0.0) -> Record:
    return Record(seq=seq, epoch=epoch, at=Timestamp(seconds=seq), clock=clock, body=body)


def header(epoch: int) -> Record:
    return record(1, epoch, Oneof("epoch_started", EpochStarted(epoch=epoch)))


def saved_run(state: core.CoreState, *inputs: core.CoreEvent) -> SavedRun:
    return SavedRun(
        state=state, envelope_version=core.ENVELOPE_SCHEMA_VERSION, strategy=b"{}", inputs=inputs
    )


def snapshot_record(epoch: int) -> Record:
    """The record that holds a saved run. Core reads its epoch and clock; its body is the reader's."""
    return record(2, epoch, None)


def snapshot_loop(world: World, epoch: int) -> Loop:
    """A run resumed from a snapshot of ``world``, in the log's epoch ``epoch``."""
    state, registry = build(world)
    snapshot = snapshot_record(epoch)
    folded = apply_saved(None, saved_run(state), snapshot)
    return Loop(
        folded.state, registry, records=[header(epoch), snapshot], epoch=epoch, clock=snapshot.clock
    )


EPOCHS = st.integers(1, 4)
"""The epoch of the log the snapshot lands in; the host counted its own, which may be ahead."""


def test_a_saved_run_round_trips_its_canonical_json() -> None:
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
    assert decode_saved(encode_saved(saved), registry) == saved


@settings(max_examples=examples(pr=10, nightly=200), deadline=None)
@given(world=worlds(), epoch=EPOCHS)
def test_a_saved_run_folds_as_the_start_of_a_new_run_does(world: World, epoch: int) -> None:
    state, registry = build(world)
    started = record(2, epoch, Oneof("run_started", seed_message(state)))
    assert apply_saved(None, saved_run(state), snapshot_record(epoch)) == fold_run(
        [header(epoch), started], registry=registry
    )


@settings(max_examples=examples(pr=10, nightly=200), deadline=None)
@given(world=worlds(), epoch=EPOCHS)
def test_the_inputs_of_a_saved_run_are_applied_in_order_as_input_records_are(
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
    separately = [
        record(2, epoch, Oneof("run_started", seed_message(state))),
        *(
            record(3 + index, epoch, Oneof("core_input", input_message(event)))
            for index, event in enumerate(inputs)
        ),
    ]
    assert apply_saved(None, saved_run(state, *inputs), snapshot_record(epoch)) == fold_run(
        [header(epoch), *separately], registry=registry
    )


def test_a_snapshot_record_is_read_by_the_reader_of_the_log_and_not_by_core() -> None:
    state, registry = build(World(()))
    snapshot = record(2, 1, Oneof("core_snapshot", CoreSnapshot()))
    with pytest.raises(ValueError, match="snapshot"):
        apply_record(None, snapshot, registry)
    with pytest.raises(ValueError, match="already started"):
        apply_saved(Folded(state), saved_run(state), snapshot)


def rejected(loop: Loop, moves: list[Move], *, lag: bool) -> int | None:
    """The first move the model of the loop rejects (by an invariant it asserts), or None."""
    try:
        loop.open_epoch()
        for index, move in enumerate(moves):
            try:
                play(loop, move)
                invariants(loop)
                if not lag or move[0] == "tick":
                    loop.tick()
                    invariants(loop)
            except AssertionError:
                return index
    except AssertionError:
        return -1
    return None


@settings(max_examples=examples(pr=10, nightly=300), deadline=None)
@given(world=worlds(), epoch=EPOCHS, moves=st.lists(MOVES, max_size=12), lag=st.booleans())
def test_a_run_resumed_from_a_snapshot_decides_as_one_started_from_its_state(
    world: World, epoch: int, moves: list[Move], *, lag: bool
) -> None:
    """Every action `decide` returns is recordable, wherever the snapshot lands in the log.

    The model of the loop rejects some trajectories of any seed (the nightly depth of
    `test_recovery_progress.py` finds them: a pause with a snapshot-and-retain open, then a
    relaunch, is one). A snapshot is held to the same: it rejects the same moves, and no others.
    """
    state, registry = build(world)
    plain = rejected(Loop(state, registry), moves, lag=lag)
    assert rejected(snapshot_loop(world, epoch), moves, lag=lag) == plain


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
    assert run.clear_first


def _log_of(world: World, moves: list[Move], *, snapshot: bool, epoch: int) -> Loop:
    """The records a trajectory writes.

    The model asserts its own invariants as it plays, and the trajectories it rejects are findings
    about the rule, held by `test_recovery_progress.py` (the nightly depth of its properties
    finds some: a pause, a crash, a clock input and a second crash charge the attempt a parked run
    held). A log is a log up to the move the model rejected, and what is asserted here is only
    about the cache of its fold, so the trajectory is cut there.
    """
    if snapshot:
        loop = snapshot_loop(world, epoch)
    else:
        state, registry = build(world)
        loop = Loop(state, registry)
    try:
        loop.open_epoch()
        for move in moves:
            play(loop, move)
            loop.tick()
    except AssertionError:
        pass
    return loop


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
    loop = _log_of(world, moves, snapshot=snapshot, epoch=epoch)
    state, _ = build(world)
    if snapshot:  # the saved run seeds the fold at its record, after the header
        seed, tail = apply_saved(None, saved_run(state), loop.records[1]), loop.records[2:]
    else:
        seed, tail = Folded(state), loop.records
    cut = data.draw(st.integers(0, len(tail)), label="records the cache covers")
    whole = fold_run(tail, seed, registry=loop.registry)
    cached = fold_run(tail[:cut], seed, registry=loop.registry)
    restored = Folded.decode(cached.encode(), loop.registry)
    assert restored == cached
    assert fold_run(tail[cut:], restored, registry=loop.registry) == whole


def test_a_cache_written_by_another_encoding_or_damaged_is_refused() -> None:
    state, registry = build(World(()))
    encoded = Folded(state).encode()
    with pytest.raises(ValueError, match="encoding version 2"):
        Folded.decode(encoded.replace(b'"schema_version":1', b'"schema_version":2', 1), registry)
    with pytest.raises(ValueError, match=r"does not|invalid|JSON"):
        Folded.decode(encoded[: len(encoded) // 2], registry)
