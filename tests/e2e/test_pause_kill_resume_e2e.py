"""Real-process proof that a scripted run survives a pause or a kill at any step, and resumes.

The run is a real process (``tests.e2e._interruptible_run``): the production host composition
over the product's on-disk ``.vibesys``, scripted agents, and the executing Fake Slurm cluster
as real files. Nothing is simulated about the interruption. A pause is the operator's
``vibesys control RUN pause`` (or ``pause-now``) written to the run's control inbox; a kill is a
real SIGKILL of the run's process; a resume is a new process that relaunches the saved run
as a new epoch, as ``vibesys --resume`` does.

The interruption lands inside a step the run process itself announces it holds (an agent turn
before its session exists, an agent turn at work, an evaluation job pending on the cluster), so
no test sleeps to place it. Every case then resumes and must end as an uninterrupted run of the
same script ends: completed, the same winner selection, the same adopted project file, and
nothing left running (no process in the run's group, no job on the cluster).
"""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass, field
from io import StringIO
from typing import TYPE_CHECKING

import pytest
from tests.e2e._interruptible_run import START, TURN
from tests.e2e.dynamic_loop_harness import _AGENT_CONFIG, LoopInput
from tests.e2e.scripted_value_run import SEED

from entrypoints.control import control_main, time_ordered_id
from vibesys.events import CoreEventType
from vibesys.orchestration.dynamic.scripted.api import IMPLEMENTER_KIND, PLANNER_KIND
from vs_sim.api import OsThreads
from vs_sim.api.testing import HANG_GUARD_S
from vs_slurm.fake_connector import active_jobs, pending_jobs, release_jobs

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

_Line = dict[str, object]

SILENCE_GUARD_S = 20.0
"""How long a run may report nothing before it counts as stuck.

A running scripted run reports many events a second, and a held step is waited for only
until it announces itself, so a third of the hang guard is far above any healthy gap.
"""
_PARKED = "parked"
_COMPLETED = "completed"


@dataclass
class RunProcess:
    """One run process: its reports so far, and the means to hold, release, signal and wait."""

    base: Path
    process: subprocess.Popen[str]
    errors: Path
    seen: list[_Line] = field(default_factory=list)
    lines: queue.Queue[_Line | None] = field(default_factory=queue.Queue)

    @classmethod
    def start(cls, base: Path, *flags: str) -> RunProcess:
        # > A real interpreter in its own session: the test signals it, and checks nothing of its
        # > process group outlives it.
        errors = base / f"run-{uuid.uuid4().hex[:6]}.err"
        process = subprocess.Popen(  # noqa: S603  # lint-waiver: LW-190301 [S603]; the test runs its own fixed child module.
            [sys.executable, "-m", "tests.e2e._interruptible_run", str(base), *flags],
            env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errors.open("w"),
            text=True,
            start_new_session=True,
        )
        started = cls(base, process, errors)
        threading.Thread(target=started._read, daemon=True).start()
        return started

    def _read(self) -> None:
        assert self.process.stdout is not None
        for text in self.process.stdout:
            self.lines.put(json.loads(text))
        self.lines.put(None)

    def wait_for(self, matches: Callable[[_Line], bool]) -> _Line:
        """Read reports until one matches; fail if the process ends or stalls first."""
        while True:
            try:
                # test-isolation: only guards a hang; a run that reports nothing for the guard is stuck, and no event can say so.
                line = self.lines.get(timeout=SILENCE_GUARD_S)
            except queue.Empty:
                message = (
                    f"the run reported nothing for {SILENCE_GUARD_S:g} s; seen {self.seen[-5:]}"
                )
                raise AssertionError(message) from None
            if line is None:
                message = (
                    f"the run process ended; seen {self.seen[-5:]}; "
                    f"stderr tail: {self.errors.read_text()[-2000:]}"
                )
                raise AssertionError(message)
            self.seen.append(line)
            if matches(line):
                return line

    @property
    def run_id(self) -> str:
        return str(self.wait_for_seen(lambda line: "run_id" in line)["run_id"])

    def wait_for_seen(self, matches: Callable[[_Line], bool]) -> _Line:
        for line in self.seen:
            if matches(line):
                return line
        return self.wait_for(matches)

    def release(self) -> None:
        """Let the held turn go on."""
        assert self.process.stdin is not None
        self.process.stdin.write("\n")
        self.process.stdin.flush()

    def kill(self) -> None:
        """SIGKILL the run's process alone, as an out-of-memory kill or a lost host does."""
        self.process.kill()
        self.process.wait(timeout=HANG_GUARD_S)

    def finish(self) -> _Line:
        """Wait for the process to end on its own; its outcome report."""
        outcome = self.wait_for(lambda line: "outcome" in line)
        assert self.process.wait(timeout=HANG_GUARD_S) == 0, self.seen[-5:]
        outcome_fields = outcome["outcome"]
        assert isinstance(outcome_fields, dict)
        return outcome_fields

    def group_is_empty(self) -> bool:
        try:
            os.killpg(self.process.pid, 0)
        except ProcessLookupError:
            return True
        return False

    def reap_group(self) -> None:
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        self.process.wait(timeout=HANG_GUARD_S)


