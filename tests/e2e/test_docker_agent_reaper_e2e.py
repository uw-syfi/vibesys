"""The Docker agent reaper ends real processes by the step's tag, through the real sweep script.

``FakeDockerEngine`` runs a ``docker exec`` program on this host, so the sweep script
(``vs_agent.reap_step_agents``) runs for real over ``/proc``: it must kill the processes whose
initial environment is exactly the step's mark, and nothing else. Linux only.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from vs_core.api import RequestId
from vs_runtime.api.docker_agent_work_reaper import DockerAgentWorkReaper
from vs_runtime.api.stage_marks import agent_mark
from vs_runtime.api.step_runner import ClearUnavailableError
from vs_sandbox.api import RUN_ID_LABEL, STEP_TAG_ENV
from vs_sandbox.fake_docker_engine import FakeDockerEngine
from vs_sim.api import DISCARD, SubprocessPipedProcessLauncher
from vs_sim.api.testing import InlineBlockingRunner, VirtualClock, run_virtual

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from vs_sim.api import OutputSink

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="the sweep reads /proc inside the container"
)

_RUN = "run-a"
_SLEEP = "import time; time.sleep(600)"
_EXIT_BOUND_S = 30.0
"""Upper bound on a killed process being reaped by its parent; reached only on a hang."""


@contextmanager
def _marked_process(mark: str) -> Iterator[subprocess.Popen[bytes]]:
    """A process carrying *mark* in its environment, as an agent's process carries its step's."""
    process = subprocess.Popen(  # noqa: S603  # lint-waiver: LW-914333 [S603]; a real-tier test starts its own fixed argv.
        [sys.executable, "-I", "-c", _SLEEP],
        env={**os.environ, STEP_TAG_ENV: mark},
        start_new_session=True,
    )
    try:
        yield process
    finally:
        process.kill()
        process.wait(_EXIT_BOUND_S)


def _mark(tag: str, run: str = _RUN) -> str:
    """What the agent processes of step *tag* of run *run* carry: the mark the reaper sweeps for."""
    return agent_mark(run, RequestId(root=tag))


def test_the_sweep_ends_the_processes_of_one_step_and_no_other() -> None:
    with (
        tempfile.TemporaryDirectory() as raw,
        _marked_process(_mark("step-a")) as target,
        _marked_process(_mark("step-ab")) as longer,
        _marked_process(_mark("other")) as other,
        _marked_process(_mark("step-a", run="run-b")) as another_run,
        _marked_process("step-a") as bare,
    ):
        engine = FakeDockerEngine(Path(raw), launcher=SubprocessPipedProcessLauncher())
        engine.run(
            ["docker", "run", "-d", "--label", f"{RUN_ID_LABEL}={_RUN}", "image"],
            timeout_seconds=_EXIT_BOUND_S,
            output=DISCARD,
        )
        reaper = DockerAgentWorkReaper(_RUN, engine, InlineBlockingRunner(), lambda _line: None)

        run_virtual(VirtualClock(0.0), reaper.reap(RequestId(root="step-a")))

        assert target.wait(_EXIT_BOUND_S) == -9
        assert longer.poll() is None
        assert other.poll() is None
        assert another_run.poll() is None, "the same tag of another run is not this run's work"
        assert bare.poll() is None, "a bare tag, with no run, is not a mark of any step"


class _EngineWhoseSweepCannotKill(FakeDockerEngine):
    """A daemon whose ``docker exec`` of the sweep runs with a ``kill`` that fails, as it does for
    a process that is not the sweep's to signal. The sweep script is the one the reaper sent."""

    def run(
        self, argv: Sequence[str], *, timeout_seconds: float, output: OutputSink
    ) -> subprocess.CompletedProcess[str]:
        if argv[:2] == ["docker", "exec"] and "-c" in argv:
            script = argv.index("-c") + 1
            argv = [*argv[:script], "kill() { return 1; }; " + argv[script], *argv[script + 1 :]]
        return super().run(argv, timeout_seconds=timeout_seconds, output=output)


def test_a_process_the_sweep_cannot_kill_is_never_reported_clear() -> None:
    """Regression: the sweep counted the kills that worked, so a failed kill read as nothing found."""
    with tempfile.TemporaryDirectory() as raw, _marked_process(_mark("step-a")) as target:
        engine = _EngineWhoseSweepCannotKill(Path(raw), launcher=SubprocessPipedProcessLauncher())
        engine.run(
            ["docker", "run", "-d", "--label", f"{RUN_ID_LABEL}={_RUN}", "image"],
            timeout_seconds=_EXIT_BOUND_S,
            output=DISCARD,
        )
        reaper = DockerAgentWorkReaper(_RUN, engine, InlineBlockingRunner(), lambda _line: None)

        with pytest.raises(ClearUnavailableError):
            run_virtual(VirtualClock(0.0), reaper.reap(RequestId(root="step-a")))

        assert target.poll() is None
