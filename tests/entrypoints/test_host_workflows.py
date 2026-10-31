"""Registry commands run through the public binary-stdio entrypoint in process."""

from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO
from typing import TYPE_CHECKING

import pytest
from tests.support.task_project import write_task

from entrypoints.host import _WATCH_SERVERS, main
from server.host_api import RUN_EVENTS_FILE
from vs_project.api import Project
from vs_proto.api import (
    MutateRequest,
    MutateResponse,
    Oneof,
    ReadRequest,
    ReadResponse,
    WatchRequest,
    WatchResponse,
    host_operations,
    validate,
)
from vs_proto.api import host_payloads as pb
from vs_sim.api.testing import FakeProcesses, FakeProcessSignaller, ProcessScript

if TYPE_CHECKING:
    from pathlib import Path

    from vs_sim.api.testing import Sim


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


def invoke(command: str, payload: bytes, sim: Sim) -> bytes:
    """Exercise dispatch, framing and the command with simulated scheduling."""
    threads = sim.threads()
    processes = FakeProcesses(lambda _: ProcessScript(), pass_time=threads.sleep)
    output = BytesIO()
    assert (
        main(
            [command],
            stdin=BytesIO(payload),
            stdout=output,
            platform=sim.platform(
                threads, processes=processes, signaller=FakeProcessSignaller(set())
            ),
        )
        == 0
    )
    assert not processes.ran
    return output.getvalue()


def test_registered_read_returns_the_filesystem_answer_in_its_envelope(
    host_root: Path, sim: Sim
) -> None:
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
    response = ReadResponse.from_binary(invoke("list-tasks", request.to_binary(), sim))
    validate(response)
    assert response.result is not None
    assert response.result.field == "list_tasks"
    assert response.result.value.tasks is not None
    assert [task.name for task in response.result.value.tasks.tasks] == ["alpha", "beta"]


def test_project_refusal_replays_without_reapplying_a_now_available_source(
    host_root: Path, sim: Sim
) -> None:
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
    first = invoke("copy-project", request.to_binary(), sim)
    response = MutateResponse.from_binary(first)
    validate(response)
    assert response.result is not None
    assert response.result.field == "failure"
    assert "no folder to copy out" in response.result.value.message
    source.mkdir()
    assert invoke("copy-project", request.to_binary(), sim) == first
    assert not destination.exists()


def test_cluster_refusal_is_typed_and_replayed(host_root: Path, sim: Sim) -> None:
    del host_root
    request = MutateRequest(
        checkout="/src/checkout",
        method=Oneof("remove_cluster", pb.RemoveClusterRequest(request_id="remove-a", name="lab")),
    )
    validate(request)
    first = invoke("remove-cluster", request.to_binary(), sim)
    response = MutateResponse.from_binary(first)
    validate(response)
    assert response.result is not None
    assert response.result.field == "failure"
    assert "no cluster named" in response.result.value.message
    assert invoke("remove-cluster", request.to_binary(), sim) == first


@pytest.mark.regression
@pytest.mark.parametrize("kind", ["missing", "file", "uninitialized"])
def test_start_refusal_is_typed_and_replayed_without_launching(
    host_root: Path, kind: str, sim: Sim
) -> None:
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
    first = invoke("start-run", request.to_binary(), sim)
    response = MutateResponse.from_binary(first)
    validate(response)
    assert response.result is not None
    assert response.result.field == "failure"
    assert str(project) in response.result.value.message
    assert invoke("start-run", request.to_binary(), sim) == first


@pytest.mark.parametrize("state", ["absent", "empty", "occupied"])
def test_destination_inspection_keeps_the_request_identity_and_filesystem_state(
    host_root: Path, sim: Sim, state: str
) -> None:
    destination = host_root / "destination"
    if state != "absent":
        destination.mkdir()
    if state == "occupied":
        (destination / "keep.txt").write_text("existing content")
    request = ReadRequest(
        checkout="/src/checkout",
        method=Oneof(
            "inspect_destination",
            pb.InspectDestinationRequest(
                request_id="destination-a", example="kvstore-default", destination=str(destination)
            ),
        ),
    )
    response = ReadResponse.from_binary(invoke("inspect-destination", request.to_binary(), sim))
    validate(response)
    assert response.result is not None
    assert response.result.field == "inspect_destination"
    assert response.result.value.request_id == "destination-a"
    assert (
        response.result.value.state
        == {
            "absent": pb.DestinationState.ABSENT,
            "empty": pb.DestinationState.EMPTY,
            "occupied": pb.DestinationState.OCCUPIED,
        }[state]
    )
    if state == "occupied":
        assert (destination / "keep.txt").read_text() == "existing content"


