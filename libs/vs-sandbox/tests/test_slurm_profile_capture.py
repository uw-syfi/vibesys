"""The capture a Slurm run can execute is derived from its service and its profile command."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_sandbox.api.slurm import (
    configured_capture_lifecycle,
    load_slurm_policy,
    trusted_profile_command,
)
from vs_slurm.api import load_slurm_config

if TYPE_CHECKING:
    from pathlib import Path

_BASE = """[slurm]
name = "test-cluster"
remote_workspace_root = "/remote/runs"
job_timeout_seconds = 1800

[slurm.transport]
kind = "ssh"
host = "cluster"

[vibesys]
remote_python = "/remote/venv/bin/python"
setup_script = "/remote/setup.sh"
"""
_SERVICE = """
[vibesys.service]
command = ["python", "-m", "engine.server", "--port", "VIBESYS_DYNAMIC_PORT"]
readiness_url = "http://127.0.0.1:VIBESYS_DYNAMIC_PORT/health"
startup_timeout_seconds = 700
"""
_ARGV = st.lists(
    st.text(alphabet="abcxyz -_./=", min_size=1, max_size=12), min_size=1, max_size=4
).map(tuple)


def _operator(tmp_path: Path, *, service: bool) -> Path:
    path = tmp_path / "slurm.toml"
    path.write_text(_BASE + (_SERVICE if service else ""), encoding="utf-8")
    return path


@pytest.mark.parametrize("service", [False, True])
@pytest.mark.parametrize("has_command", [False, True])
def test_a_capture_exists_exactly_when_there_is_something_to_capture(
    tmp_path: Path, *, service: bool, has_command: bool
) -> None:
    path = _operator(tmp_path, service=service)
    config, policy = load_slurm_config(path), load_slurm_policy(path)
    command = ("python", "kernel_bench.py") if has_command else None
    if service and not has_command:
        with pytest.raises(ValueError, match=r"profile\.command"):
            configured_capture_lifecycle(config, policy, command)
        return
    lifecycle = configured_capture_lifecycle(config, policy, command)
    assert (lifecycle is not None) == has_command
    trusted = trusted_profile_command(config, policy, command, profiler_tree="tree")
    assert (trusted is not None) == has_command


@pytest.fixture(scope="module")
def serviceless_operator(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _operator(tmp_path_factory.mktemp("operator"), service=False)


@given(command=_ARGV, workload_timeout=st.none() | st.integers(min_value=1, max_value=100_000))
def test_a_command_only_capture_waits_for_the_target_to_exit(
    serviceless_operator: Path, command: tuple[str, ...], workload_timeout: int | None
) -> None:
    config, policy = (
        load_slurm_config(serviceless_operator),
        load_slurm_policy(serviceless_operator),
    )
    lifecycle = configured_capture_lifecycle(
        config, policy, command, workload_timeout_seconds=workload_timeout
    )
    assert lifecycle is not None
    # No service: nothing to make ready, no load to drive, so the target is never signalled
    # to stop; it exits by itself within a timeout that fits the job.
    assert "load_command" not in lifecycle
    assert "ready_command" not in lifecycle
    assert 0 < float(str(lifecycle["timeout_s"])) <= config.job_timeout_seconds
    if workload_timeout is not None:
        assert float(str(lifecycle["timeout_s"])) <= workload_timeout + float(
            str(lifecycle["grace_s"])
        )
    request_command = trusted_profile_command(
        config, policy, command, profiler_tree="tree", workload_timeout_seconds=workload_timeout
    )
    assert request_command is not None
    request = json.loads(request_command[request_command.index("--request-json") + 1])
    assert request["lifecycle"] == lifecycle


def test_a_command_using_the_dynamic_port_needs_a_service(tmp_path: Path) -> None:
    path = _operator(tmp_path, service=False)
    with pytest.raises(ValueError, match="service"):
        configured_capture_lifecycle(
            load_slurm_config(path),
            load_slurm_policy(path),
            ("bench", "--port", "VIBESYS_DYNAMIC_PORT"),
        )
