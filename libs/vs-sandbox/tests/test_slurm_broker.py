"""Public contract tests for host-owned Slurm transport access."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from vs_sandbox.api.slurm import BrokeredProcesses, SlurmProcessBroker

# test-isolation: peer-abandoned reply containment is a private handler boundary.
from vs_sandbox.slurm_broker import _write_reply

# test-isolation: this pins the exact shared broker wire envelope around the public transport cap.
from vs_sandbox.slurm_broker_protocol import MAX_FRAME_BYTES, BrokerRan, ProcessReply
from vs_sim.api import (
    OsProcesses,
    ProcessOutcome,
    ProcessOutputTooLarge,
    ProcessSpec,
    UnixNetwork,
)
from vs_sim.api.testing import FakeProcesses, ManualClock, ProcessScript
from vs_slurm.api import (
    MAX_TRANSPORT_STREAM_BYTES,
    SlurmConfig,
    SlurmConnectorTransport,
    SlurmLocalTransport,
    SlurmSshTransport,
    slurm_transport_broker_available,
    slurm_transport_broker_policy,
)


def _config() -> SlurmConfig:
    return SlurmConfig(
        name="cluster",
        remote_workspace_root="/remote/vibesys",
        transport=SlurmSshTransport(host="cluster", ssh_command=("/usr/bin/printf",)),
        transport_timeout_seconds=5,
    )


def _brokered(
    broker: SlurmProcessBroker, argv: tuple[str, ...], stdin: bytes = b"", timeout: float = 5
) -> ProcessOutcome:
    processes = BrokeredProcesses(broker.socket_path, broker.token, network=UnixNetwork())
    run = processes.run(ProcessSpec(argv, input=stdin), timeout=timeout)
    assert isinstance(run, ProcessOutcome)
    return run


_CONNECTOR_COMMAND = ("site-connector", "--configured")
_ARCHIVE_TIME_NS = 1_700_000_000_123_456_789


def _connector_config() -> SlurmConfig:
    return SlurmConfig(
        name="cluster",
        remote_workspace_root="/remote/vibesys",
        transport=SlurmConnectorTransport(kind="connector", command=_CONNECTOR_COMMAND),
        transport_timeout_seconds=5,
    )


def _connector_request(**updates: object) -> bytes:
    request: dict[str, object] = {
        "version": 1,
        "operation": "exec",
        "command": "hostname",
    }
    for key, value in updates.items():
        if value is None:
            request.pop(key, None)
        else:
            request[key] = value
    return json.dumps(request).encode()


@pytest.mark.parametrize("error", [BrokenPipeError(), ConnectionResetError()])
def test_broker_contains_a_client_disconnect_while_writing_its_reply(error: OSError) -> None:
    class DisconnectedWriter:
        def write(self, _payload: bytes) -> int:
            raise error

    _write_reply(
        DisconnectedWriter(),
        BrokerRan(ok=True, result=ProcessReply(returncode=0, stdout="", stderr="")),
    )


def test_broker_executes_only_the_configured_destination(tmp_path: Path) -> None:
    broker = SlurmProcessBroker(
        slurm_transport_broker_policy(_config(), local_roots=(tmp_path,)),
        tmp_path / "broker.sock",
        processes=OsProcesses(),
    )
    broker.start()
    try:
        result = _brokered(broker, ("/usr/bin/printf", "--", "cluster", "hostname"))
        with pytest.raises(PermissionError, match="unauthorized destination"):
            _brokered(broker, ("/usr/bin/printf", "--", "other-cluster", "hostname"))
    finally:
        broker.close()

    assert result.returncode == 0
    assert result.stdout == b"cluster"
    assert not broker.socket_path.exists()


def test_broker_rejects_rsync_paths_outside_run_roots(tmp_path: Path) -> None:
    config = _config().model_copy(
        update={
            "transport": SlurmSshTransport(
                host="cluster",
                ssh_command=("ssh",),
                rsync_command=("rsync",),
            )
        }
    )
    broker = SlurmProcessBroker(
        slurm_transport_broker_policy(config, local_roots=(tmp_path,)),
        tmp_path / "unused.sock",
        processes=OsProcesses(),
    )
    broker.start()
    try:
        with pytest.raises(PermissionError, match="run-owned roots"):
            _brokered(
                broker,
                (
                    "rsync",
                    "-a",
                    "-e",
                    "ssh",
                    "--",
                    "/etc/passwd",
                    "cluster:/remote/vibesys/cluster/workspace/passwd",
                ),
            )
    finally:
        broker.close()


def test_a_brokered_process_is_outside_the_callers_process_group(tmp_path: Path) -> None:
    """r14: Ctrl-C to the run's process group killed brokered transport calls."""
    report = "import os; print(os.getpgid(0), os.getpid())"
    config = SlurmConfig(
        name="cluster",
        remote_workspace_root="/remote/vibesys",
        transport=SlurmSshTransport(host="cluster", ssh_command=(sys.executable, "-c", report)),
        transport_timeout_seconds=30,
    )
    broker = SlurmProcessBroker(
        slurm_transport_broker_policy(config, local_roots=(tmp_path,)),
        tmp_path / "broker.sock",
        processes=OsProcesses(),
    )
    broker.start()
    try:
        result = _brokered(
            broker, (sys.executable, "-c", report, "--", "cluster", "scancel 1"), timeout=30
        )
    finally:
        broker.close()

    group, pid = (int(value) for value in result.stdout.split())
    assert group == pid
    assert group != os.getpgid(0)