def test_candidate_inspection_is_a_registered_binary_read(host_root: Path, sim: Sim) -> None:
    request = ReadRequest(
        checkout="/src/checkout",
        method=Oneof(
            "inspect_candidate",
            pb.InspectCandidateRequest(
                request_id="candidate-a", path=str(host_root / "absent"), selection_revision=3
            ),
        ),
    )
    response = ReadResponse.from_binary(invoke("inspect-candidate", request.to_binary(), sim))
    validate(response)
    assert response.result is not None
    assert response.result.field == "inspect_candidate"
    assert response.result.value.request_id == "candidate-a"
    assert response.result.value.selection_revision == 3
    assert response.result.value.state == pb.CandidateState.NOT_FOUND


def _catalog_request(cli: str, project: Path) -> ReadRequest:
    """The request of one catalog read, over ``project``."""
    checkout = "/src/checkout"
    match cli:
        case "list-tasks":
            method = Oneof("list_tasks", pb.ListTasksRequest(request_id="r", project=str(project)))
        case "get-task-goal":
            method = Oneof(
                "get_task_goal",
                pb.GetTaskGoalRequest(request_id="r", project=str(project), task="alpha"),
            )
        case "list-harnesses":
            method = Oneof("list_harnesses", pb.ListHarnessesRequest(request_id="r"))
        case "list-examples":
            method = Oneof("list_examples", pb.ListExamplesRequest(request_id="r"))
        case "list-compute":
            method = Oneof("list_compute", pb.ListComputeRequest(request_id="r"))
        case _:
            method = Oneof(
                "inspect_destination",
                pb.InspectDestinationRequest(
                    request_id="r", example="kvstore-default", destination=str(project / "copy")
                ),
            )
    return ReadRequest(checkout=checkout, method=method)


@pytest.mark.parametrize(
    "cli",
    [
        "list-tasks",
        "get-task-goal",
        "list-harnesses",
        "list-examples",
        "list-compute",
        "inspect-destination",
    ],
)
def test_a_catalog_read_answers_without_starting_a_process(
    host_root: Path, sim: Sim, cli: str
) -> None:
    """The catalogs the app asks for at every launch answer from files; ``invoke`` fails the test
    if the read ran anything on the platform's processes (Git, a probe of the machine)."""
    project = host_root / "project"
    project.mkdir()
    Project.open(project).state.create_project("example", now=datetime(2026, 1, 1, tzinfo=UTC))
    write_task(project, "", task="alpha")
    request = _catalog_request(cli, project)
    validate(request)

    response = ReadResponse.from_binary(invoke(cli, request.to_binary(), sim))

    validate(response)
    assert response.result is not None
    assert response.result.field == cli.replace("-", "_")


@pytest.mark.regression
def test_native_cluster_probe_uses_the_injected_processes_and_replays_once(
    host_root: Path, sim: Sim
) -> None:
    del host_root
    threads = sim.threads()
    processes = FakeProcesses(
        lambda _: ProcessScript(returncode=255, stderr=b"probe refused"),
        pass_time=threads.sleep,
    )
    platform = sim.platform(threads, processes=processes, signaller=FakeProcessSignaller(set()))
    request = MutateRequest(
        checkout="/src/checkout",
        method=Oneof(
            "test_cluster",
            pb.TestClusterRequest(
                request_id="cluster-probe-a",
                cluster=pb.SlurmClusterSettings(
                    name="lab",
                    workspace="/work",
                    reach=pb.SlurmClusterSettingsReach(
                        value=Oneof("ssh_reach", pb.SshReach(kind="ssh", host="host-a"))
                    ),
                ),
            ),
        ),
    )
    validate(request)
    replies = []
    for _ in range(2):
        output = BytesIO()
        assert (
            main(
                ["test-cluster"],
                stdin=BytesIO(request.to_binary()),
                stdout=output,
                platform=platform,
            )
            == 0
        )
        replies.append(output.getvalue())
    assert replies[0] == replies[1]
    response = MutateResponse.from_binary(replies[0])
    validate(response)
    assert response.result is not None
    assert response.result.field == "test_cluster"
    result = response.result.value.result
    assert result is not None
    assert result.field == "failed"
    assert result.value.stage is pb.ClusterTestStage.REACH
    assert result.value.detail == "probe refused"
    assert len(processes.ran) == 1
    assert processes.ran[0].argv[0] == "ssh"


