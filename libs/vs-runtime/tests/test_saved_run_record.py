"""A saved run as a record of the run.log: what it carries, and how a reader of a log folds it.

Core folds a `SavedRun`; the snapshot record packs its canonical JSON compressed. Whatever the saved
run holds, the record gives it back, and a log's reader folds the snapshot as core folds the run
decoded from it. A record another encoding wrote, or damage left, is refused by name.
"""

from __future__ import annotations

import json
import zlib

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api.recovery import SavedRun, apply_record, apply_saved, seed_message
from vs_proto.api import EpochStarted, Oneof, Record, Timestamp
from vs_runtime.api.step_runner import fold_log, saved_run_in, snapshot_message
from vs_runtime.api.testing import fake_run_seed
from vs_sim.api.testing import examples


def record(seq: int, body: Oneof, epoch: int = 1, clock: float = 0.0) -> Record:
    return Record(seq=seq, epoch=epoch, at=Timestamp(seconds=seq), clock=clock, body=body)


def snapshot_of(saved: SavedRun, *, epoch: int = 1, seq: int = 2) -> Record:
    return record(
        seq, Oneof("core_snapshot", snapshot_message(saved)), epoch, saved.state.run.now_at
    )


STATE = fake_run_seed((100.0,), 1).state
CLOCKS = st.lists(st.floats(0.0, 1e6), max_size=4).map(sorted)
"""Run-clock times in order: the clock only moves forward, so a saved run's inputs do too."""
SAVED = st.builds(
    SavedRun,
    state=st.just(STATE),
    envelope_version=st.just(core.ENVELOPE_SCHEMA_VERSION),
    strategy=st.binary(max_size=2048),
    inputs=CLOCKS.map(lambda times: tuple(core.ClockAdvanced(now_at=t) for t in times)),
    wakes=st.lists(st.floats(0.0, 1e6), max_size=4).map(tuple),
)


@settings(max_examples=examples(pr=20, nightly=300), deadline=None)
@given(saved=SAVED)
def test_a_snapshot_record_gives_back_the_run_it_holds(saved: SavedRun) -> None:
    registry = fake_run_seed((100.0,), 1).registry
    assert saved_run_in(snapshot_of(saved), registry) == saved


@settings(max_examples=examples(pr=20, nightly=300), deadline=None)
@given(saved=SAVED, epoch=st.integers(1, 4))
def test_a_reader_folds_a_snapshot_as_core_folds_the_run_it_holds(
    saved: SavedRun, epoch: int
) -> None:
    registry = fake_run_seed((100.0,), 1).registry
    snapshot = snapshot_of(saved, epoch=epoch)
    header = record(1, Oneof("epoch_started", EpochStarted(epoch=epoch)), epoch)
    assert fold_log([header, snapshot], registry) == apply_saved(None, saved, snapshot)


def test_a_reader_folds_a_record_that_is_not_a_snapshot_as_core_does() -> None:
    seed = fake_run_seed((100.0,), 1)
    started = record(2, Oneof("run_started", seed_message(seed.state)))
    assert fold_log([started], seed.registry) == apply_record(None, started, seed.registry)
    assert saved_run_in(started, seed.registry) is None


def test_a_snapshot_that_does_not_inflate_is_refused_by_name() -> None:
    seed = fake_run_seed((100.0,), 1)
    message = snapshot_message(SavedRun(seed.state, core.ENVELOPE_SCHEMA_VERSION, b"{}"))
    message.state = b"not compressed"
    with pytest.raises(ValueError, match="does not inflate"):
        saved_run_in(record(2, Oneof("core_snapshot", message)), seed.registry)


def test_a_snapshot_another_encoding_wrote_is_refused() -> None:
    seed = fake_run_seed((100.0,), 1)
    message = snapshot_message(SavedRun(seed.state, core.ENVELOPE_SCHEMA_VERSION, b"{}"))
    message.schema_version += 1
    with pytest.raises(ValueError, match="encoding version"):
        saved_run_in(record(2, Oneof("core_snapshot", message)), seed.registry)


@pytest.mark.regression
@example(epoch=2, pending=True)
@settings(max_examples=examples(pr=20, nightly=200), deadline=None)
@given(epoch=st.integers(0, 50), pending=st.booleans())
def test_a_snapshot_written_at_version_43_is_read_with_its_epoch_and_without_the_removed_state(
    epoch: int, *, pending: bool
) -> None:
    """Version 43 wrote ``hosts``, ``children`` and ``recovery`` in the records state (#3708).

    Delete this test with the tolerant read, after 2026-11-08.
    """
    registry = fake_run_seed((100.0,), 1).registry
    saved = SavedRun(STATE, core.ENVELOPE_SCHEMA_VERSION, b"{}")
    message = snapshot_message(saved)
    document = json.loads(zlib.decompress(message.state))
    records = document["records"]
    assert "epoch" in records
    del records["epoch"]
    records["hosts"] = []
    records["children"] = []
    records["recovery"] = {
        "checks": [],
        "epoch": epoch,
        "phase": "recovering" if pending else "ready",
    }
    message.state = zlib.compress(json.dumps(document).encode())
    read = saved_run_in(record(2, Oneof("core_snapshot", message)), registry)
    assert read is not None
    assert read.state.records.epoch == epoch
    assert read.state == STATE.model_copy(
        update={"records": STATE.records.model_copy(update={"epoch": epoch})}
    )
