"""Headless detached launch and the ``vibesys instances`` command."""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.support.task_project import write_task

from entrypoints import launcher
from entrypoints.detached_run import SERVER_EXITED, RecordPublisher
from entrypoints.detached_run import main as detached_run_main
from entrypoints.instances import main as instances_main
from entrypoints.instances import run
from entrypoints.launcher import _headless_requested
from entrypoints.server import (
    _DetachedGatewayEffects,
    _headless_argv,
    _launch_detached_instance,
    _spawn_detached_instance,
    main,
)
from server.host_api import (
    DetachedLaunchErrorCode,
    DetachedLaunchFailure,
    FailureReason,
    InstanceInspection,
    InstanceInspectionMetadata,
    InstanceList,
    InstanceLogTail,
    InstanceStatus,
    InstanceStopResult,
    LiveInstanceRecord,
    RunPhase,
    RunStatus,
    StartupStage,
    StopOutcome,
)
from server.instances import ControlSocketStopRequester
from server.registry import (
    TOMBSTONE_RETENTION_SECONDS,
    FakeInstanceLogs,
    FakeInstanceMetadataStore,
    FakeInstanceStore,
    FileInstanceLogs,
    FileInstanceMetadataStore,
    FileInstanceStore,
    InstanceHold,
    InstanceInspector,
    InstanceLogError,
    LiveRegistry,
    StopEffects,
    driving,
    instance_root,
)
from vibesys.api import CoreEvent, RunRequest, RunResult
from vibesys.api.testing import FakeRuns
from vibesys.orchestration.dynamic.launch import DynamicLaunch
from vs_project.api import OrchestrationDescriptor, Project, RunEnvironmentRecord
from vs_project.api.testing import run_execution_record
from vs_sim.api.testing import (
    FakeForegroundLauncher,
    FakeProcessSignaller,
    ForegroundScript,
    ManualClock,
    SimNetwork,
    SimThreads,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import BinaryIO

    from vibesys.api import Runs, RunView
    from vibesys.api.auxiliary import RunReady

INSTANCE = "0123456789ab"
_RUN_ID = "20261010-120000-11111111-dynamic"


class _FinishedSession:
    """In-memory run session that completes without emitting observations."""

    def __init__(self, run_id: str) -> None:
        self._run_id = run_id

    def start(self) -> None:
        pass

    async def await_result(self) -> RunResult:
        return RunResult(run_id=self._run_id, loop="plain", succeeded=True)

    def stop(self) -> None:
        pass

    def close(self) -> None:
        pass

    def on_committed_view(
        self, listener: Callable[[RunView, tuple[str, ...] | None], None]
    ) -> None:
        del listener

    def on_ready(self, listener: Callable[[RunReady], None]) -> None:
        del listener


def _finished_runs() -> Runs:
    runs = FakeRuns[RunRequest, CoreEvent, RunResult, _FinishedSession](
        lambda request, _sink: _FinishedSession(request.resolved_run_id),
        identity=lambda request: request.resolved_run_id,
        is_resume=lambda request: request.resume is not None,
    )
    return cast("Runs", runs)


def _record(
    instance_id: str, socket_path: str, status: InstanceStatus, run_id: str = _RUN_ID
) -> LiveInstanceRecord:
    return LiveInstanceRecord(
        id=instance_id,
        status=status,
        phase=RunPhase(status=RunStatus.RUNNING),
        socket_path=socket_path,
        log_path="/run/server.log",
        project_root="/project",
        run_id=run_id,
        pid=4242,
        started_at=1.0,
        hostname="node",
        vibesys_version="0+test",
    )


class _Child:
    def __init__(self, status: int | None) -> None:
        self.status = status
        self.terminated = False

    def poll(self) -> int | None:
        return self.status

    def terminate(self) -> None:
        self.terminated = True
        self.status = -15

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return self.status or 0

    def kill(self) -> None:
        self.status = -9


class _ChildEffects(_DetachedGatewayEffects):
    """Spawn a simulated detached child that registers itself as the real one does.

    Like ``entrypoints.detached_run``, it names the run its launcher allocated
    (``--run-id``) in the first record it publishes.
    """

    def __init__(self, root: Path, *, serves: bool, exit_status: int | None = None) -> None:
        self.root = root
        self.serves = serves
        self.child = _Child(exit_status)
        self.command: list[str] = []
        self.environment: dict[str, str] = {}
        self.holds: list[InstanceHold] = []
        self.clock = ManualClock()

    def spawn(self, command: list[str], environment: dict[str, str], output: BinaryIO) -> _Child:
        self.command = command
        self.environment = environment
        output.write(b"child output\n")
        output.flush()
        instance_id = command[command.index("--instance-id") + 1]
        socket_path = command[command.index("--control-socket") + 1]
        run_id = command[command.index("--run-id") + 1]
        hold = FileInstanceStore(self.root).hold(instance_id)
        self.holds.append(hold)
        status = InstanceStatus.SERVING if self.serves else InstanceStatus.STARTING
        hold.publish(_record(instance_id, socket_path, status, run_id))
        return self.child

    def monotonic(self) -> float:
        return self.clock.now()

    def sleep(self, seconds: float) -> None:
        self.clock.advance(seconds)


def test_a_detached_launch_returns_the_record_once_the_server_is_serving(tmp_path: Path) -> None:
    effects = _ChildEffects(tmp_path, serves=True)

    record = _spawn_detached_instance(["--local", "--run-id", _RUN_ID], tmp_path, effects)

    instance_id = effects.command[effects.command.index("--instance-id") + 1]
    socket_path = tmp_path / "runs" / instance_id / "control.sock"
    assert effects.command[1:6] == [
        "-m",
        "entrypoints.detached_run",
        "--local",
        "--run-id",
        _RUN_ID,
    ]
    assert effects.command[-4:] == [
        "--control-socket",
        str(socket_path),
        "--instance-id",
        instance_id,
    ]
    assert effects.environment["VIBESYS_DETACHED_CHILD"] == "1"
    assert record.id == instance_id
    assert record.socket_path == str(socket_path)
    assert record.status is InstanceStatus.SERVING
    log_path = socket_path.with_name("server.log")
    assert log_path.read_text() == "child output\n"
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(log_path.parent.stat().st_mode) == 0o700
    assert effects.child.terminated is False
    effects.holds[0].release()


def test_a_detached_launch_reports_a_child_that_exits_before_serving(tmp_path: Path) -> None:
    effects = _ChildEffects(tmp_path, serves=False, exit_status=3)

    with pytest.raises(RuntimeError, match=r"(?s)child output.*exited with status 3"):
        _spawn_detached_instance(["--local", "--run-id", _RUN_ID], tmp_path, effects)


def test_a_detached_launch_stops_a_child_that_never_serves(tmp_path: Path) -> None:
    effects = _ChildEffects(tmp_path, serves=False)

    with pytest.raises(RuntimeError, match="did not become ready"):
        _spawn_detached_instance(["--local", "--run-id", _RUN_ID], tmp_path, effects)

    assert effects.child.terminated is True
    effects.holds[0].release()


def test_detached_server_flags_never_reach_run_parsing() -> None:
    assert _headless_argv(["--detach", "--instance-id", INSTANCE, "--local"]) == ["--local"]
    assert _headless_argv([f"--instance-id={INSTANCE}", "--local"]) == ["--local"]


def test_a_detached_launch_chooses_its_own_control_socket(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--detach", "--control-socket", str(tmp_path / "control.sock")])

    assert exit_info.value.code == 2
    output = capsys.readouterr()
    assert "drop --control-socket" in output.err
    failure = DetachedLaunchFailure.model_validate_json(output.out)
    assert (failure.code, failure.exit_code) == ("invalid_arguments", 2)


# --- vibesys --detach --resume ---------------------------------------------------

_RESUME = ["--detach", "--outer-loop", "dynamic", "--resume", _RUN_ID]


def _project_with_run(root: Path) -> Path:
    """A project holding one recorded, stopped run that ``--resume`` resolves."""
    root.mkdir()
    write_task(
        root,
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n[benchmark]\ncommand = ["true"]\n',
        objective="Make the queue faster.\n",
    )
    project = Project.open(root)
    project.state.create_project(root.name, now=datetime(2026, 1, 1, tzinfo=UTC))
    manifest = project.state.new_run_manifest(
        root.name,
        run_id=_RUN_ID,
        branch=f"vibesys-runs/{_RUN_ID}",
        vibesys_version="0.2.0-test",
        run_environment=RunEnvironmentRecord(name="docker"),
        execution=run_execution_record(),
        orchestration=OrchestrationDescriptor(
            id="dynamic",
            config_version=1,
            options=DynamicLaunch(interface="service", max_rounds=2, judge_every=2).model_dump(
                # A run records its options as the operator wrote them: profiling follows the plan.
                mode="json",
                exclude={"profiling"},
            ),
        ),
        trusted_input_baseline="0" * 40,
        now=datetime(2026, 10, 10, 12, tzinfo=UTC),
    )
    project.state.create_run(manifest, make_current=True)
    return root


@pytest.fixture
def resumable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run from a project holding a stopped run; return this user's registry root."""
    monkeypatch.chdir(_project_with_run(tmp_path / "project"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    return instance_root(os.environ, os.getuid())


def test_a_detached_resume_prints_the_new_servers_record(
    resumable: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    effects = _ChildEffects(resumable, serves=True)

    _launch_detached_instance(_RESUME, effects)

    record = LiveInstanceRecord.model_validate_json(capsys.readouterr().out)
    assert record.status is InstanceStatus.SERVING
    assert record.run_id == _RUN_ID
    # The child runs this process's interpreter, so it resumes on the invoked code.
    assert effects.command[1:3] == ["-m", "entrypoints.detached_run"]
    assert effects.command[3 : 5 + len(_RESUME)] == [*_RESUME, "--run-id", _RUN_ID]
    effects.holds[0].release()


def test_a_detached_resume_of_the_latest_run_names_the_run_it_resolved(
    resumable: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    effects = _ChildEffects(resumable, serves=True)

    _launch_detached_instance(["--detach", "--outer-loop", "dynamic", "--resume"], effects)

    record = LiveInstanceRecord.model_validate_json(capsys.readouterr().out)
    assert record.run_id == _RUN_ID
    assert effects.command[-6:-4] == ["--run-id", _RUN_ID]
    effects.holds[0].release()


def test_a_fresh_detached_run_is_named_before_its_server_starts(
    resumable: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    effects = _ChildEffects(resumable, serves=True)

    _launch_detached_instance(["--detach", "--outer-loop", "dynamic", "--local"], effects)

    record = LiveInstanceRecord.model_validate_json(capsys.readouterr().out)
    allocated = effects.command[effects.command.index("--run-id") + 1]
    assert record.run_id == allocated
    assert allocated != _RUN_ID
    assert Project.open(resumable.parent / "project").state.load_run(_RUN_ID) is not None
    effects.holds[0].release()


def test_a_detached_resume_refuses_a_run_another_server_is_driving(
    resumable: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    live = FileInstanceStore(resumable).hold(INSTANCE)
    live.publish(
        _record(INSTANCE, "/run/s.sock", InstanceStatus.SERVING).model_copy(
            update={"run_id": _RUN_ID}
        )
    )
    effects = _ChildEffects(resumable, serves=True)

    with pytest.raises(SystemExit) as exit_info:
        _launch_detached_instance(_RESUME, effects)

    failure = DetachedLaunchFailure.model_validate_json(capsys.readouterr().out)
    assert exit_info.value.code == failure.exit_code == 1
    assert failure.code == DetachedLaunchErrorCode.RUN_ALREADY_LIVE
    assert failure.live_instance is not None
    assert failure.live_instance.id == INSTANCE
    assert effects.command == []
    live.release()


def test_a_detached_resume_whose_server_dies_names_its_log(
    resumable: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    effects = _ChildEffects(resumable, serves=False, exit_status=3)

    with pytest.raises(SystemExit) as exit_info:
        _launch_detached_instance(_RESUME, effects)

    failure = DetachedLaunchFailure.model_validate_json(capsys.readouterr().out)
    assert exit_info.value.code == failure.exit_code == 1
    assert failure.code == DetachedLaunchErrorCode.SERVER_START_FAILED
    assert failure.log_path is not None
    assert Path(failure.log_path).read_text() == "child output\n"
    effects.holds[0].release()


def test_a_detached_resume_of_an_unknown_run_starts_nothing(
    resumable: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    effects = _ChildEffects(resumable, serves=True)

    with pytest.raises(SystemExit) as exit_info:
        _launch_detached_instance(["--detach", "--resume", "no-such-run"], effects)

    failure = DetachedLaunchFailure.model_validate_json(capsys.readouterr().out)
    assert (failure.code, failure.stage) == ("resume_not_found", "resume_resolution")
    assert exit_info.value.code == failure.exit_code
    assert effects.command == []


@given(
    run_ids=st.lists(st.sampled_from(["a", "b", "c"]), max_size=5),
    wanted=st.sampled_from(["a", "b", "c"]),
)
def test_driving_finds_exactly_a_live_server_with_that_run(run_ids: list[str], wanted: str) -> None:
    records = tuple(
        _record(f"{index:012x}", "/s", InstanceStatus.SERVING, run_id)
        for index, run_id in enumerate(run_ids)
    )

    found = driving(InstanceList(instances=records), wanted)

    assert (found is not None) == (wanted in run_ids)
    assert found is None or found.run_id == wanted


def test_the_launcher_routes_detached_runs_to_python() -> None:
    assert _headless_requested(["--detach"]) is True


def test_the_launcher_routes_input_checks_to_python() -> None:
    assert _headless_requested(["check-input"]) is True


def test_the_launcher_lists_instances_in_process_without_a_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    children = FakeForegroundLauncher(lambda _argv: ForegroundScript(exits_immediately=True))

    code = launcher.main(["instances", "list", "--json"], launcher.LauncherHost(children=children))

    assert code == 0
    assert children.children == []
    assert InstanceList.model_validate_json(capsys.readouterr().out) == InstanceList(instances=())


# --- vibesys instances ----------------------------------------------------------


def _serving_store() -> tuple[FakeInstanceStore, InstanceHold]:
    store = FakeInstanceStore()
    hold = store.hold(INSTANCE)
    hold.publish(_record(INSTANCE, "/run/s.sock", InstanceStatus.SERVING))
    return store, hold


def _run(
    argv: list[str],
    store: FakeInstanceStore,
    *,
    logs: FakeInstanceLogs | None = None,
    metadata: FakeInstanceMetadataStore | None = None,
) -> tuple[int, str]:
    clock = ManualClock()
    return run(
        argv,
        registry=LiveRegistry(store, ManualClock()),
        inspector=InstanceInspector(
            store,
            logs if logs is not None else FakeInstanceLogs(),
            metadata if metadata is not None else FakeInstanceMetadataStore(),
            ManualClock(),
        ),
        # Nothing listens on the simulated network, so every stop falls back
        # to the signal path, as it does for a server that does not answer.
        effects=StopEffects(
            ControlSocketStopRequester(SimNetwork(SimThreads()), clock=ManualClock()),
            FakeProcessSignaller(set()),
            clock,
            clock.advance,
        ),
    )


def _inspection_metadata() -> InstanceInspectionMetadata:
    return InstanceInspectionMetadata(
        id=INSTANCE,
        project_root="/project",
        run_id=_RUN_ID,
        task="queue",
        pid=4242,
        started_at=1.0,
        hostname="node",
        vibesys_version="0+test",
        vibesys_root="/src/vibesys",
        control_socket_path="/run/control.sock",
        server_log_path="/run/server.log",
    )


def test_list_json_is_one_versioned_document() -> None:
    store, _ = _serving_store()

    code, output = _run(["list", "--json"], store)

    assert code == 0
    listing = InstanceList.model_validate_json(output)
    assert [record.id for record in listing.instances] == [INSTANCE]
    assert json.loads(output)["version"] == 4


def test_list_human_names_each_server_or_says_there_are_none() -> None:
    store, hold = _serving_store()

    assert INSTANCE in _run(["list"], store)[1]
    hold.release()
    assert f"{INSTANCE}  failed (ended): " in _run(["list"], store)[1]
    assert _run(["list"], FakeInstanceStore())[1] == (
        "No detached VibeSys servers are running on this node.\n"
    )


def test_inspect_json_reports_a_live_server_without_reading_its_log() -> None:
    store, _ = _serving_store()

    class RefusingLogs(FakeInstanceLogs):
        def tail(self, instance_id: str) -> InstanceLogTail:
            del instance_id
            raise AssertionError

    code, output = _run(["inspect", INSTANCE, "--json"], store, logs=RefusingLogs())

    inspection = InstanceInspection.model_validate_json(output)
    assert code == 0
    assert inspection.state.value == "live"
    assert inspection.record is not None
    assert inspection.log is None


def test_inspect_human_names_retained_ended_identity_and_paths() -> None:
    metadata = FakeInstanceMetadataStore()
    metadata.publish(_inspection_metadata())

    code, output = _run(["inspect", INSTANCE], FakeInstanceStore(), metadata=metadata)

    assert code == 0
    assert "State: ended" in output
    assert "Hostname: node" in output
    assert "Task: queue" in output
    assert f"Run: {_RUN_ID}" in output
    assert "Project: /project" in output
    assert "Checkout: /src/vibesys" in output
    assert "Control socket: /run/control.sock" in output
    assert "Server log: /run/server.log" in output
    assert "Diagnostics: not loaded (pass --include-log)" in output


def test_inspect_explicit_log_consent_reports_missing_and_bounded_diagnostics() -> None:
    store, _ = _serving_store()

    missing = InstanceInspection.model_validate_json(
        _run(["inspect", INSTANCE, "--include-log", "--json"], store)[1]
    )
    assert missing.log is not None
    assert missing.log.state == "missing"

    logs = FakeInstanceLogs(limit=5)
    logs.contents[INSTANCE] = b"0123456789"
    available = InstanceInspection.model_validate_json(
        _run(["inspect", INSTANCE, "--include-log", "--json"], store, logs=logs)[1]
    )
    assert available.log is not None
    assert available.log.state == "available"
    assert available.log.text == "56789"
    assert available.log.truncated is True


def test_inspect_surfaces_an_unsafe_log_without_returning_partial_output() -> None:
    store, _ = _serving_store()

    class UnsafeLogs(FakeInstanceLogs):
        def tail(self, instance_id: str) -> InstanceLogTail:
            raise InstanceLogError.unsafe(Path(f"/run/{instance_id}/server.log"), "symbolic link")

    with pytest.raises(InstanceLogError, match="refusing to inspect symbolic link"):
        _run(["inspect", INSTANCE, "--include-log", "--json"], store, logs=UnsafeLogs())


def test_inspect_cli_refuses_an_unsafe_log_without_printing_partial_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    root = instance_root(os.environ, os.getuid())
    run_directory = root / "runs" / INSTANCE
    run_directory.mkdir(parents=True)
    outside = tmp_path / "outside.log"
    outside.write_text("secret\n")
    (run_directory / "server.log").symlink_to(outside)

    assert instances_main(["inspect", INSTANCE, "--include-log", "--json"]) == 1

    output = capsys.readouterr()
    assert output.out == ""
    assert "cannot safely inspect" in output.err
    assert "secret" not in output.err


def test_stop_json_reports_a_typed_outcome() -> None:
    store, _ = _serving_store()

    # The Fake signaller knows no live pid, so the server is reported, not signalled.
    code, output = _run(["stop", INSTANCE, "--json"], store)

    assert code == 1
    assert InstanceStopResult.model_validate_json(output).outcome is StopOutcome.NOT_RUNNING


@pytest.mark.parametrize("command", ["inspect", "stop"])
@pytest.mark.parametrize("bad", ["../x", "ABCDEF012345", "short"])
def test_instance_actions_reject_an_id_that_could_name_a_path(
    command: str, bad: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        _run([command, bad], FakeInstanceStore())

    assert exit_info.value.code == 2
    assert "must match" in capsys.readouterr().err


def test_the_command_reads_this_users_runtime_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert instances_main(["list", "--json"]) == 0
    assert InstanceList.model_validate_json(capsys.readouterr().out).instances == ()
    assert stat.S_IMODE((tmp_path / "vibesys").stat().st_mode) == 0o700
    assert [entry.name for entry in (tmp_path / "vibesys").iterdir()] == ["instances"]


def _starting(instance_id: str) -> LiveInstanceRecord:
    return _record(instance_id, "/run/s.sock", InstanceStatus.STARTING).model_copy(
        update={"phase": RunPhase(status=RunStatus.STARTING, stage=StartupStage.RESOLVING)}
    )


def test_a_detached_server_record_follows_its_run_and_names_it_throughout() -> None:
    clock = ManualClock(100.0)
    registry = LiveRegistry(FakeInstanceStore(), clock)
    phases: list[tuple[InstanceStatus, RunPhase, str]] = []

    def seen() -> None:
        record = registry.find(INSTANCE)
        assert record is not None
        phases.append((record.status, record.phase, record.run_id))

    with registry.register(INSTANCE) as hold:
        publisher = RecordPublisher(
            hold,
            _starting(INSTANCE),
            clock,
            metadata=FakeInstanceMetadataStore(),
            threads=SimThreads(),
        )
        seen()
        publisher.listening()
        seen()
        publisher.observe(status=RunStatus.RUNNING)
        seen()
        publisher.observe(status=RunStatus.STOPPING)
        publisher.observe(status=RunStatus.STOPPED)
        publisher.end()

    assert phases == [
        (
            InstanceStatus.STARTING,
            RunPhase(status=RunStatus.STARTING, stage=StartupStage.RESOLVING),
            _RUN_ID,
        ),
        (
            InstanceStatus.SERVING,
            RunPhase(status=RunStatus.STARTING, stage=StartupStage.RESOLVING),
            _RUN_ID,
        ),
        (InstanceStatus.SERVING, RunPhase(status=RunStatus.RUNNING), _RUN_ID),
    ]
    (ended,) = registry.list().ended
    assert (ended.phase, ended.ended_at, ended.run_id) == (
        RunPhase(status=RunStatus.STOPPED),
        100.0,
        _RUN_ID,
    )


def test_resolved_project_replaces_the_launch_directory_before_listening(tmp_path: Path) -> None:
    """A live inspection sees the parsed project in both identity contracts."""
    launch_directory = tmp_path / "launcher-cwd"
    project = tmp_path / "selected-project"
    launch_directory.mkdir()
    project.mkdir()
    store = FakeInstanceStore()
    metadata = FakeInstanceMetadataStore()
    clock = ManualClock()
    registry = LiveRegistry(store, clock)
    starting = _starting(INSTANCE).model_copy(
        update={"project_root": str(launch_directory.resolve())}
    )

    with registry.register(INSTANCE) as hold:
        publisher = RecordPublisher(hold, starting, clock, metadata=metadata, threads=SimThreads())
        publisher.resolved(project_root=project, task="latency")
        publisher.listening()

        inspection = InstanceInspector(store, FakeInstanceLogs(), metadata, ManualClock()).inspect(
            INSTANCE
        )
        assert inspection.state.value == "live"
        assert inspection.record is not None
        assert inspection.metadata is not None
        assert inspection.record.project_root == str(project.resolve())
        assert inspection.metadata.project_root == str(project.resolve())
        publisher.end()


_EARLY_FAILURE = FailureReason(
    code="sandbox_start_failed",
    stage="run",
    message="docker run --gpus all: could not select device driver",
)


def test_a_run_that_fails_at_startup_shows_its_reason_to_a_client_that_comes_later() -> None:
    # Supersedes holding a failed server open for a first client (#1826): the
    # reason lives in the tombstone, so the server exits at once and a client
    # that lists the host afterwards still reads why the run ended.
    clock = ManualClock(100.0)
    registry = LiveRegistry(FakeInstanceStore(), clock)
    with registry.register(INSTANCE) as hold:
        publisher = RecordPublisher(
            hold,
            _starting(INSTANCE),
            clock,
            metadata=FakeInstanceMetadataStore(),
            threads=SimThreads(),
        )
        publisher.listening()
        publisher.observe(status=RunStatus.FAILED)
        publisher.observe(failure=_EARLY_FAILURE)
        publisher.end()

    clock.advance(TOMBSTONE_RETENTION_SECONDS - 1)
    listing = registry.list()
    assert listing.instances == ()
    (ended,) = listing.ended
    assert ended.phase == RunPhase(status=RunStatus.FAILED, reason=_EARLY_FAILURE)
    assert ended.log_path == "/run/server.log"

    clock.advance(1)
    assert registry.list() == InstanceList(instances=())


def test_a_server_that_exits_before_its_run_ends_leaves_a_failed_tombstone() -> None:
    clock = ManualClock(100.0)
    registry = LiveRegistry(FakeInstanceStore(), clock)
    with registry.register(INSTANCE) as hold:
        RecordPublisher(
            hold,
            _starting(INSTANCE),
            clock,
            metadata=FakeInstanceMetadataStore(),
            threads=SimThreads(),
        ).end()

    (ended,) = registry.list().ended
    assert ended.phase == RunPhase(status=RunStatus.FAILED, reason=SERVER_EXITED)


@pytest.mark.parametrize("missing", ["--instance-id", "--run-id", "--control-socket"])
def test_a_detached_server_without_its_allocated_identity_registers_nothing(
    missing: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    identity = {
        "--instance-id": INSTANCE,
        "--run-id": _RUN_ID,
        "--control-socket": str(tmp_path / "control.sock"),
    }
    del identity[missing]

    with pytest.raises(SystemExit) as exit_info:
        detached_run_main([token for pair in identity.items() for token in pair])

    assert exit_info.value.code == 2
    assert not (tmp_path / "vibesys").exists()


def test_detached_start_retains_resolved_identity_for_later_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The child retains the sole task that server parsing selected automatically."""
    launch_directory = tmp_path / "launcher-cwd"
    launch_directory.mkdir()
    project = tmp_path / "resolved-project"
    project.mkdir()
    write_task(
        project,
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n[benchmark]\ncommand = ["true"]\n',
        task="latency",
    )
    monkeypatch.chdir(launch_directory)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("VIBESYS_DETACHED_CHILD", "1")
    root = instance_root(os.environ, os.getuid())
    (root / "runs" / INSTANCE).mkdir(parents=True, mode=0o700)
    socket = tmp_path / "control.sock"

    detached_run_main(
        [
            "--detach",
            "--instance-id",
            INSTANCE,
            "--run-id",
            _RUN_ID,
            "--control-socket",
            str(socket),
            "--outer-loop",
            "plain",
            "--project",
            str(project),
        ],
        runs=_finished_runs(),
    )

    inspection = InstanceInspector(
        FileInstanceStore(root),
        FileInstanceLogs(root),
        FileInstanceMetadataStore(root),
        ManualClock(),
    ).inspect(INSTANCE)
    assert inspection.state.value == "ended"
    assert inspection.record is None
    assert inspection.metadata is not None
    assert inspection.metadata.project_root == str(project)
    assert inspection.metadata.task == "latency"
    assert inspection.metadata.run_id == _RUN_ID
    assert inspection.metadata.control_socket_path == str(socket)