class Scenario:
    """One scripted project and every run process started over it."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.loop_input = LoopInput.create(
            base,
            on_disk_state=True,
            poll_interval_s=0.05,
            agent_config=f"{_AGENT_CONFIG}[agent]\nscripted_seed = {SEED}\n",
        )
        self.processes: list[RunProcess] = []

    def start(self, *flags: str) -> RunProcess:
        started = RunProcess.start(self.base, *flags)
        self.processes.append(started)
        return started

    def hold(self, at: str, kind: str) -> RunProcess:
        return self.start("--hold", f"{at}:{kind}")

    def resume(self, run_id: str, *flags: str) -> RunProcess:
        return self.start("--resume", run_id, *flags)

    def post(self, run_id: str, action: str, out: StringIO, err: StringIO) -> int:
        """``vibesys control RUN ACTION`` against this project's run."""
        return control_main(
            [run_id, "--project", str(self.loop_input.root), action],
            cwd=self.loop_input.root,
            new_control_id=lambda: time_ordered_id(OsThreads().now(), uuid.uuid4().hex[:8]),
            out=out,
            err=err,
        )

    def adopted_project_file(self) -> str:
        return (self.loop_input.root / "queue.py").read_text(encoding="utf-8")

    def hold_evaluations(self) -> None:
        """Leave every job submitted from now on pending until released."""
        self.loop_input.hold_jobs()

    def release_evaluations(self) -> None:
        release_jobs(self.loop_input.cluster)

    def wait_for_pending_job(self) -> None:
        """Wait, bounded, until an evaluation job is pending on the cluster."""
        threads = OsThreads()
        deadline = threads.now() + HANG_GUARD_S
        while not pending_jobs(self.loop_input.cluster):
            assert threads.now() < deadline, "no evaluation job reached the cluster"
            threads.sleep(0.02)

    def leftovers(self) -> list[str]:
        """What still runs after every run process ended: group members and cluster jobs."""
        found = [
            f"process group of pid {p.process.pid}"
            for p in self.processes
            if not p.group_is_empty()
        ]
        return [*found, *(f"cluster job {job}" for job in active_jobs(self.loop_input.cluster))]

    def close(self) -> None:
        for each in self.processes:
            each.reap_group()


@pytest.fixture
def scenario(tmp_path: Path) -> Iterator[Scenario]:
    created = Scenario(tmp_path)
    try:
        yield created
    finally:
        created.close()


