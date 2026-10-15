"""Public contract tests for host-owned Slurm transport access."""

from __future__ import annotations

import json
import os
import sys
from typing import TYPE_CHECKING

import pytest

from vs_sandbox.api.slurm import BrokeredProcesses, SlurmProcessBroker
from vs_sim.api import OsProcesses, ProcessOutcome, ProcessSpec, UnixNetwork
from vs_sim.api.testing import FakeProcesses, ManualClock, ProcessScript
from vs_slurm.api import (
    SlurmConfig,
    SlurmConnectorTransport,
    SlurmLocalTransport,
    SlurmSshTransport,
    slurm_transport_broker_policy,
)

if TYPE_CHECKING:
    from pathlib import Path


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
        ProcessSpec(("/usr/bin/printf", "--", "cluster", "hostname"), input=b"transport input")
    ]


def test_connector_broker_runs_only_the_exact_configured_command(tmp_path: Path) -> None:
    processes = FakeProcesses(
        lambda _spec: ProcessScript(stdout=b'{"version":1}'),
        pass_time=ManualClock().advance,
    )
    config = _connector_config()
    broker = SlurmProcessBroker(
        slurm_transport_broker_policy(config, local_roots=(tmp_path,)),
        tmp_path / "broker.sock",
        processes=processes,
    )
    broker.start()
    request = _connector_request()
    try:
        result = _brokered(broker, _CONNECTOR_COMMAND, request)
        with pytest.raises(PermissionError, match="executable is not authorized"):
            _brokered(broker, ("site-connector", "--other"), request)
    finally:
        broker.close()

    assert result.stdout == b'{"version":1}'
    assert processes.ran == [ProcessSpec(_CONNECTOR_COMMAND, input=request)]


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
def test_connector_broker_rejects_invalid_contracts_and_paths_before_execution(
    tmp_path: Path, payload: bytes, message: str
) -> None:
    processes = FakeProcesses(lambda _spec: ProcessScript(), pass_time=ManualClock().advance)
    config = _connector_config()
    broker = SlurmProcessBroker(
        slurm_transport_broker_policy(config, local_roots=(tmp_path,)),
        tmp_path / "broker.sock",
        processes=processes,
    )
    broker.start()
    try:
        with pytest.raises(PermissionError, match=message) as raised:
            _brokered(broker, _CONNECTOR_COMMAND, payload)
    finally:
        broker.close()

    assert "sensitive-value" not in str(raised.value)
    assert processes.ran == []


def test_local_transport_explicitly_has_no_safe_broker_capability(tmp_path: Path) -> None:
    config = SlurmConfig(
        name="cluster",
        remote_workspace_root="/shared/vibesys",
        transport=SlurmLocalTransport(kind="local"),
    )
    policy = slurm_transport_broker_policy(config, local_roots=(tmp_path,))

    assert not policy.available
    assert policy.unavailable_reason is not None
    assert "operator shell on the host" in policy.unavailable_reason
    with pytest.raises(TypeError, match="cannot be safely exposed"):
        SlurmProcessBroker(
            policy,
            tmp_path / "broker.sock",
            processes=FakeProcesses(lambda _spec: ProcessScript(), pass_time=ManualClock().advance),
        )
