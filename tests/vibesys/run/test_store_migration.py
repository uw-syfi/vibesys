"""A run saved before the cutover enters its run.log once, whole, and runs from there.

The saved envelopes are the golden ones of every version the build still reads, each decoded to the
current version the way a resume reads it. Pre-cutover runs of every standing (steps dispatched or
reconciling with no attempt row, host epochs ahead of the log's, wake-ups and held decisions) must
fold without refusal, and `decide` must run every open step to a result.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vibesys.run.store_migration import (
    Migration,
    SnapshotNotAppendedError,
    SnapshotTooLargeError,
    migrate_saved_run,
    saved_run,
)
from vs_core.api import FlowState, RunEnvelope
from vs_core.api.recovery import (
    Cancelled,
    CancelReason,
    Folded,
    Run,
    Settle,
    decide,
    finished_message,
    input_message,
    seed_message,
    started_message,
)
from vs_core.api.testing import FAKE_REGISTRY, FakeView
from vs_project.api import (
    OrchestrationDescriptor,
    Project,
    RunEnvironmentRecord,
    StoredEnvelope,
)
from vs_project.api.testing import run_execution_record, seed_stored_record
from vs_proto.api import EpochStarted, Oneof, Record, Timestamp
from vs_runlog.api import ConfirmedLog
from vs_runtime.api.core import CoreResumeError, RuntimeRecord
from vs_runtime.api.step_runner import fold_log, restore_log, snapshot_message
from vs_runtime.api.testing import CounterConfig, FakeRunRecords, counter
from vs_sim.api.testing import SimThreads, examples

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vs_core.api import SearchDriver
    from vs_proto.api import CoreSnapshot

GOLDENS = sorted((Path(__file__).parents[3] / "libs/vs-core/tests/envelopes").glob("v*-*.json"))
OPEN = (core.RequestPhase.PREPARED, core.RequestPhase.DISPATCHED, core.RequestPhase.RECONCILING)
IN_FLIGHT = (core.RequestPhase.DISPATCHED, core.RequestPhase.RECONCILING)


def golden(path: Path) -> RuntimeRecord[FlowState]:
    """The runtime record a pre-cutover run saved, as a resume decodes it: at the current version."""
    envelope = FAKE_REGISTRY.decode_envelope(RunEnvelope[FlowState], path.read_text())
    assert envelope.schema_version == core.ENVELOPE_SCHEMA_VERSION
    cursor = envelope.event_cursor.sequence
    return RuntimeRecord[FlowState](
        schema_version=1,
        envelope=envelope,
        pending_publications=(),
        delivery_cursor=cursor,
        next_publication_sequence=cursor + 1,
        pending_inputs=(),
    )


def entry(seq: int, epoch: int, body: Oneof, clock: float = 0.0) -> Record:
    return Record(seq=seq, epoch=epoch, at=Timestamp(seconds=seq), clock=clock, body=body)


def header(seq: int, epoch: int) -> Record:
    return entry(seq, epoch, Oneof("epoch_started", EpochStarted(epoch=epoch)))


def folded(records: Sequence[Record]) -> Folded:
    """The fold of a log, a snapshot in it read as a resume reads it."""
    result = fold_log(records, FAKE_REGISTRY)
    assert result is not None, "the log holds no start of the run"
    return result


def folded_from(record: RuntimeRecord[FlowState], epoch: int) -> core.CoreState:
    """The state a process of the log's ``epoch`` folds from the snapshot of ``record``."""
    snapshot = entry(
        2,
        epoch,
        Oneof("core_snapshot", _message(record)),
        clock=record.envelope.core.run.now_at,
    )
    return folded([header(1, epoch), snapshot]).state


def _message(record: RuntimeRecord[FlowState]) -> CoreSnapshot:
    return snapshot_message(saved_run(record))


@st.composite
def pre_cutover(draw: st.DrawFn) -> RuntimeRecord[FlowState]:
    """A golden saved run with its open steps in the standing an earlier version leaves them.

    Each open step is prepared, dispatched or reconciling with no attempt row, with a limit of
    its own, and the host counted an epoch the log's numbering may be behind.
    """
    saved = golden(draw(st.sampled_from(GOLDENS)))
    state = saved.envelope.core
    rows = tuple(
        row.model_copy(
            update={
                "phase": draw(st.sampled_from(OPEN)),
                "until": row.until.model_copy(
                    update={"at": draw(st.sampled_from([2.0, 10.0, 100.0]))}
                ),
            }
        )
        if row.phase in OPEN
        else row
        for row in state.records.records
    )
    epoch = draw(st.sampled_from([0, 1, 7]))
    limits = state.run.limits.model_copy(
        update={
            "request_retries": state.run.limits.request_retries.model_copy(
                update={"limit": draw(st.integers(0, 3))}
            )
        }
    )
    state = state.model_copy(
        update={
            "run": state.run.model_copy(update={"limits": limits}),
            "records": state.records.model_copy(update={"records": rows, "epoch": epoch}),
        }
    )
    envelope = saved.envelope.model_copy(update={"core": state})
    wakes = tuple(draw(st.lists(st.floats(0, 1000, allow_nan=False), max_size=3)))
    return saved.model_copy(update={"envelope": envelope, "wakes": wakes})


