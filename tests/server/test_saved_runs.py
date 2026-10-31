"""One stream lists a project's runs: its frames stay bounded, and a failure keeps what arrived."""

from __future__ import annotations

import gzip
import json
import shutil
from dataclasses import replace
from datetime import UTC, datetime
from itertools import count
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.server.host_helper_support import fake_host, project_with_run

from server.host_api import RUN_EVENTS_FILE
from server.host_envelope import HelperErrorCode
from server.host_examples import ProjectLookupError
from server.host_helper_os import FileProjects
from server.host_helper_requests import HelperHost, registry_read_source
from server.host_operation_reads import stream_saved_runs
from server.host_run_reads import ProjectRun
from vibesys.api.host_operations.saved_runs import saved_run_frames
from vs_project.api import Project
from vs_project.api.testing import FakeGitRepositories
from vs_proto.api import (
    RUN_LOG_MAGIC,
    EpochStarted,
    FailureCategory,
    Oneof,
    OperationFailure,
    Record,
    Timestamp,
    WatchRequest,
    WatchResponse,
    validate,
)
from vs_proto.api import host_payloads as pb
from vs_runlog.api import RunLock, encode_frame
from vs_sim.api.testing import ManualClock, SimNetwork, SimThreads, examples

if TYPE_CHECKING:
    from collections.abc import Iterator

_CHECKOUTS = count()


def _host() -> HelperHost:
    threads = SimThreads()
    return replace(
        fake_host(threads, SimNetwork(threads)),
        projects=FileProjects(FakeGitRepositories().repository, threads, clock=ManualClock()),
    )


def _frames(written: bytes) -> list[pb.StreamSavedRunsResponse | OperationFailure]:
    """Each frame of the stream ``written``, read as the gateway reads them."""
    frames: list[pb.StreamSavedRunsResponse | OperationFailure] = []
    while written:
        length = int.from_bytes(written[:4], "big")
        response = WatchResponse.from_binary(written[4 : 4 + length])
        written = written[4 + length :]
        validate(response)
        assert response.result is not None
        frame = response.result.value
        assert isinstance(frame, pb.StreamSavedRunsResponse | OperationFailure)
        frames.append(frame)
    return frames


def _listed(
    host: HelperHost, project: Path | str
) -> list[pb.StreamSavedRunsResponse | OperationFailure]:
    request = WatchRequest(
        checkout="/checkout",
        method=Oneof(
            "stream_saved_runs",
            pb.StreamSavedRunsRequest(request_id="listing-a", project=str(project)),
        ),
    )
    written = bytearray()
    stream_saved_runs(request.to_binary(), registry_read_source(host), written.extend)
    return _frames(bytes(written))


def _rows(frames: list[pb.StreamSavedRunsResponse | OperationFailure]) -> list[pb.SavedRun]:
    return [
        row
        for response in frames
        if isinstance(response, pb.StreamSavedRunsResponse)
        for row in response.runs
    ]


def _run(index: int, name: str = "Saved run") -> pb.SavedRun:
    return pb.SavedRun(
        run_id=f"run-{index}",
        task="task-a",
        display_name=name,
        created_at=Timestamp.from_seconds(1790812800),
        imported=pb.SavedRunImport.AVAILABLE,
    )


def _native_log(root: Path, run_id: str = "run-1") -> tuple[Path, bytes]:
    """Lay down a native run log for ``run_id``: its path and its bytes."""
    path = Project.run_log_path_for(root, run_id)
    path.parent.mkdir(parents=True)
    fixture = Path(__file__).resolve().parents[2] / "proto/testdata/conversion/run.log.gz"
    original = gzip.decompress(fixture.read_bytes())
    path.write_bytes(original)
    path.with_name("run.head").write_bytes(fixture.with_name("run.head").read_bytes())
    return path, original


@settings(max_examples=examples(pr=20, nightly=200), deadline=None)
@given(
    sizes=st.lists(st.sampled_from([1, 100, 1000, 65536]), max_size=40),
    max_rows=st.integers(min_value=1, max_value=200),
)
def test_frames_carry_every_row_once_within_the_limits_and_only_the_last_says_complete(
    sizes: list[int], max_rows: int
) -> None:
    rows = [_run(index, "x" * size) for index, size in enumerate(sizes)]

    frames = list(saved_run_frames("bounded", "/project", rows, max_rows=max_rows))

    assert [row.run_id for response in frames for row in response.runs] == [
        row.run_id for row in rows
    ]
    assert [response.complete for response in frames] == [False] * (len(frames) - 1) + [True]
    for response in frames:
        validate(response)
        assert len(response.runs) <= max_rows
        wrapped = WatchResponse(result=Oneof("stream_saved_runs", response))
        assert len(wrapped.to_binary()) <= 1 << 20


