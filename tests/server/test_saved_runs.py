"""Native discovery pages preserve metadata and label runs kept in the old format."""

from __future__ import annotations

import gzip
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.server.host_helper_support import fake_host

from server.host_helper_os import FileProjects
from server.host_helper_requests import registry_read_source
from server.host_operation_reads import answer_registered_read
from server.host_saved_runs import page_saved_runs
from vs_project.api import Project
from vs_project.api.testing import FakeGitRepositories
from vs_proto.api import (
    RUN_LOG_MAGIC,
    EpochStarted,
    Oneof,
    ReadRequest,
    ReadResponse,
    Record,
    Timestamp,
    validate,
)
from vs_proto.api import host_payloads as pb
from vs_runlog.api import RunLock, encode_frame
from vs_sim.api.testing import ManualClock, SimNetwork, SimThreads


def _run(index: int, name: str = "Saved run") -> pb.SavedRun:
    return pb.SavedRun(
        run_id=f"run-{index}",
        task="task-a",
        display_name=name,
        created_at=Timestamp.from_seconds(1790812800),
        imported=pb.SavedRunImport.AVAILABLE,
    )


def test_discovery_fences_pages_preserves_metadata_and_reports_missing_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "project"
    root.mkdir()
    entries = [_run(index) for index in range(230)]
    first = page_saved_runs(
        pb.ListSavedRunsRequest(request_id="first", project=str(root)), root, entries
    )
    assert len(first.runs) == 200
    assert first.next_offset == 200
    assert all(row.imported == pb.SavedRunImport.UNAVAILABLE for row in first.runs)
    assert all(row.detail == "old format" for row in first.runs)
    assert first.runs[0].task == "task-a"
    assert first.runs[0].display_name == "Saved run"
    request = pb.ListSavedRunsRequest(
        request_id="next", project=str(root), offset=first.next_offset, revision=first.revision
    )
    second = page_saved_runs(request, root, entries)
    assert [row.run_id for row in first.runs + second.runs] == [row.run_id for row in entries]
    assert not second.has_field("next_offset")
    assert all(row.imported == pb.SavedRunImport.AVAILABLE for row in entries)
    with pytest.raises(ValueError, match="revision"):
        page_saved_runs(request, root, [*entries, _run(999)])
    with pytest.raises(ValueError, match="offset"):
        page_saved_runs(
            pb.ListSavedRunsRequest(request_id="bad", project=str(root), offset=999), root, entries
        )


@pytest.mark.parametrize("size", [1, 1000, 65536])
def test_discovery_obeys_the_response_byte_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, size: int
) -> None:
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "project"
    root.mkdir()
    entries = [_run(index, "x" * size) for index in range(200)]
    result = page_saved_runs(
        pb.ListSavedRunsRequest(request_id="bounded", project=str(root)), root, entries
    )
    assert len(result.to_binary()) <= 1 << 20
    assert len(result.runs) <= 200
    assert result.has_field("next_offset") == (len(result.runs) < len(entries))
    if result.has_field("next_offset"):
        assert result.next_offset == len(result.runs)


@pytest.mark.parametrize("live", [False, True])
def test_native_history_is_listed_without_any_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, live: bool
) -> None:
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "project"
    root.mkdir()
    path = Project.run_log_path_for(root, "run-1")
    path.parent.mkdir(parents=True)
    fixture = Path(__file__).resolve().parents[2] / "proto/testdata/conversion/run.log.gz"
    original = gzip.decompress(fixture.read_bytes())
    path.write_bytes(original)
    path.with_name("run.head").write_bytes(fixture.with_name("run.head").read_bytes())
    lock = RunLock(path.parent / "run.lock") if live else None
    try:
        result = page_saved_runs(
            pb.ListSavedRunsRequest(request_id="native", project=str(root)), root, [_run(1)]
        )
    finally:
        if lock is not None:
            lock.close()
    assert result.runs[0].imported == pb.SavedRunImport.AVAILABLE
    assert path.read_bytes() == original
    assert Project.indexed_run_directories() == ()


def test_discovery_includes_native_only_runs_with_no_portable_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "project"
    root.mkdir()
    path = Project.run_log_path_for(root, "run-1")
    path.parent.mkdir(parents=True)
    fixture = Path(__file__).resolve().parents[2] / "proto/testdata/conversion/run.log.gz"
    path.write_bytes(gzip.decompress(fixture.read_bytes()))
    path.with_name("run.head").write_bytes(fixture.with_name("run.head").read_bytes())
    source = FileProjects(FakeGitRepositories().repository, SimThreads(), clock=ManualClock())
    response = source.saved_runs(
        pb.ListSavedRunsRequest(request_id="native-only", project=str(root))
    )
    assert [run.run_id for run in response.runs] == ["run-1"]
    assert response.runs[0].imported == pb.SavedRunImport.AVAILABLE


@pytest.mark.parametrize("live", [False, True])
def test_registry_read_discovers_native_history_through_the_public_host_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, live: bool
) -> None:
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "project"
    root.mkdir()
    path = Project.run_log_path_for(root, "run-1")
    path.parent.mkdir(parents=True)
    fixture = Path(__file__).resolve().parents[2] / "proto/testdata/conversion/run.log.gz"
    original = gzip.decompress(fixture.read_bytes())
    path.write_bytes(original)
    path.with_name("run.head").write_bytes(fixture.with_name("run.head").read_bytes())
    threads = SimThreads()
    host = replace(
        fake_host(threads, SimNetwork(threads)),
        projects=FileProjects(FakeGitRepositories().repository, threads, clock=ManualClock()),
    )
    request = ReadRequest(
        checkout="/checkout",
        method=Oneof(
            "list_saved_runs", pb.ListSavedRunsRequest(request_id="discovery-a", project=str(root))
        ),
    )
    lock = RunLock(path.parent / "run.lock") if live else None
    try:
        encoded = answer_registered_read(
            "list-saved-runs", request.to_binary(), registry_read_source(host)
        )
    finally:
        if lock is not None:
            lock.close()
    response = ReadResponse.from_binary(encoded)
    validate(response)
    assert response.result is not None
    assert response.result.field == "list_saved_runs"
    listing = response.result.value
    assert listing.request_id == "discovery-a"
    assert listing.project_root == str(root)
    assert [row.run_id for row in listing.runs] == ["run-1"]
    assert listing.runs[0].imported == pb.SavedRunImport.AVAILABLE
    assert path.read_bytes() == original


def test_discovery_dates_native_only_runs_from_the_first_epoch_and_sorts_them(
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
    threads = SimThreads()
    host = replace(
        fake_host(threads, SimNetwork(threads)),
        projects=FileProjects(FakeGitRepositories().repository, threads, clock=ManualClock()),
    )
    request = ReadRequest(
        checkout="/checkout",
        method=Oneof(
            "list_saved_runs", pb.ListSavedRunsRequest(request_id="dated", project=str(root))
        ),
    )
    response = ReadResponse.from_binary(
        answer_registered_read("list-saved-runs", request.to_binary(), registry_read_source(host))
    )
    validate(response)
    assert response.result is not None
    assert response.result.field == "list_saved_runs"
    assert [(row.run_id, row.created_at) for row in response.result.value.runs] == [
        (run_id, Timestamp.from_seconds(datetime(2026, 10, day, tzinfo=UTC).timestamp()))
        for run_id, day in [("run-z", 2), ("run-a", 20)]
    ]
