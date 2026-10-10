"""Headless detached launch and the ``vibesys instances`` command."""

from __future__ import annotations

import json
import stat
from typing import TYPE_CHECKING

import pytest

from entrypoints.instances import main as instances_main
from entrypoints.instances import run
from entrypoints.launcher import _headless_requested
from entrypoints.server import (
    _DetachedGatewayEffects,
    _headless_argv,
    _spawn_detached_instance,
    main,
)
from server.instances import (
    FakeInstanceStore,
    FileInstanceStore,
    InstanceHold,
    InstanceList,
    InstanceStatus,
    InstanceStopResult,
    LiveInstanceRecord,
    LiveRegistry,
    StopOutcome,
)
from vs_sim.api.testing import FakeProcessSignaller, ManualClock

if TYPE_CHECKING:
    from pathlib import Path
    from typing import BinaryIO

INSTANCE = "0123456789ab"


def _record(instance_id: str, socket_path: str, status: InstanceStatus) -> LiveInstanceRecord:
    return LiveInstanceRecord(
        id=instance_id,
        status=status,
        socket_path=socket_path,
        project_root="/project",
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
    """Spawn a simulated detached child that registers itself as the real one does."""

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
        hold = FileInstanceStore(self.root).hold(instance_id)
        self.holds.append(hold)
        status = InstanceStatus.SERVING if self.serves else InstanceStatus.STARTING
        hold.publish(_record(instance_id, socket_path, status))
        return self.child

    def monotonic(self) -> float:
        return self.clock.now()

    def sleep(self, seconds: float) -> None:
        self.clock.advance(seconds)


def test_a_detached_launch_returns_the_record_once_the_server_is_serving(tmp_path: Path) -> None:
    effects = _ChildEffects(tmp_path, serves=True)

    record = _spawn_detached_instance(["--local"], tmp_path, effects)

    instance_id = effects.command[effects.command.index("--instance-id") + 1]
    socket_path = tmp_path / "runs" / instance_id / "control.sock"
    assert effects.command[1:4] == ["-m", "entrypoints.server", "--local"]
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
        _spawn_detached_instance(["--local"], tmp_path, effects)


def test_a_detached_launch_stops_a_child_that_never_serves(tmp_path: Path) -> None:
    effects = _ChildEffects(tmp_path, serves=False)

    with pytest.raises(RuntimeError, match="did not become ready"):
        _spawn_detached_instance(["--local"], tmp_path, effects)

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
    assert "drop --control-socket" in capsys.readouterr().err


def test_the_launcher_routes_detached_runs_and_instances_to_python() -> None:
    assert _headless_requested(["--detach"]) is True
    assert _headless_requested(["instances", "list"]) is True


# --- vibesys instances ----------------------------------------------------------


def _serving_store() -> tuple[FakeInstanceStore, InstanceHold]:
    store = FakeInstanceStore()
    hold = store.hold(INSTANCE)
    hold.publish(_record(INSTANCE, "/run/s.sock", InstanceStatus.SERVING))
    return store, hold


def _run(argv: list[str], store: FakeInstanceStore) -> tuple[int, str]:
    clock = ManualClock()
    return run(
        argv,
        registry=LiveRegistry(store),
        signaller=FakeProcessSignaller(set()),
        clock=clock,
        pause=clock.advance,
    )


def test_list_json_is_one_versioned_document() -> None:
    store, _ = _serving_store()

    code, output = _run(["list", "--json"], store)

    assert code == 0
    listing = InstanceList.model_validate_json(output)
    assert [record.id for record in listing.instances] == [INSTANCE]
    assert json.loads(output)["version"] == 1


def test_list_human_names_each_server_or_says_there_are_none() -> None:
    store, hold = _serving_store()

    assert INSTANCE in _run(["list"], store)[1]
    hold.release()
    assert _run(["list"], store)[1] == "No detached VibeSys servers are running on this node.\n"


def test_stop_json_reports_a_typed_outcome() -> None:
    store, _ = _serving_store()

    # The Fake signaller knows no live pid, so the server is reported, not signalled.
    code, output = _run(["stop", INSTANCE, "--json"], store)

    assert code == 1
    assert InstanceStopResult.model_validate_json(output).outcome is StopOutcome.NOT_RUNNING


@pytest.mark.parametrize("bad", ["../x", "ABCDEF012345", "short"])
def test_stop_rejects_an_id_that_could_name_a_path(
    bad: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        _run(["stop", bad], FakeInstanceStore())

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
