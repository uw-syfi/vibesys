"""Startup reaping of the agents a dead host left in a run's containers."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import agentshim
import pytest
from agentshim.testing import FakeConfinement
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_agent.api import OrphanReapError, reap_orphaned_agents
from vs_agent.api.testing import DockerResult, docker_result, docker_timed_out
from vs_sandbox.api import RUN_ID_LABEL
from vs_sandbox.fake_docker_engine import FakeDockerEngine

if TYPE_CHECKING:
    from collections.abc import Sequence


def _start(engine: FakeDockerEngine, run_id: str | None) -> str:
    label = [] if run_id is None else ["--label", f"{RUN_ID_LABEL}={run_id}"]
    result = engine.run(["docker", "run", "-d", *label, "image"], timeout_seconds=1)
    return result.stdout.strip()


class _Recorder:
    """Confinements that note, in one log with the engine's removals, what happened first."""

    def __init__(self, engine: FakeDockerEngine) -> None:
        self.engine = engine
        self.reaped: list[tuple[str, bool]] = []
        self.failing: set[str] = set()

    def confinement_for(self, container_id: str) -> agentshim.Confinement:
        present = any(c.container_id == container_id for c in self.engine.containers())
        self.reaped.append((container_id, present))
        if container_id in self.failing:
            return _FailingReap()
        return FakeConfinement()


class _FailingReap(FakeConfinement):
    def reap(self) -> None:
        msg = "daemon error"
        raise agentshim.ReapError(msg)


@settings(max_examples=40, deadline=None)
@given(
    own=st.integers(min_value=0, max_value=4),
    others=st.integers(min_value=0, max_value=3),
    unlabelled=st.integers(min_value=0, max_value=2),
)
def test_every_container_of_the_run_and_only_those_are_reaped_then_removed(
    own: int, others: int, unlabelled: int
) -> None:
    with tempfile.TemporaryDirectory() as raw:
        engine = FakeDockerEngine(Path(raw), agent_ids=(1000, 1000))
        mine = [_start(engine, "run-a") for _ in range(own)]
        theirs = [_start(engine, "run-b") for _ in range(others)]
        loose = [_start(engine, None) for _ in range(unlabelled)]
        recorder = _Recorder(engine)
        logs: list[str] = []

        ended = reap_orphaned_agents(
            "run-a",
            docker=engine,
            log=logs.append,
            confinement_for=recorder.confinement_for,
        )

        assert sorted(ended) == sorted(mine)
        # Each orphan was reaped while its container still existed, then removed.
        assert sorted(recorder.reaped) == sorted((c, True) for c in mine)
        assert sorted(c.container_id for c in engine.containers()) == sorted(theirs + loose)
        assert len(logs) == own


def test_a_failed_reap_stops_the_resume_and_leaves_the_container_for_inspection() -> None:
    with tempfile.TemporaryDirectory() as raw:
        engine = FakeDockerEngine(Path(raw), agent_ids=(1000, 1000))
        container = _start(engine, "run-a")
        recorder = _Recorder(engine)
        recorder.failing.add(container)

        with pytest.raises(OrphanReapError, match=container):
            reap_orphaned_agents(
                "run-a",
                docker=engine,
                log=lambda _line: None,
                confinement_for=recorder.confinement_for,
            )

        assert [c.container_id for c in engine.containers()] == [container]


class _BrokenDocker:
    """A docker client whose every command fails the given way."""

    def __init__(self, returncode: int | None) -> None:
        self._returncode = returncode

    def run(self, argv: Sequence[str], *, timeout_seconds: float) -> DockerResult:
        if self._returncode is None:
            raise docker_timed_out(list(argv), timeout_seconds)
        return docker_result(list(argv), self._returncode, "", "daemon down")

    def spawn(self, argv: Sequence[str]) -> NoReturn:
        raise AssertionError(argv)


@pytest.mark.parametrize("returncode", [None, 1, 125])
def test_a_daemon_that_cannot_list_containers_is_an_error_not_an_empty_run(
    returncode: int | None,
) -> None:
    with pytest.raises(OrphanReapError, match="run-a"):
        reap_orphaned_agents("run-a", docker=_BrokenDocker(returncode), log=lambda _line: None)
