"""Registry commands run through the public binary-stdio entrypoint in process."""

from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO
from typing import TYPE_CHECKING

import pytest
from tests.support.task_project import write_task

from entrypoints.host import main
from vs_project.api import Project
from vs_proto.api import MutateRequest, MutateResponse, Oneof, ReadRequest, ReadResponse, validate
from vs_proto.api import host_payloads as pb
from vs_sim.api.testing import FakeProcesses, ProcessScript, SimThreads

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def host_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every registry, admission and configuration path belongs to this test."""
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def invoke(command: str, payload: bytes) -> bytes:
    """Exercise dispatch, framing and the command with simulated scheduling."""
    threads = SimThreads()
    processes = FakeProcesses(lambda _: ProcessScript(), pass_time=threads.sleep)
    output = BytesIO()
    assert (
        main([command], stdin=BytesIO(payload), stdout=output, processes=processes, threads=threads)
        == 0
    )
    assert not processes.ran
    return output.getvalue()


def test_registered_read_returns_the_filesystem_answer_in_its_envelope(host_root: Path) -> None:
    folder = host_root / "projects"
    folder.mkdir()
    Project.open(folder).state.create_project("example", now=datetime(2026, 1, 1, tzinfo=UTC))
    write_task(folder, "", task="alpha")
    write_task(folder, "", task="beta")
    request = ReadRequest(
        checkout="/src/checkout",
        method=Oneof("list_tasks", pb.ListTasksRequest(request_id="tasks-a", project=str(folder))),
    )
    validate(request)
    response = ReadResponse.from_binary(invoke("list-tasks", request.to_binary()))
    validate(response)
    assert response.result is not None
    assert response.result.field == "list_tasks"
    assert response.result.value.tasks is not None
    assert [task.name for task in response.result.value.tasks.tasks] == ["alpha", "beta"]


def test_project_refusal_replays_without_reapplying_a_now_available_source(host_root: Path) -> None:
    source = host_root / "source"
    destination = host_root / "destination"
    request = MutateRequest(
        checkout="/src/checkout",
        method=Oneof(
            "copy_project",
            pb.CopyProjectRequest(
                request_id="copy-a", source=str(source), destination=str(destination)
            ),
        ),
    )
    validate(request)
    first = invoke("copy-project", request.to_binary())
    response = MutateResponse.from_binary(first)
    validate(response)
    assert response.result is not None
    assert response.result.field == "failure"
    assert "no folder to copy out" in response.result.value.message
    source.mkdir()
    assert invoke("copy-project", request.to_binary()) == first
    assert not destination.exists()


def test_cluster_refusal_is_typed_and_replayed(host_root: Path) -> None:
    del host_root
    request = MutateRequest(
        checkout="/src/checkout",
        method=Oneof("remove_cluster", pb.RemoveClusterRequest(request_id="remove-a", name="lab")),
    )
    validate(request)
    first = invoke("remove-cluster", request.to_binary())
    response = MutateResponse.from_binary(first)
    validate(response)
    assert response.result is not None
    assert response.result.field == "failure"
    assert "no cluster named" in response.result.value.message
    assert invoke("remove-cluster", request.to_binary()) == first


@pytest.mark.regression
@pytest.mark.parametrize("kind", ["missing", "file", "uninitialized"])
def test_start_refusal_is_typed_and_replayed_without_launching(host_root: Path, kind: str) -> None:
    project = host_root / "unavailable-project"
    if kind == "file":
        project.touch()
    elif kind == "uninitialized":
        project.mkdir()
    request = MutateRequest(
        checkout="/src/checkout",
        method=Oneof(
            "start_run",
            pb.StartRunRequest(request_id="start-a", project=str(project), task="task-a"),
        ),
    )
    validate(request)
    first = invoke("start-run", request.to_binary())
    response = MutateResponse.from_binary(first)
    validate(response)
    assert response.result is not None
    assert response.result.field == "failure"
    assert str(project) in response.result.value.message
    assert invoke("start-run", request.to_binary()) == first
