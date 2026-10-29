"""The SSH transport never prompts, and says so when ssh could not connect.

A run's child has no controlling terminal, so an OpenSSH that would prompt reads
an empty answer and submits it to the server, up to three times per call, or
shows an askpass window on the operator's desktop. ``BatchMode=yes`` stops ssh
from asking at all. When ssh then fails (exit 255) the error names the cause
class and the docs page instead of a bare exit code, without carrying ssh's
standard error, which can hold a banner or a host's secrets.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from vs_sim.api import ProcessOutcome
from vs_sim.api.testing import ManualClock
from vs_slurm.api import (
    SlurmConfig,
    SlurmError,
    SlurmJobRequest,
    SlurmJobRunner,
    SlurmTransportFailedError,
)

if TYPE_CHECKING:
    from pathlib import Path

    from vs_sim.api import OutputSink, ProcessRun, ProcessSpec

_HOST = "cluster.example"
_PRIVATE_STDERR = "Banner for alice@cluster.example: site-token-7f3a"
_SSH_CONNECTION_FAILED = 255
_BATCH_MODE = ("-o", "BatchMode=yes")
_DOC = "docs/remote-slurm-execution.md"


class _SshCluster:
    """Answer the SSH transport's programs; *fail* names the one that exits 255."""

    def __init__(self, ssh: str, *, fail: str | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._ssh = ssh
        self._fail = fail

    def run(self, spec: ProcessSpec, *, timeout: float | None, output: OutputSink) -> ProcessRun:
        del timeout, output
        argv = spec.argv
        self.calls.append(argv)
        program = "ssh" if argv[0] == self._ssh else "rsync"
        if program == self._fail:
            return ProcessOutcome(_SSH_CONNECTION_FAILED, b"", _PRIVATE_STDERR.encode())
        if program == "rsync":
            return ProcessOutcome(0, b"", b"")
        command = argv[-1]
        if "for attempt in" in command:
            stdout = "PUBLISHED"
        elif "printf 'READY'" in command:
            stdout = "MISSING"
        elif "sbatch" in command:
            stdout = "Submitted batch job 4567\n"
        else:
            stdout = ""
        return ProcessOutcome(0, stdout.encode(), b"")


def _config(ssh_command: tuple[str, ...]) -> SlurmConfig:
    return SlurmConfig.model_validate(
        {
            "name": "test-cluster",
            "remote_workspace_root": "/operator/campaign/runs",
            "transport": {
                "kind": "ssh",
                "host": _HOST,
                "ssh_command": list(ssh_command),
                "rsync_command": ["rsync"],
            },
            "sbatch_command": ["sbatch"],
            "transport_timeout_seconds": 20,
        }
    )


def _submit(tmp_path: Path, cluster: _SshCluster, ssh_command: tuple[str, ...]) -> None:
    workspace = tmp_path / "candidate"
    workspace.mkdir(exist_ok=True)
    SlurmJobRunner(
        _config(ssh_command), processes=cluster, invocation_id=lambda: "ssh_01", clock=ManualClock()
    ).submit(SlurmJobRequest(workspace=workspace, command=("true",)))


def _contains(argv: tuple[str, ...], part: tuple[str, ...]) -> bool:
    return any(argv[index : index + len(part)] == part for index in range(len(argv)))


_OPTION = st.sampled_from(("-F", "-i", "-J", "-p", "-l")).flatmap(
    lambda flag: st.tuples(st.just(flag), st.from_regex(r"[A-Za-z0-9_./-]{1,12}", fullmatch=True))
)
_SSH_COMMANDS = st.tuples(
    st.sampled_from(("ssh", "/usr/bin/ssh", "/opt/site/bin/ssh")),
    st.lists(_OPTION, max_size=3),
).map(lambda parts: (parts[0], *(item for option in parts[1] for item in option)))


@pytest.mark.regression
@given(ssh_command=_SSH_COMMANDS)
@example(ssh_command=("ssh",))
def test_every_ssh_transport_call_runs_in_batch_mode(
    tmp_path_factory: pytest.TempPathFactory, ssh_command: tuple[str, ...]
) -> None:
    """Each ssh call, and the remote shell of each rsync, disables every prompt."""
    cluster = _SshCluster(ssh_command[0])

    _submit(tmp_path_factory.mktemp("run"), cluster, ssh_command)

    ssh_calls = [argv for argv in cluster.calls if argv[0] == ssh_command[0]]
    rsync_calls = [argv for argv in cluster.calls if argv[0] == "rsync"]
    assert ssh_calls
    assert rsync_calls
    for argv in ssh_calls:
        options = argv[: argv.index("--")]
        assert options[: len(ssh_command)] == ssh_command
        assert _contains(options, _BATCH_MODE)
    for argv in rsync_calls:
        remote_shell = shlex.split(argv[argv.index("-e") + 1])
        assert remote_shell[: len(ssh_command)] == list(ssh_command)
        assert _contains(tuple(remote_shell), _BATCH_MODE)


@pytest.mark.regression
@pytest.mark.parametrize("failing", ["ssh", "rsync"])
def test_an_ssh_connection_failure_names_its_cause_without_its_output(
    tmp_path: Path, failing: str
) -> None:
    """Exit 255 points to non-interactive SSH and the docs; ssh's stderr stays private."""
    cluster = _SshCluster("ssh", fail=failing)

    with pytest.raises(SlurmError) as raised:
        _submit(tmp_path, cluster, ("ssh",))

    message = str(raised.value)
    assert "non-interactive" in message
    assert _DOC in message
    assert "site-token-7f3a" not in message
    cause = raised.value.__cause__
    assert isinstance(cause, SlurmTransportFailedError)
    assert cause.returncode == _SSH_CONNECTION_FAILED