def test_a_broker_timeout_is_refused_after_the_transport_timeout(tmp_path: Path) -> None:
    clock = ManualClock()
    processes = FakeProcesses(
        lambda _spec: ProcessScript(stdout=b"partial-out", runs_until_signalled=True),
        pass_time=clock.advance,
    )
    broker = SlurmProcessBroker(
        slurm_transport_broker_policy(_config(), local_roots=(tmp_path,)),
        tmp_path / "broker.sock",
        processes=processes,
    )
    broker.start()
    try:
        with pytest.raises(PermissionError, match=r"timed out after 5 seconds"):
            _brokered(broker, ("/usr/bin/printf", "--", "cluster", "hostname"), b"transport input")
    finally:
        broker.close()

    assert clock.now() == 5
    assert processes.ran == [
        ProcessSpec(
            ("/usr/bin/printf", "--", "cluster", "hostname"),
            input=b"transport input",
            max_output_bytes=MAX_TRANSPORT_STREAM_BYTES,
        )
    ]


def test_connector_policy_runs_only_the_exact_configured_command(tmp_path: Path) -> None:
    processes = FakeProcesses(
        lambda _spec: ProcessScript(stdout=b'{"version":1}'),
        pass_time=ManualClock().advance,
    )
    policy = slurm_transport_broker_policy(_connector_config(), local_roots=(tmp_path,))
    request = _connector_request()
    result = policy.run(
        ProcessSpec(_CONNECTOR_COMMAND, input=request), processes=processes, timeout=5
    )
    with pytest.raises(PermissionError, match="executable is not authorized"):
        policy.run(
            ProcessSpec(("site-connector", "--other"), input=request),
            processes=processes,
            timeout=5,
        )

    assert isinstance(result, ProcessOutcome)
    assert result.stdout == b'{"version":1}'
    assert processes.ran == [
        ProcessSpec(
            _CONNECTOR_COMMAND,
            input=request,
            max_output_bytes=MAX_TRANSPORT_STREAM_BYTES,
        )
    ]


