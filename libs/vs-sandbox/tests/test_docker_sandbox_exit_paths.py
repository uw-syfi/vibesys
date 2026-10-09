"""A Docker sandbox leaves no container behind on any way out of its lifecycle.

Regression for #1521: a ``docker run`` whose client died, timed out, or was
interrupted after the daemon created the container printed no id, so the
sandbox had nothing to stop and the container kept running. The sandbox names
its container up front and removes it by that name on every failed start.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_sandbox.api import RUN_ID_LABEL, DockerSandbox, SubprocessDockerCli
from vs_sandbox.api.testing import FakeDockerEngine

# One entry per `docker run` that loses its client after the daemon created the
# container: False leaves no id on stdout (crashed or interrupted client), True
# times out. Runs past the list succeed.
_LOST_RUNS = st.lists(st.booleans(), max_size=4)


class _BodyFailedError(RuntimeError):
    pass


def _engine(base: Path) -> FakeDockerEngine:
    state = base / "engine"
    state.mkdir(exist_ok=True)
    return FakeDockerEngine(state)


def _sandbox(base: Path, engine: FakeDockerEngine, run_id: str | None = "run-1") -> DockerSandbox:
    workspace = base / "workspace"
    workspace.mkdir(exist_ok=True)
    return DockerSandbox(
        host_workspace=str(workspace), image="agent-image", docker=engine, run_id=run_id
    )


@given(lost=_LOST_RUNS, exit_path=st.sampled_from(["completes", "body-fails"]))
def test_no_container_survives_any_start_outcome_or_exit(lost: list[bool], exit_path: str) -> None:
    with tempfile.TemporaryDirectory() as raw:
        base = Path(raw)
        engine = _engine(base)
        engine.runs_lost_after_creating(lost)
        # Every start up to the first success fails; the last one succeeds.
        for _attempt in range(len(lost) + 1):
            sandbox = _sandbox(base, engine)
            try:
                with sandbox:
                    if exit_path == "body-fails":
                        raise _BodyFailedError
            except (RuntimeError, subprocess.TimeoutExpired):
                pass
            assert engine.containers() == ()


def test_a_run_lost_after_the_daemon_created_the_container_is_removed() -> None:
    with tempfile.TemporaryDirectory() as raw:
        base = Path(raw)
        engine = _engine(base)
        engine.runs_lost_after_creating([False])

        with pytest.raises(RuntimeError, match="Failed to start Docker container"):
            _sandbox(base, engine).start()

        assert engine.containers() == ()


def test_a_run_that_times_out_after_the_daemon_created_the_container_is_removed() -> None:
    with tempfile.TemporaryDirectory() as raw:
        base = Path(raw)
        engine = _engine(base)
        engine.runs_lost_after_creating([True])

        with pytest.raises(RuntimeError, match="Timed out starting Docker container"):
            _sandbox(base, engine).start()

        assert engine.containers() == ()


@given(run_id=st.text(alphabet="abcdef0123456789-", min_size=1, max_size=24))
def test_the_container_carries_its_run_id_label(run_id: str) -> None:
    with tempfile.TemporaryDirectory() as raw:
        base = Path(raw)
        engine = _engine(base)

        with _sandbox(base, engine, run_id=run_id):
            (container,) = engine.containers()
            assert container.labels == {RUN_ID_LABEL: run_id}

        assert engine.containers() == ()


def test_a_sandbox_without_a_run_id_is_unlabelled() -> None:
    with tempfile.TemporaryDirectory() as raw:
        base = Path(raw)
        engine = _engine(base)

        with _sandbox(base, engine, run_id=None):
            (container,) = engine.containers()
            assert container.labels == {}


def test_docker_lifecycle_commands_run_outside_the_terminals_process_group() -> None:
    """Ctrl-C signals the foreground process group; a `docker stop` in it dies mid-flight."""
    probe = "import os; print(os.getsid(0) == os.getpid())"

    result = SubprocessDockerCli().run([sys.executable, "-c", probe], timeout_seconds=30)

    assert result.stdout.strip() == "True"
