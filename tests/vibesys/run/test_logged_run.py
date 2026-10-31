"""A process that does not run a run reads it from its run.log: the fold, and the search's last state.

The run's log is its one truth. A reader folds the log's committed prefix and takes the search's
state from the last whole state the log holds, with no search driver and no state store.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from hypothesis import given, settings
from hypothesis import strategies as st

from vibesys.run.logged_run import read_logged_run
from vibesys.run.store_migration import saved_run
from vs_core.api import ENVELOPE_SCHEMA_VERSION, FlowState, RunEnvelope
from vs_core.api.testing import FAKE_REGISTRY
from vs_project.api import Project
from vs_proto.api import EpochStarted, Oneof, Record, Timestamp
from vs_runlog.api import FileRunLogWriter
from vs_runtime.api.core import RuntimeRecord
from vs_runtime.api.step_runner import fold_log, snapshot_message
from vs_sim.api.testing import ManualClock, SimThreads, examples

if TYPE_CHECKING:
    import pytest

GOLDENS = sorted((Path(__file__).parents[3] / "libs/vs-core/tests/envelopes").glob("v*-*.json"))
RUN_ID = "run-a"
AT = datetime(2026, 1, 1, tzinfo=UTC)


def golden(path: Path) -> RuntimeRecord[FlowState]:
    envelope = FAKE_REGISTRY.decode_envelope(RunEnvelope[FlowState], path.read_text())
    assert envelope.schema_version == ENVELOPE_SCHEMA_VERSION
    cursor = envelope.event_cursor.sequence
    return RuntimeRecord[FlowState](
        schema_version=1,
        envelope=envelope,
        pending_publications=(),
        delivery_cursor=cursor,
        next_publication_sequence=cursor + 1,
        pending_inputs=(),
    )


def record(seq: int, body: Oneof, clock: float = 0.0) -> Record:
    return Record(seq=seq, epoch=1, at=Timestamp.from_datetime(AT), clock=clock, body=body)


def write_log(root: Path, records: list[Record]) -> None:
    log = Project.run_log_path_for(root, RUN_ID)
    log.parent.mkdir(parents=True, exist_ok=True)
    with FileRunLogWriter(log, clock=ManualClock(), threads=SimThreads()) as writer:
        for each in records:
            writer.append(each)


@settings(max_examples=examples(pr=4, nightly=len(GOLDENS)), deadline=None)
@given(path=st.sampled_from(GOLDENS))
def test_a_saved_run_in_a_log_reads_back_as_its_fold_with_the_search_it_saved(
    path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    root = tmp_path_factory.mktemp("logged")
    saved = golden(path)
    records = [
        record(1, Oneof("epoch_started", EpochStarted(epoch=1))),
        record(
            2,
            Oneof("core_snapshot", snapshot_message(saved_run(saved))),
            clock=saved.envelope.core.run.now_at,
        ),
    ]
    write_log(root, records)

    logged = read_logged_run(
        Project.open(root), RUN_ID, registry=FAKE_REGISTRY, state_type=FlowState
    )

    assert logged is not None
    expected = fold_log(records, FAKE_REGISTRY)
    assert expected is not None
    assert logged.core == expected.state
    assert logged.strategy == saved.envelope.strategy


def test_a_log_that_holds_no_start_of_the_run_reads_as_no_run(tmp_path: Path) -> None:
    write_log(tmp_path, [record(1, Oneof("epoch_started", EpochStarted(epoch=1)))])

    logged = read_logged_run(
        Project.open(tmp_path), RUN_ID, registry=FAKE_REGISTRY, state_type=FlowState
    )

    assert logged is None


def test_a_run_with_no_log_reads_as_no_run(tmp_path: Path) -> None:
    logged = read_logged_run(
        Project.open(tmp_path), RUN_ID, registry=FAKE_REGISTRY, state_type=FlowState
    )

    assert logged is None