def test_native_cluster_put_and_remove_persist_without_the_helper(
    host_root: Path, sim: Sim
) -> None:
    del host_root
    threads = sim.threads()
    processes = FakeProcesses(
        lambda spec: ProcessScript(
            stdout=b"cpu|1|(null)\n" if any("sinfo" in arg for arg in spec.argv) else b""
        ),
        pass_time=threads.sleep,
    )
    platform = sim.platform(threads, processes=processes, signaller=FakeProcessSignaller(set()))
    put = MutateRequest(
        checkout="/src/checkout",
        method=Oneof(
            "put_cluster",
            pb.PutClusterRequest(
                request_id="cluster-put-a",
                cluster=pb.SlurmClusterSettings(
                    name="lab",
                    workspace="/work",
                    reach=pb.SlurmClusterSettingsReach(
                        value=Oneof("ssh_reach", pb.SshReach(kind="ssh", host="host-a"))
                    ),
                ),
            ),
        ),
    )
    output = BytesIO()
    assert (
        main(["put-cluster"], stdin=BytesIO(put.to_binary()), stdout=output, platform=platform) == 0
    )
    response = MutateResponse.from_binary(output.getvalue())
    validate(response)
    assert response.result is not None
    assert response.result.field == "put_cluster"
    settings = response.result.value.settings
    assert settings is not None
    assert len(settings.clusters) == 1
    saved = settings.clusters[0].cluster
    assert saved is not None
    assert saved.name == "lab"
    assert len(processes.ran) == 3
    remove = MutateRequest(
        checkout="/src/checkout",
        method=Oneof(
            "remove_cluster", pb.RemoveClusterRequest(request_id="cluster-remove-a", name="lab")
        ),
    )
    output = BytesIO()
    assert (
        main(
            ["remove-cluster"], stdin=BytesIO(remove.to_binary()), stdout=output, platform=platform
        )
        == 0
    )
    response = MutateResponse.from_binary(output.getvalue())
    validate(response)
    assert response.result is not None
    assert response.result.field == "remove_cluster"
    settings = response.result.value.settings
    assert settings is not None
    assert list(settings.clusters) == []
    assert len(processes.ran) == 3


class _FlushCounter(BytesIO):
    """A stdout that counts flushes, since the gateway's progress bound sees only what was flushed."""

    def __init__(self) -> None:
        super().__init__()
        self.flushes = 0

    def flush(self) -> None:
        self.flushes += 1
        super().flush()


def test_the_run_listing_streams_one_flushed_frame_per_batch_and_ends_complete(
    host_root: Path, sim: Sim
) -> None:
    folder = host_root / "projects"
    folder.mkdir()
    Project.open(folder).state.create_project("example", now=datetime(2026, 1, 1, tzinfo=UTC))
    for index in range(450):
        journal = Project.log_directory_for(folder.resolve(), f"run-{index:04d}") / RUN_EVENTS_FILE
        journal.parent.mkdir(parents=True)
        journal.write_text('{"sequence": 1, "timestamp": "2026-10-01T00:00:00+00:00"}\n')
    request = WatchRequest(
        checkout="/src/checkout",
        method=Oneof(
            "stream_saved_runs",
            pb.StreamSavedRunsRequest(request_id="listing-a", project=str(folder)),
        ),
    )
    validate(request)
    threads = sim.threads()
    output = _FlushCounter()

    assert (
        main(
            ["stream-saved-runs"],
            stdin=BytesIO(request.to_binary()),
            stdout=output,
            platform=sim.platform(
                threads,
                processes=FakeProcesses(lambda _: ProcessScript(), pass_time=threads.sleep),
                signaller=FakeProcessSignaller(set()),
            ),
        )
        == 0
    )

    written, frames = output.getvalue(), []
    while written:
        length = int.from_bytes(written[:4], "big")
        frames.append(WatchResponse.from_binary(written[4 : 4 + length]))
        written = written[4 + length :]
    results = [frame.result.value for frame in frames]
    assert [len(result.runs) for result in results] == [200, 200, 50]
    assert [result.complete for result in results] == [False, False, True]
    assert output.flushes >= len(frames)
    assert len({row.run_id for result in results for row in result.runs}) == 450


def test_every_registered_watch_has_a_command_to_run() -> None:
    assert set(_WATCH_SERVERS) == set(host_operations.WATCHES)
