"""A ``docker rm -f`` that fails without saying the container is gone is retried.

Regression: a removal whose ``docker`` client died by a signal (or timed out, or
got an unrecognised daemon error) was tried once, so the container stayed until
interpreter exit. Removal is idempotent, so the sandbox retries it a bounded
number of times; a later "No such container" means the first attempt worked.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_sandbox.api.testing import (
    DockerCliOutcome,
    ScriptedDockerCli,
    docker_result,
    docker_timed_out,
)
from vs_sandbox.docker_sandbox import DockerSandbox, _cleanup_containers, _live_containers
from vs_sim.api import OsThreads

# The attempts the sandbox makes before keeping the container for a later retry.
_ATTEMPTS = 3

_FAILED_REMOVALS: st.SearchStrategy[DockerCliOutcome] = st.one_of(
    st.sampled_from([-9, -11, -15]).map(lambda code: docker_result(returncode=code)),
    st.just(docker_result(returncode=1, stderr="daemon unavailable")),
    st.just(docker_timed_out(("docker", "rm", "-f", "abc123"), 10.0)),
    st.just(OSError("Docker daemon disconnected")),
)

_SETTLED_REMOVALS: st.SearchStrategy[DockerCliOutcome] = st.sampled_from(
    [
        docker_result(),
        docker_result(returncode=1, stderr="Error response from daemon: No such container: abc123"),
    ]
)


def _released_after(
    failures: list[DockerCliOutcome], settled: DockerCliOutcome, act: Literal["stop", "cleanup"]
) -> tuple[bool, int]:
    """Start a sandbox, fail its removal *failures* times, then settle; return (released, rm calls)."""
    docker = ScriptedDockerCli()
    docker.on(("docker", "rm"), *failures, settled)
    docker.always(docker_result(stdout="abc123\n"))
    with tempfile.TemporaryDirectory() as raw:
        sandbox = DockerSandbox(
            docker=docker,
            host_workspace=str(Path(raw) / "workspace"),
            image="test-image",
            threads=OsThreads(),
        )
        sandbox.start()
        try:
            if act == "stop":
                try:
                    sandbox.stop()
                except (RuntimeError, OSError, subprocess.TimeoutExpired):
                    pass
            else:
                _cleanup_containers()
            removals = sum(1 for argv in docker.argvs if argv[:2] == ("docker", "rm"))
            return "abc123" not in _live_containers, removals
        finally:
            _live_containers.pop("abc123", None)


@pytest.mark.parametrize("act", ["stop", "cleanup"])
@given(
    failures=st.lists(_FAILED_REMOVALS, max_size=_ATTEMPTS + 1),
    settled=_SETTLED_REMOVALS,
)
def test_a_failed_removal_is_retried_until_docker_says_the_container_is_gone(
    act: Literal["stop", "cleanup"], failures: list[DockerCliOutcome], settled: DockerCliOutcome
) -> None:
    released, removals = _released_after(failures, settled, act)

    assert released is (len(failures) < _ATTEMPTS)
    assert removals == min(len(failures) + 1, _ATTEMPTS)
