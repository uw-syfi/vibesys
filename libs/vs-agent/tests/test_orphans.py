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
from vs_sim.api import DISCARD
from vs_sim.api.testing import FakePipedProcessLauncher, examples

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vs_sim.api import OutputSink


def _start(engine: FakeDockerEngine, run_id: str | None) -> str:
    label = [] if run_id is None else ["--label", f"{RUN_ID_LABEL}={run_id}"]
    result = engine.run(["docker", "run", "-d", *label, "image"], timeout_seconds=1, output=DISCARD)
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


@settings(max_examples=examples(pr=5, nightly=40), deadline=None)
@given(
    own=st.integers(min_value=0, max_value=4),
    others=st.integers(min_value=0, max_value=3),
    unlabelled=st.integers(min_value=0, max_value=2),
)
def test_every_container_of_the_run_and_only_those_are_reaped_then_removed(
    own: int, others: int, unlabelled: int
) -> None:
    with tempfile.TemporaryDirectory() as raw:
        engine = FakeDockerEngine(
            Path(raw), agent_ids=(1000, 1000), launcher=FakePipedProcessLauncher()
        )
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
        engine = FakeDockerEngine(
            Path(raw), agent_ids=(1000, 1000), launcher=FakePipedProcessLauncher()
        )
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

    def run(
        self, argv: Sequence[str], *, timeout_seconds: float, output: OutputSink
    ) -> DockerResult:
        del output
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


class _RemovalAnswers:
    """A docker client listing one container of the run, whose ``rm -f`` answers as given."""

    def __init__(self, container_id: str, returncode: int | None, stderr: str) -> None:
        self._container_id = container_id
        self._returncode = returncode
        self._stderr = stderr

    def run(
        self, argv: Sequence[str], *, timeout_seconds: float, output: OutputSink
    ) -> DockerResult:
        del output
        if argv[1] == "ps":
            return docker_result(list(argv), 0, f"{self._container_id}\n", "")
        assert list(argv[1:3]) == ["rm", "-f"]
        if self._returncode is None:
            raise docker_timed_out(list(argv), timeout_seconds)
        return docker_result(list(argv), self._returncode, "", self._stderr)

    def spawn(self, argv: Sequence[str]) -> NoReturn:
        raise AssertionError(argv)


_GONE = "Error response from daemon: No such container: {id}"
_GOING = "Error response from daemon: removal of container {id} is already in progress"


@settings(max_examples=examples(pr=20, nightly=100))
@given(
    answer=st.one_of(
        st.just((0, "")),
        st.tuples(st.integers(1, 255), st.sampled_from([_GONE, _GOING])),
        st.tuples(st.integers(1, 255), st.text(alphabet="abc :", max_size=20)),
        st.just((None, "")),
    )
)
def test_a_removal_is_released_exactly_when_the_container_is_gone_or_going(
    answer: tuple[int | None, str],
) -> None:
    """Regression (REVIEW-9 P3-3): an ``--rm`` container gone before its ``rm`` refused a resume.

    The reaper reads ``docker rm -f`` through the one Docker lifecycle translator,
    so "already gone" and "already being removed" end the orphan, and any other
    failure, or a client that never answered, still stops the resume.
    """
    returncode, template = answer
    container = "c0ffee"
    stderr = template.format(id=container)
    docker = _RemovalAnswers(container, returncode, stderr)
    gone_or_going = returncode == 0 or template in {_GONE, _GOING}

    def reap() -> tuple[str, ...]:
        return reap_orphaned_agents(
            "run-a",
            docker=docker,
            log=lambda _line: None,
            confinement_for=lambda _container: FakeConfinement(),
        )

    if gone_or_going:
        assert reap() == (container,)
    else:
        with pytest.raises(OrphanReapError, match=container):
            reap()