@pytest.fixture(scope="module")
def uninterrupted(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    """What the script ends as when nothing interrupts it."""
    reference = Scenario(tmp_path_factory.mktemp("uninterrupted"))
    try:
        outcome = reference.start().finish()
        assert outcome["status"] == _COMPLETED, outcome
        return {**without_run_id(outcome), "adopted": reference.adopted_project_file()}
    finally:
        reference.close()


def without_run_id(outcome: dict[str, object]) -> dict[str, object]:
    return {name: value for name, value in outcome.items() if name != "run_id"}


def _event(kind: CoreEventType) -> Callable[[_Line], bool]:
    return lambda line: line.get("event") == kind.value


def _assert_ends_as_uninterrupted(
    scenario: Scenario, outcome: dict[str, object], uninterrupted: dict[str, object]
) -> None:
    assert outcome["failure"] is None, outcome
    assert {**without_run_id(outcome), "adopted": scenario.adopted_project_file()} == uninterrupted
    assert scenario.leftovers() == []


def _wait_until_only_evaluations_remain(scenario: Scenario, run: RunProcess) -> None:
    """Wait until the judge's step has ended and an evaluation job is pending on the held cluster.

    The input's measurement is submitted as the run starts, in parallel with the planner, so a
    pending job alone says nothing about what else is mid-step. The judge is the last agent
    turn of the one round, and its step is over once the run records it ended (its turn's
    report comes earlier), so after that the only steps left are the held evaluations.
    """
    run.wait_for(
        lambda line: (
            line.get("event") == "span_ended" and str(line.get("span", "")).endswith("/judge.1")
        )
    )
    scenario.wait_for_pending_job()


def _pause(scenario: Scenario, run: RunProcess) -> None:
    """Post ``vibesys control RUN pause`` while the run holds its step, and wait until it lands."""
    out, err = StringIO(), StringIO()
    assert scenario.post(run.run_id, "pause", out, err) == 0, err.getvalue()
    run.wait_for(_event(CoreEventType.PARK_REQUESTED))


@pytest.mark.parametrize(
    ("at", "kind"),
    [(START, PLANNER_KIND), (TURN, IMPLEMENTER_KIND)],
    ids=["planner-session-start", "agent-turn"],
)
def test_a_run_paused_in_an_agent_turn_resumes_to_the_uninterrupted_result(
    scenario: Scenario, uninterrupted: dict[str, object], at: str, kind: str
) -> None:
    first = scenario.hold(at, kind)
    first.wait_for(lambda line: line.get("held") == f"{at}:{kind}")
    _pause(scenario, first)
    first.release()
    assert first.finish()["status"] == _PARKED

    resumed = scenario.resume(first.run_id)

    _assert_ends_as_uninterrupted(scenario, resumed.finish(), uninterrupted)


def test_a_run_paused_in_an_evaluation_resumes_to_the_uninterrupted_result(
    scenario: Scenario, uninterrupted: dict[str, object]
) -> None:
    scenario.hold_evaluations()
    first = scenario.start()
    _wait_until_only_evaluations_remain(scenario, first)
    _pause(scenario, first)
    scenario.release_evaluations()
    assert first.finish()["status"] == _PARKED

    resumed = scenario.resume(first.run_id)

    _assert_ends_as_uninterrupted(scenario, resumed.finish(), uninterrupted)


@pytest.mark.xfail(
    strict=True,
    reason="#3475 recovery redesign: the resumed run stays silent on the killed turn's request",
)
def test_a_run_killed_in_an_agent_turn_resumes_to_the_uninterrupted_result(
    scenario: Scenario, uninterrupted: dict[str, object]
) -> None:
    first = scenario.hold(TURN, IMPLEMENTER_KIND)
    first.wait_for(lambda line: line.get("held") == f"{TURN}:{IMPLEMENTER_KIND}")
    first.kill()

    resumed = scenario.resume(first.run_id)

    _assert_ends_as_uninterrupted(scenario, resumed.finish(), uninterrupted)


def test_a_run_killed_in_an_evaluation_resumes_to_the_uninterrupted_result(
    scenario: Scenario, uninterrupted: dict[str, object]
) -> None:
    scenario.hold_evaluations()
    first = scenario.start()
    _wait_until_only_evaluations_remain(scenario, first)
    first.kill()
    scenario.release_evaluations()

    resumed = scenario.resume(first.run_id)

    _assert_ends_as_uninterrupted(scenario, resumed.finish(), uninterrupted)


def test_a_run_paused_again_after_it_resumed_still_resumes_to_the_uninterrupted_result(
    scenario: Scenario, uninterrupted: dict[str, object]
) -> None:
    first = scenario.hold(START, PLANNER_KIND)
    first.wait_for(lambda line: line.get("held") == f"{START}:{PLANNER_KIND}")
    _pause(scenario, first)
    first.release()
    assert first.finish()["status"] == _PARKED

    second = scenario.resume(first.run_id, "--hold", f"{TURN}:{IMPLEMENTER_KIND}")
    second.wait_for(lambda line: line.get("held") == f"{TURN}:{IMPLEMENTER_KIND}")
    _pause(scenario, second)
    second.release()
    assert second.finish()["status"] == _PARKED
    third = scenario.resume(first.run_id)

    _assert_ends_as_uninterrupted(scenario, third.finish(), uninterrupted)