def test_a_project_with_no_runs_is_one_complete_frame_with_no_rows(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()

    frames = _listed(_host(), root)

    assert len(frames) == 1
    assert isinstance(frames[0], pb.StreamSavedRunsResponse)
    assert frames[0].complete
    assert frames[0].request_id == "listing-a"
    assert frames[0].project_root == str(root)


def test_a_missing_project_ends_the_stream_with_a_typed_failure(tmp_path: Path) -> None:
    frames = _listed(_host(), tmp_path / "absent")

    assert len(frames) == 1
    assert isinstance(frames[0], OperationFailure)
    assert frames[0].category == FailureCategory.NOT_FOUND
    assert "absent" in frames[0].message


def test_a_failure_part_way_keeps_every_frame_before_it_and_never_says_complete(
    tmp_path: Path,
) -> None:
    class FailingProjects(FileProjects):
        def stream_runs(self, project: Path) -> tuple[Path, Iterator[ProjectRun]]:
            def rows() -> Iterator[ProjectRun]:
                for index in range(250):
                    yield ProjectRun(
                        run_id=f"run-{index}",
                        task=None,
                        display_name=f"run-{index}",
                        created_at=datetime(2026, 10, 1, tzinfo=UTC),
                    )
                raise ProjectLookupError(
                    HelperErrorCode.TASK_INVALID, str(project), "the disk went away part way"
                )

            return project, rows()

    threads = SimThreads()
    root = tmp_path / "project"
    root.mkdir()
    host = replace(
        fake_host(threads, SimNetwork(threads)),
        projects=FailingProjects(FakeGitRepositories().repository, threads, clock=ManualClock()),
    )

    frames = _listed(host, root)

    assert [run.run_id for run in _rows(frames)] == [f"run-{index}" for index in range(200)]
    assert isinstance(frames[-1], OperationFailure)
    assert not any(
        response.complete for response in frames if isinstance(response, pb.StreamSavedRunsResponse)
    )


@pytest.mark.parametrize("live", [False, True])
def test_native_history_is_listed_without_any_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, live: bool
) -> None:
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "project"
    root.mkdir()
    path, original = _native_log(root)

    lock = RunLock(path.parent / "run.lock") if live else None
    try:
        frames = _listed(_host(), root)
    finally:
        if lock is not None:
            lock.close()

    assert [(row.run_id, row.imported) for row in _rows(frames)] == [
        ("run-1", pb.SavedRunImport.AVAILABLE)
    ]
    assert path.read_bytes() == original
    assert Project.indexed_run_directories() == ()


def test_a_recorded_run_with_no_native_log_is_labelled_old_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path / "state"))
    project = project_with_run(tmp_path / "project", "run-1")

    (row,) = _rows(_listed(_host(), project))

    assert row.run_id == "run-1"
    assert row.imported == pb.SavedRunImport.UNAVAILABLE
    assert row.detail == "old format"


def test_native_only_runs_are_dated_from_their_first_epoch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "project"
    root.mkdir()
    for run_id, day in [("run-a", 20), ("run-z", 2)]:
        path = Project.run_log_path_for(root, run_id)
        path.parent.mkdir(parents=True)
        path.write_bytes(
            RUN_LOG_MAGIC
            + b"".join(
                encode_frame(
                    Record(
                        seq=epoch,
                        epoch=epoch,
                        at=Timestamp.from_seconds(datetime(2026, 10, date, tzinfo=UTC).timestamp()),
                        body=Oneof("epoch_started", EpochStarted(epoch=epoch)),
                    )
                )
                for epoch, date in [(1, day), (2, 25)]
            )
        )

    rows = _rows(_listed(_host(), root))

    assert {row.run_id: row.created_at for row in rows} == {
        run_id: Timestamp.from_seconds(datetime(2026, 10, day, tzinfo=UTC).timestamp())
        for run_id, day in [("run-z", 2), ("run-a", 20)]
    }


def _journal(project: Path, run_id: str) -> None:
    journal = Project.log_directory_for(project, run_id) / RUN_EVENTS_FILE
    journal.parent.mkdir(parents=True, exist_ok=True)
    event = {"sequence": 1, "run_id": run_id, "timestamp": "2026-10-01T00:00:00+00:00"}
    journal.write_bytes(json.dumps(event).encode() + b"\n")


def _remove(project: Path, run_id: str) -> None:
    shutil.rmtree(Project.log_directory_for(project, run_id).parent, ignore_errors=True)
    shutil.rmtree(Project.open(project).state.run_directory(run_id), ignore_errors=True)


_KEPT = 4
_CHURNED = 4
_STEPS = st.lists(
    st.tuples(st.sampled_from(["add", "remove"]), st.integers(min_value=0, max_value=_CHURNED - 1)),
    max_size=_KEPT + _CHURNED,
)


@settings(
    max_examples=examples(pr=15, nightly=150),
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(steps=_STEPS)
def test_runs_added_or_removed_during_a_listing_never_fail_it_or_cost_a_run_that_stayed(
    tmp_path: Path, steps: list[tuple[str, int]]
) -> None:
    project = project_with_run(tmp_path / f"proj-{next(_CHECKOUTS)}", "kept-recorded").resolve()
    kept = ["kept-recorded", *(f"kept-{index}" for index in range(_KEPT - 1))]
    for run_id in kept[1:]:
        _journal(project, run_id)
    for index in range(0, _CHURNED, 2):
        _journal(project, f"churned-{index}")
    root, rows = _host().projects.stream_runs(project)
    remaining = iter(steps)

    listed: list[str] = []
    for row in rows:
        listed.append(row.run_id)
        for action, index in remaining:
            if action == "add":
                _journal(project, f"churned-{index}")
            else:
                _remove(project, f"churned-{index}")
            break

    assert root == project
    assert sorted(run_id for run_id in listed if run_id in kept) == sorted(kept)
    assert len(listed) == len(set(listed))