def test_connector_policy_executes_a_private_snapshot_when_the_source_name_is_replaced(
    tmp_path: Path,
) -> None:
    """Regression: a source swapped after authorization exposed a path outside the run root."""
    root = tmp_path / "run"
    root.mkdir()
    source = root / "input"
    source.write_text("authorized", encoding="utf-8")
    source.chmod(0o751)
    os.utime(source, ns=(_ARCHIVE_TIME_NS, _ARCHIVE_TIME_NS))
    outside = tmp_path / "outside"
    outside.write_text("secret", encoding="utf-8")
    observed: list[tuple[str, int, int]] = []

    def connector(spec: ProcessSpec) -> ProcessScript:
        source.unlink()
        source.symlink_to(outside)
        request = json.loads(spec.input)
        staged = Path(request["local_path"])
        observed.append(
            (
                staged.read_text(encoding="utf-8"),
                staged.stat().st_mode & 0o777,
                staged.stat().st_mtime_ns,
            )
        )
        assert staged != source
        return ProcessScript(stdout=b'{"version":1,"returncode":0,"stdout":"","stderr":""}')

    policy = slurm_transport_broker_policy(_connector_config(), local_roots=(root,))
    outcome = policy.run(
        ProcessSpec(
            _CONNECTOR_COMMAND,
            input=_connector_request(
                operation="put",
                local_path=str(source),
                remote_path="/remote/vibesys/cluster/input",
                command=None,
            ),
        ),
        processes=FakeProcesses(connector, pass_time=ManualClock().advance),
        timeout=5,
    )

    assert isinstance(outcome, ProcessOutcome)
    assert observed == [("authorized", 0o751, _ARCHIVE_TIME_NS)]


def test_ssh_policy_uses_the_same_private_snapshot_for_rsync(tmp_path: Path) -> None:
    root = tmp_path / "run"
    root.mkdir()
    source = root / "input"
    source.write_text("authorized", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.write_text("secret", encoding="utf-8")
    observed: list[str] = []

    def rsync(spec: ProcessSpec) -> ProcessScript:
        source.unlink()
        source.symlink_to(outside)
        staged = Path(spec.argv[-2])
        observed.append(staged.read_text(encoding="utf-8"))
        assert staged != source
        return ProcessScript()

    config = _config().model_copy(
        update={
            "transport": SlurmSshTransport(
                host="cluster",
                ssh_command=("ssh",),
                rsync_command=("rsync",),
            )
        }
    )
    outcome = slurm_transport_broker_policy(config, local_roots=(root,)).run(
        ProcessSpec(
            (
                "rsync",
                "-a",
                "-e",
                "ssh",
                "--",
                str(source),
                "cluster:/remote/vibesys/cluster/input",
            )
        ),
        processes=FakeProcesses(rsync, pass_time=ManualClock().advance),
        timeout=5,
    )

    assert isinstance(outcome, ProcessOutcome)
    assert observed == ["authorized"]


def test_connector_upload_snapshot_preserves_tree_and_executable_metadata(
    tmp_path: Path,
) -> None:
    root = tmp_path / "run"
    source = root / "tools"
    source.mkdir(parents=True)
    executable = source / "capture"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o551)
    os.utime(executable, ns=(_ARCHIVE_TIME_NS, _ARCHIVE_TIME_NS))
    source.chmod(0o550)
    os.utime(source, ns=(_ARCHIVE_TIME_NS, _ARCHIVE_TIME_NS))
    observed: list[tuple[int, int, int, int]] = []

    def connector(spec: ProcessSpec) -> ProcessScript:
        staged = Path(json.loads(spec.input)["local_dir"])
        staged_executable = staged / "capture"
        observed.append(
            (
                staged.stat().st_mode & 0o777,
                staged.stat().st_mtime_ns,
                staged_executable.stat().st_mode & 0o777,
                staged_executable.stat().st_mtime_ns,
            )
        )
        return ProcessScript(stdout=b'{"version":1,"returncode":0,"stdout":"","stderr":""}')

    outcome = slurm_transport_broker_policy(_connector_config(), local_roots=(root,)).run(
        ProcessSpec(
            _CONNECTOR_COMMAND,
            input=_connector_request(
                operation="sync_to",
                local_dir=str(source),
                remote_dir="/remote/vibesys/cluster/tools",
                delete=True,
                excludes=[],
                command=None,
            ),
        ),
        processes=FakeProcesses(connector, pass_time=ManualClock().advance),
        timeout=5,
    )

    assert isinstance(outcome, ProcessOutcome)
    assert observed == [(0o550, _ARCHIVE_TIME_NS, 0o551, _ARCHIVE_TIME_NS)]