def run_to_results(records: list[Record], epoch: int) -> core.CoreState:
    """Runners that cooperate: what the loop records for what `decide` asks, until nothing is left.

    The process of ``epoch`` resumes a run that is paused, and each step it is asked to run ends
    cancelled, which is a result any step can have.
    """
    state = folded(records).state
    if state.run.status in (core.RunStatus.PARKED, core.RunStatus.PAUSED):
        resume = core.ControlInput(
            control_id=core.ControlId(root="resume"), action="resume", when="now"
        )
        event = core.RunControlEvent(control=resume, now_at=state.run.now_at)
        _add(records, epoch, Oneof("core_input", input_message(event)))
    for _ in range(30):
        state = folded(records).state
        actions = [a for a in decide(state, state.run.now_at) if isinstance(a, Run | Settle)]
        if not actions:
            return state
        for action in actions:
            if isinstance(action, Run):
                _add(records, epoch, Oneof("step_started", started_message(action)))
                result = Cancelled(reason=CancelReason.WITHDRAWN)
                _add(
                    records,
                    epoch,
                    Oneof("step_finished", finished_message(action.tag, action.attempt, result)),
                )
            else:
                attempts = state.steps.of(action.tag)
                last = 0 if attempts is None else attempts.started
                body = Oneof("step_finished", finished_message(action.tag, last, action.result))
                _add(records, epoch, body)
    return folded(records).state


def _add(records: list[Record], epoch: int, body: Oneof) -> None:
    clock = folded(records).state.run.now_at
    records.append(entry(len(records) + 1, epoch, body, clock))
    folded(records)  # a record the fold refuses fails the test here


@pytest.mark.parametrize("path", GOLDENS, ids=lambda p: p.stem)
def test_a_golden_envelope_migrates_and_every_open_request_of_it_runs_to_a_result(
    path: Path,
) -> None:
    saved = golden(path)
    state = folded_from(saved, epoch=2)
    assert state.run.run_id == saved.envelope.core.run.run_id
    snapshot = entry(2, 2, Oneof("core_snapshot", _message(saved)), saved.envelope.core.run.now_at)
    records = [header(1, 2), snapshot, header(3, 3)]
    ended = run_to_results(records, epoch=3)
    assert not [row for row in ended.records.records if row.phase in OPEN]


@pytest.mark.parametrize("path", [p for p in GOLDENS if golden(p).envelope.core.records.records])
def test_decide_asks_for_every_open_request_of_a_golden_once_the_new_epoch_has_opened(
    path: Path,
) -> None:
    saved = golden(path)
    records = [
        header(1, 2),
        entry(2, 2, Oneof("core_snapshot", _message(saved)), 0.0),
        header(3, 3),
    ]
    state = folded(records).state
    open_tags = {row.request_id for row in state.records.records if row.phase in OPEN}
    runs = {a.tag for a in decide(state, state.run.now_at) if isinstance(a, Run)}
    if state.run.status is core.RunStatus.PARKED:
        assert runs == set()
    else:
        assert runs == open_tags


@settings(max_examples=examples(pr=10, nightly=200), deadline=None)
@given(saved=pre_cutover(), epoch=st.integers(1, 4))
def test_a_pre_cutover_run_of_any_standing_folds_without_refusal_and_runs_to_results(
    saved: RuntimeRecord[FlowState], epoch: int
) -> None:
    snapshot = entry(
        2, epoch, Oneof("core_snapshot", _message(saved)), saved.envelope.core.run.now_at
    )
    records = [header(1, epoch), snapshot, header(3, epoch + 1)]
    ended = run_to_results(records, epoch=epoch + 1)
    assert not [row for row in ended.records.records if row.phase in OPEN]


def in_flight(
    saved: RuntimeRecord[FlowState], phase: core.RequestPhase
) -> RuntimeRecord[FlowState]:
    """``saved`` with every open step in ``phase``, as a process that died mid-step leaves it."""
    state = saved.envelope.core
    rows = tuple(
        row.model_copy(update={"phase": phase}) if row.phase in OPEN else row
        for row in state.records.records
    )
    state = state.model_copy(update={"records": state.records.model_copy(update={"records": rows})})
    return saved.model_copy(update={"envelope": saved.envelope.model_copy(update={"core": state})})


