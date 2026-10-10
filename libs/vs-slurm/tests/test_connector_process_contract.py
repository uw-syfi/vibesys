"""One process-boundary contract for every transport and the in-process Fake.

The runner reaches a cluster through ``SlurmProcess``. The deterministic tiers
inject ``FakeConnector`` so no process is spawned; the real connector is the
executable ``python -m vs_slurm.fake_connector``, which also stands in for the
local transport's shell and rsync programs. A caller written against the
interface must observe the same replies from each, so the same job runs against
each and the observable outcome and the scheduler commands issued must agree.
"""

from __future__ import annotations

import json
import re
import sys
from typing import TYPE_CHECKING

from hypothesis import given, settings
from hypothesis import strategies as st

from vs_slurm.api import (
    SlurmConfig,
    SlurmConnectorTransport,
    SlurmJobRequest,
    SlurmJobRunner,
    SlurmLocalTransport,
)

# test-isolation: the Fake connector is the library's executable test double.
from vs_slurm.fake_connector import FakeConnector, executing_cluster, recorded_commands

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import pytest

_JOB_NAME = re.compile(r"--job-name=\S+")


def _real_runner(root: Path, remote: Path) -> SlurmJobRunner:
    """Run the connector as the production runner does: one process per request."""
    state = executing_cluster(root / "cluster")
    command = (sys.executable, "-m", "vs_slurm.fake_connector", str(state))
    return SlurmJobRunner(
        _config(remote, SlurmConnectorTransport(kind="connector", command=command)),
        scratch_root=root / "scratch",
        invocation_id=lambda: "contract_01",
    )


def _local_runner(root: Path, remote: Path) -> SlurmJobRunner:
    """Run the local transport's shell and rsync as processes, as a submit host does."""
    state = executing_cluster(root / "cluster")
    program = (sys.executable, "-m", "vs_slurm.fake_connector", str(state))
    transport = SlurmLocalTransport(
        kind="local", shell_command=(*program, "shell"), rsync_command=(*program, "rsync")
    )
    return SlurmJobRunner(
        _config(remote, transport),
        scratch_root=root / "scratch",
        invocation_id=lambda: "contract_01",
    )


def _fake_runner(root: Path, remote: Path) -> SlurmJobRunner:
    """Run the same runner over the in-process Fake: no process is spawned."""
    connector = FakeConnector(root / "cluster")
    return SlurmJobRunner(
        _config(remote, SlurmConnectorTransport(kind="connector", command=("fake-connector",))),
        process=connector,
        scratch_root=root / "scratch",
        invocation_id=lambda: "contract_01",
    )


def _config(
    remote: Path, transport: SlurmConnectorTransport | SlurmLocalTransport
) -> SlurmConfig:
    return SlurmConfig(name="fake", remote_workspace_root=str(remote), transport=transport)


def _observe(
    root: Path, remote: Path, make: Callable[[Path, Path], SlurmJobRunner], script: str
) -> tuple[object, ...]:
    root.mkdir(parents=True)
    remote.mkdir()
    workspace = root / "workspace"
    workspace.mkdir()
    (root / "scratch").mkdir()
    result = make(root, remote).run(
        SlurmJobRequest(workspace=workspace, command=("sh", "-c", script))
    )
    commands = [
        # The scheduler job name embeds a digest of the (per-side) workspace path.
        _JOB_NAME.sub("--job-name=<name>", command.replace(str(remote), "<remote>")).replace(
            str(root), "<root>"
        )
        for command in recorded_commands(root / "cluster")
    ]
    return result.exit_code, result.output, result.job_id, result.collection_failure, commands


@settings(max_examples=4, deadline=None)
@given(
    exit_code=st.integers(min_value=0, max_value=3),
    text=st.text(alphabet="abcxyz 012", min_size=1, max_size=12),
)
def test_the_in_process_fake_answers_like_the_real_connector(
    tmp_path_factory: pytest.TempPathFactory, exit_code: int, text: str
) -> None:
    base = tmp_path_factory.mktemp("contract")
    script = f"echo '{text}'; exit {exit_code}"

    real = _observe(base / "real", base / "real-remote", _real_runner, script)
    fake = _observe(base / "fake", base / "fake-remote", _fake_runner, script)

    assert fake == real
    assert real[0] == exit_code


@settings(max_examples=4, deadline=None)
@given(
    exit_code=st.integers(min_value=0, max_value=3),
    text=st.text(alphabet="abcxyz 012", min_size=1, max_size=12),
)
def test_the_local_transport_answers_like_the_connector(
    tmp_path_factory: pytest.TempPathFactory, exit_code: int, text: str
) -> None:
    base = tmp_path_factory.mktemp("contract")
    script = f"echo '{text}'; exit {exit_code}"

    local = _observe(base / "local", base / "local-remote", _local_runner, script)
    connector = _observe(base / "real", base / "real-remote", _real_runner, script)

    assert local == connector
    assert local[0] == exit_code


def _exec_request(command: str) -> str:
    return json.dumps({"version": 1, "operation": "exec", "command": command})


@settings(max_examples=6, deadline=None)
@given(text=st.text(alphabet="abcxyz012", min_size=1, max_size=8))
def test_an_interceptor_answers_or_forwards_every_request_and_none_restores_the_cluster(
    tmp_path_factory: pytest.TempPathFactory, text: str
) -> None:
    connector = FakeConnector(tmp_path_factory.mktemp("cluster") / "cluster")
    seen: list[object] = []
    refusal: dict[str, object] = {"version": 1, "returncode": 1, "stdout": "", "stderr": text}

    def intercept(
        request: dict[str, object], answer: Callable[[dict[str, object]], dict[str, object]]
    ) -> dict[str, object]:
        seen.append(request["command"])
        return refusal if str(request["command"]).startswith("refuse") else answer(request)

    connector.intercept(intercept)
    refused = connector(["x"], stdin=_exec_request(f"refuse {text}"), timeout=1)
    forwarded = connector(["x"], stdin=_exec_request(f"echo {text}"), timeout=1)

    assert json.loads(refused.stdout) == refusal
    assert json.loads(forwarded.stdout)["stdout"] == f"{text}\n"
    assert seen == [f"refuse {text}", f"echo {text}"]
    # The refused request never reached the cluster; the forwarded one did.
    assert recorded_commands(connector.state) == [f"echo {text}"]
    connector.intercept(None)
    connector(["x"], stdin=_exec_request(f"echo {text}"), timeout=1)
    assert seen == [f"refuse {text}", f"echo {text}"]