def test_connector_policy_publishes_a_download_through_the_authorized_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "run"
    root.mkdir()
    target = root / "results" / "profile.json"

    def connector(spec: ProcessSpec) -> ProcessScript:
        request = json.loads(spec.input)
        staged = Path(request["local_path"])
        staged.write_text("captured", encoding="utf-8")
        staged.chmod(0o751)
        os.utime(staged, ns=(_ARCHIVE_TIME_NS, _ARCHIVE_TIME_NS))
        return ProcessScript(stdout=b'{"version":1,"returncode":0,"stdout":"","stderr":""}')

    policy = slurm_transport_broker_policy(_connector_config(), local_roots=(root,))
    outcome = policy.run(
        ProcessSpec(
            _CONNECTOR_COMMAND,
            input=_connector_request(
                operation="get",
                remote_path="/remote/vibesys/cluster/profile.json",
                local_path=str(target),
                kind="file",
                command=None,
            ),
        ),
        processes=FakeProcesses(connector, pass_time=ManualClock().advance),
        timeout=5,
    )

    assert isinstance(outcome, ProcessOutcome)
    assert target.read_text(encoding="utf-8") == "captured"
    assert (target.stat().st_mode & 0o777, target.stat().st_mtime_ns) == (
        0o751,
        _ARCHIVE_TIME_NS,
    )


def test_connector_tree_download_preserves_tree_and_executable_metadata(tmp_path: Path) -> None:
    root = tmp_path / "run"
    root.mkdir()
    target = root / "profile"

    def connector(spec: ProcessSpec) -> ProcessScript:
        staged = Path(json.loads(spec.input)["local_path"])
        executable = staged / "capture"
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o551)
        os.utime(executable, ns=(_ARCHIVE_TIME_NS, _ARCHIVE_TIME_NS))
        staged.chmod(0o550)
        os.utime(staged, ns=(_ARCHIVE_TIME_NS, _ARCHIVE_TIME_NS))
        return ProcessScript(stdout=b'{"version":1,"returncode":0,"stdout":"","stderr":""}')

    outcome = slurm_transport_broker_policy(_connector_config(), local_roots=(root,)).run(
        ProcessSpec(
            _CONNECTOR_COMMAND,
            input=_connector_request(
                operation="get",
                remote_path="/remote/vibesys/cluster/profile",
                local_path=str(target),
                kind="tree",
                command=None,
            ),
        ),
        processes=FakeProcesses(connector, pass_time=ManualClock().advance),
        timeout=5,
    )

    executable = target / "capture"
    assert isinstance(outcome, ProcessOutcome)
    assert (target.stat().st_mode & 0o777, target.stat().st_mtime_ns) == (
        0o550,
        _ARCHIVE_TIME_NS,
    )
    assert (executable.stat().st_mode & 0o777, executable.stat().st_mtime_ns) == (
        0o551,
        _ARCHIVE_TIME_NS,
    )


def test_connector_policy_refuses_a_download_parent_replaced_during_execution(
    tmp_path: Path,
) -> None:
    root = tmp_path / "run"
    parent = root / "results"
    parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    target = parent / "profile.json"

    def connector(spec: ProcessSpec) -> ProcessScript:
        request = json.loads(spec.input)
        Path(request["local_path"]).write_text("captured", encoding="utf-8")
        parent.rename(root / "replaced-results")
        parent.symlink_to(outside, target_is_directory=True)
        return ProcessScript(stdout=b'{"version":1,"returncode":0,"stdout":"","stderr":""}')

    policy = slurm_transport_broker_policy(_connector_config(), local_roots=(root,))
    with pytest.raises(PermissionError, match="run-owned roots"):
        policy.run(
            ProcessSpec(
                _CONNECTOR_COMMAND,
                input=_connector_request(
                    operation="get",
                    remote_path="/remote/vibesys/cluster/profile.json",
                    local_path=str(target),
                    kind="file",
                    command=None,
                ),
            ),
            processes=FakeProcesses(connector, pass_time=ManualClock().advance),
            timeout=5,
        )

    assert not (outside / "profile.json").exists()