@pytest.mark.parametrize("phase", IN_FLIGHT)
@pytest.mark.parametrize(
    "path", [p for p in GOLDENS if p.stem.endswith("floor")], ids=lambda p: p.stem
)
def test_every_step_in_flight_in_a_pre_cutover_run_is_cleared_before_it_runs_again(
    path: Path, phase: core.RequestPhase
) -> None:
    saved = in_flight(golden(path), phase)
    snapshot = entry(2, 2, Oneof("core_snapshot", _message(saved)), 0.0)
    state = folded([header(1, 2), snapshot, header(3, 3)]).state
    runs = [a for a in decide(state, state.run.now_at) if isinstance(a, Run)]
    assert runs
    assert all(run.clear_first for run in runs)


# --- the migration of a project's run -------------------------------------------------------------


def search() -> SearchDriver[FlowState, CounterConfig]:
    return counter.driver(CounterConfig())


def saved_counter(run_id: str, *, wakes: tuple[float, ...] = ()) -> RuntimeRecord[FlowState]:
    strategy = search()
    state = core.initial_state()
    state = state.model_copy(
        update={
            "run": state.run.model_copy(
                update={"run_id": core.RunId(root=run_id), "declaration": strategy.declaration}
            )
        }
    )
    envelope = RunEnvelope[FlowState](
        schema_version=core.ENVELOPE_SCHEMA_VERSION,
        strategy_id=strategy.declaration.strategy_id,
        state_schema=strategy.declaration.state_schema,
        core=state,
        strategy=strategy.init(CounterConfig(), state.run.facts, FakeView().current())[0],
        event_cursor=core.EventCursor(sequence=0),
    )
    return RuntimeRecord[FlowState].fresh(envelope).model_copy(update={"wakes": wakes})


@dataclass
class Saved:
    project: Project
    run_id: str


