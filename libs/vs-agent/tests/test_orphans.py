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

from vs_agent.api import (
    OrphanReapError,
    reap_orphaned_agents,
    reap_step_agents,
)
from vs_agent.api.testing import (
    DockerResult,
    FakeAgentProcesses,
    docker_result,
    docker_timed_out,
)
from vs_sandbox.api import RUN_ID_LABEL, STEP_TAG_ENV, step_tag_env
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


_TAGS = st.sampled_from(["step-a", "step-b", "step-c", "step-ab", "a=b"])


def _engine_with_containers(raw: str, run: int, others: int) -> tuple[FakeDockerEngine, list[str]]:
    engine = FakeDockerEngine(
        Path(raw), agent_ids=(1000, 1000), launcher=FakePipedProcessLauncher()
    )
    mine = [_start(engine, "run-a") for _ in range(run)]
    for _ in range(others):
        _start(engine, "run-b")
    return engine, mine


@settings(max_examples=examples(pr=15, nightly=100), deadline=None)
@given(
    own=st.integers(min_value=1, max_value=3),
    others=st.integers(min_value=0, max_value=2),
    processes=st.lists(
        st.tuples(st.integers(0, 4), st.one_of(st.none(), _TAGS)), min_size=0, max_size=14
    ),
    target=_TAGS,
)
def test_a_step_sweep_ends_the_processes_carrying_its_tag_and_nothing_else(
    own: int, others: int, processes: list[tuple[int, str | None]], target: str
) -> None:
    """The tag a turn marks its processes with (``step_tag_env``) is the one the sweep matches."""
    with tempfile.TemporaryDirectory() as raw:
        engine, mine = _engine_with_containers(raw, own, others)
        everyone = [c.container_id for c in engine.containers()]
        table = FakeAgentProcesses()
        started = [
            table.start(everyone[slot % len(everyone)], {} if tag is None else step_tag_env(tag))
            for slot, tag in processes
        ]
        containers_before = sorted(everyone)

        searched = reap_step_agents(
            "run-a",
            target,
            docker=engine,
            log=lambda _line: None,
            reap_tagged=table.reap_tagged,
        )
        again = reap_step_agents(
            "run-a", target, docker=engine, log=lambda _line: None, reap_tagged=table.reap_tagged
        )

        assert sorted(searched) == sorted(mine) == sorted(again)
        for process in started:
            swept = process.container_id in mine and process.tag == target
            assert table.is_alive(process) is not swept
        assert sorted(c.container_id for c in engine.containers()) == containers_before


def test_a_step_sweep_that_cannot_reach_a_container_names_it_and_the_step() -> None:
    with tempfile.TemporaryDirectory() as raw:
        engine, mine = _engine_with_containers(raw, 2, 0)
        table = FakeAgentProcesses()

        with (
            table.unreachable(mine[1]),
            pytest.raises(OrphanReapError, match=f"step-a.*{mine[1]}"),
        ):
            reap_step_agents(
                "run-a",
                "step-a",
                docker=engine,
                log=lambda _line: None,
                reap_tagged=table.reap_tagged,
            )


@pytest.mark.parametrize("tag", ["", "a\nb", "a\0b"])
def test_a_tag_no_sweep_could_match_is_refused_before_docker_is_asked(tag: str) -> None:
    with pytest.raises(ValueError, match="step tag"):
        reap_step_agents(
            "run-a", tag, docker=_BrokenDocker(1), log=lambda _line: None, reap_tagged=None
        )


class _ExecAnswers:
    """A docker client listing one container of the run, whose ``exec`` answers as given."""

    def __init__(self, container_id: str, returncode: int | None, stderr: str) -> None:
        self._container_id = container_id
        self._returncode = returncode
        self._stderr = stderr
        self.execs: list[list[str]] = []

    def run(
        self, argv: Sequence[str], *, timeout_seconds: float, output: OutputSink
    ) -> DockerResult:
        del output
        if argv[1] == "ps":
            return docker_result(list(argv), 0, f"{self._container_id}\n", "")
        assert argv[1] == "exec", argv
        self.execs.append(list(argv))
        if self._returncode is None:
            raise docker_timed_out(list(argv), timeout_seconds)
        return docker_result(list(argv), self._returncode, "", self._stderr)

    def spawn(self, argv: Sequence[str]) -> NoReturn:
        raise AssertionError(argv)


def test_the_default_sweep_passes_the_tag_as_an_argument_never_as_environment() -> None:
    docker = _ExecAnswers("c0ffee", 0, "")

    reap_step_agents("run-a", "step-a", docker=docker, log=lambda _line: None)

    (argv,) = docker.execs
    assert argv[:4] == ["docker", "exec", "c0ffee", "sh"]
    assert "-e" not in argv
    assert argv[-2:] == [STEP_TAG_ENV, "step-a"]


@pytest.mark.parametrize(
    ("returncode", "stderr", "ends"),
    [
        (0, "", True),
        (1, "Error response from daemon: No such container: c0ffee", True),
        (1, "Error response from daemon: Container c0ffee is not running", True),
        (127, "reap needs tr and grep in the container", False),
        (1, "daemon error", False),
        (None, "", False),
    ],
)
def test_the_default_sweep_is_clean_only_when_every_tagged_process_is_proven_ended(
    returncode: int | None, stderr: str, *, ends: bool
) -> None:
    docker = _ExecAnswers("c0ffee", returncode, stderr)

    def sweep() -> tuple[str, ...]:
        return reap_step_agents("run-a", "step-a", docker=docker, log=lambda _line: None)

    if ends:
        assert sweep() == ("c0ffee",)
    else:
        with pytest.raises(OrphanReapError, match="step-a"):
            sweep()