def test_transport_stream_bound_always_fits_both_streams_in_the_broker_envelope() -> None:
    worst_json_escape = "\0" * MAX_TRANSPORT_STREAM_BYTES
    reply = BrokerRan(
        ok=True,
        result=ProcessReply(returncode=0, stdout=worst_json_escape, stderr=worst_json_escape),
    )

    assert len(reply.model_dump_json().encode() + b"\n") <= MAX_FRAME_BYTES


def test_transport_policy_enforces_the_same_stream_bound_at_execution() -> None:
    policy = slurm_transport_broker_policy(_connector_config())
    request = ProcessSpec(_CONNECTOR_COMMAND, input=_connector_request())

    accepted = policy.run(
        request,
        processes=FakeProcesses(
            lambda _spec: ProcessScript(stdout=b"x" * MAX_TRANSPORT_STREAM_BYTES),
            pass_time=ManualClock().advance,
        ),
        timeout=5,
    )
    refused = policy.run(
        request,
        processes=FakeProcesses(
            lambda _spec: ProcessScript(stdout=b"x" * (MAX_TRANSPORT_STREAM_BYTES + 1)),
            pass_time=ManualClock().advance,
        ),
        timeout=5,
    )

    assert isinstance(accepted, ProcessOutcome)
    assert isinstance(refused, ProcessOutputTooLarge)
    assert refused.limit == MAX_TRANSPORT_STREAM_BYTES


@pytest.mark.parametrize("spelling", ["/run/./output", "/run/jobs/../output"])
def test_connector_contract_rejects_dot_components_before_execution(
    tmp_path: Path, spelling: str
) -> None:
    processes = FakeProcesses(lambda _spec: ProcessScript(), pass_time=ManualClock().advance)
    policy = slurm_transport_broker_policy(_connector_config(), local_roots=(tmp_path,))

    with pytest.raises(PermissionError, match="malformed"):
        policy.run(
            ProcessSpec(
                _CONNECTOR_COMMAND,
                input=_connector_request(
                    operation="get",
                    remote_path="/remote/vibesys/cluster/output",
                    local_path=spelling,
                    kind="file",
                    command=None,
                ),
            ),
            processes=processes,
            timeout=5,
        )

    assert processes.ran == []


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (_connector_request(unexpected="sensitive-value"), "malformed"),
        (_connector_request(operation="remove"), "malformed"),
        (_connector_request(version=2), "malformed"),
        (b"not-json", "malformed"),
        (
            _connector_request(
                operation="put",
                local_path="/etc/passwd",
                remote_path="/remote/vibesys/cluster/input",
                command=None,
            ),
            "run-owned roots",
        ),
        (
            _connector_request(
                operation="get",
                remote_path="/other-cluster/output",
                local_path="/outside/output",
                kind="file",
                command=None,
            ),
            "remote namespace",
        ),
    ],
)
def test_connector_policy_rejects_invalid_contracts_and_paths_before_execution(
    tmp_path: Path, payload: bytes, message: str
) -> None:
    processes = FakeProcesses(lambda _spec: ProcessScript(), pass_time=ManualClock().advance)
    policy = slurm_transport_broker_policy(_connector_config(), local_roots=(tmp_path,))
    with pytest.raises(PermissionError, match=message) as raised:
        policy.run(ProcessSpec(_CONNECTOR_COMMAND, input=payload), processes=processes, timeout=5)

    assert "sensitive-value" not in str(raised.value)
    assert processes.ran == []


def test_local_transport_explicitly_has_no_safe_broker_capability(tmp_path: Path) -> None:
    config = SlurmConfig(
        name="cluster",
        remote_workspace_root="/shared/vibesys",
        transport=SlurmLocalTransport(kind="local"),
    )
    assert not slurm_transport_broker_available(config)
    with pytest.raises(PermissionError, match="operator shell on the host"):
        slurm_transport_broker_policy(config, local_roots=(tmp_path,))