def saved_project(root: Path, *, wakes: tuple[float, ...] = (), claims: str | None = None) -> Saved:
    """A project whose run has a ``store.json``, as the cutover finds one.

    ``claims`` is the run the saved envelope says it belongs to, when it is not this one.
    """
    project = Project.open(root)
    project.state.create_project("migration-test", now=datetime(2026, 1, 1, tzinfo=UTC))
    manifest = project.state.new_run_manifest(
        "migration-test",
        branch="test",
        vibesys_version="test",
        run_environment=RunEnvironmentRecord(name="docker"),
        execution=run_execution_record(),
        orchestration=OrchestrationDescriptor(id="dynamic", config_version=1, options={}),
        trusted_input_baseline="a" * 40,
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    project.state.create_run(manifest)
    run_id = manifest.run_id
    payload = saved_counter(claims or run_id, wakes=wakes).model_dump_json().encode()
    seed_stored_record(
        project, run_id, StoredEnvelope(revision=0, schema_version=1, payload=payload)
    )
    return Saved(project, run_id)


LOST = "the append was lost"
UNCONFIRMED = "the sync failed after the record was written"


class Records(FakeRunRecords):
    """Durable records that can fail an append, or fail to confirm one that is durable."""

    def __init__(self) -> None:
        super().__init__()
        self.lose_next_append = False
        self.fail_next_commit = False

    def append(self, record: Record) -> None:
        if self.lose_next_append:
            self.lose_next_append = False
            raise OSError(LOST)
        super().append(record)

    def commit(self) -> None:
        if self.fail_next_commit:
            self.fail_next_commit = False
            raise OSError(UNCONFIRMED)
        super().commit()


def resume(saved: Saved, records: FakeRunRecords) -> Migration:
    """What a resuming process does first: open its epoch, then migrate if the log holds no start."""
    log = ConfirmedLog(records, SimThreads())
    epoch = (records.records[-1].epoch if records.records else 0) + 1
    log.append(EpochStarted(epoch=epoch), epoch=epoch, at=Timestamp(), clock=0.0)
    return migrate_saved_run(
        saved.project,
        saved.run_id,
        log,
        search=search(),
        registry=FAKE_REGISTRY,
        epoch=epoch,
        stamp=Timestamp,
    )


def snapshots(records: FakeRunRecords) -> list[Record]:
    return [r for r in records.records if r.body is not None and r.body.field == "core_snapshot"]


def test_a_run_with_no_log_and_a_store_migrates_once(tmp_path: Path) -> None:
    saved = saved_project(tmp_path)
    records = FakeRunRecords()
    assert resume(saved, records) is Migration.MIGRATED
    (snapshot,) = snapshots(records)
    assert snapshot.epoch == 1
    expected = saved_counter(saved.run_id).envelope.core
    state = folded(records.records).state
    assert state.run.run_id == expected.run.run_id
    before = snapshots(records)
    assert resume(saved, records) is Migration.STARTED
    assert snapshots(records) == before


def test_the_store_is_left_as_it_was(tmp_path: Path) -> None:
    saved = saved_project(tmp_path)
    stored = saved.project.stored_record(saved.run_id)
    resume(saved, FakeRunRecords())
    assert saved.project.stored_record(saved.run_id) == stored


def test_the_search_the_saved_run_left_is_the_search_the_log_restores(tmp_path: Path) -> None:
    wakes = (30.0, 90.0)
    saved = saved_project(tmp_path, wakes=wakes)
    records = FakeRunRecords()
    assert resume(saved, records) is Migration.MIGRATED
    restored = restore_log(records.records, search(), FAKE_REGISTRY)
    assert restored is not None
    assert restored.wakes == wakes
    assert restored.state == saved_counter(saved.run_id).envelope.strategy


def test_a_run_that_was_never_saved_is_new(tmp_path: Path) -> None:
    project = Project.open(tmp_path)
    nothing = Saved(project, "not-a-saved-run")
    records = FakeRunRecords()
    assert resume(nothing, records) is Migration.NOTHING_SAVED
    assert snapshots(records) == []


def test_a_log_that_holds_the_start_of_a_new_run_is_not_migrated_over_it(tmp_path: Path) -> None:
    saved = saved_project(tmp_path)
    records = FakeRunRecords()
    seed = saved_counter(saved.run_id).envelope.core
    records.records.extend([header(1, 1), entry(2, 1, Oneof("run_started", seed_message(seed)))])
    assert resume(saved, records) is Migration.STARTED
    assert snapshots(records) == []


def test_a_store_that_belongs_to_another_run_is_refused_and_the_log_gets_no_snapshot(
    tmp_path: Path,
) -> None:
    saved = saved_project(tmp_path, claims="another-run")
    records = FakeRunRecords()
    with pytest.raises(CoreResumeError):
        resume(saved, records)
    assert snapshots(records) == []


@pytest.mark.parametrize(
    "crash_after", [0, 1, 2], ids=["before header", "after header", "after snapshot"]
)
def test_the_migration_is_idempotent_under_a_crash_at_every_append(
    tmp_path: Path, crash_after: int
) -> None:
    """A resume appends a header and a snapshot; the process dies after any number of them."""
    saved = saved_project(tmp_path)
    whole = FakeRunRecords()
    resume(saved, whole)
    survivors = FakeRunRecords()
    survivors.records.extend(whole.records[:crash_after])
    assert resume(saved, survivors) in (Migration.MIGRATED, Migration.STARTED)
    assert len(snapshots(survivors)) == 1
    again = len(survivors.records)
    assert resume(saved, survivors) is Migration.STARTED
    assert len(snapshots(survivors)) == 1
    assert len(survivors.records) == again + 1  # a header, as every process writes
    assert folded(survivors.records).state.run == folded(whole.records).state.run


@pytest.mark.parametrize("durable", [False, True], ids=["lost", "durable but unconfirmed"])
def test_an_append_that_cannot_be_confirmed_leaves_the_next_resume_to_decide(
    tmp_path: Path, *, durable: bool
) -> None:
    saved = saved_project(tmp_path)
    records = Records()
    log_epoch = 1
    records.records.append(header(1, log_epoch))
    log = ConfirmedLog(records, SimThreads())
    if durable:
        records.fail_next_commit = True
    else:
        records.lose_next_append = True
    with pytest.raises(SnapshotNotAppendedError):
        migrate_saved_run(
            saved.project,
            saved.run_id,
            log,
            search=search(),
            registry=FAKE_REGISTRY,
            epoch=log_epoch,
            stamp=Timestamp,
        )
    assert len(snapshots(records)) == int(durable)
    assert resume(saved, records) is (Migration.STARTED if durable else Migration.MIGRATED)
    assert len(snapshots(records)) == 1


def test_a_saved_run_too_large_for_a_record_is_refused_by_name(tmp_path: Path) -> None:
    wakes = tuple((index * 0.7310585786300049) % 1e6 for index in range(200_000))
    big = saved_project(tmp_path, wakes=wakes)
    records = FakeRunRecords()
    with pytest.raises(SnapshotTooLargeError, match=big.run_id):
        resume(big, records)
    assert snapshots(records) == []
