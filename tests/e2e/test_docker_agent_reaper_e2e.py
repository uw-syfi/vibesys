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
from vs_sandbox.api import RUN_ID_LABEL, STEP_TAG_ENV
from vs_sandbox.fake_docker_engine import FakeDockerEngine
from vs_sim.api import DISCARD, SubprocessPipedProcessLauncher
from vs_sim.api.testing import InlineBlockingRunner, VirtualClock, run_virtual

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="the sweep reads /proc inside the container"
)

_RUN = "run-a"
_SLEEP = "import time; time.sleep(600)"
_EXIT_BOUND_S = 30.0
"""Upper bound on a killed process being reaped by its parent; reached only on a hang."""


@contextmanager
def _marked_process(tag: str) -> Iterator[subprocess.Popen[bytes]]:
    process = subprocess.Popen(  # noqa: S603  # lint-waiver: LW-914333 [S603]; a real-tier test starts its own fixed argv.
        [sys.executable, "-I", "-c", _SLEEP],
        env={**os.environ, STEP_TAG_ENV: tag},
        start_new_session=True,
    )
    try:
        yield process
    finally:
        process.kill()
        process.wait(_EXIT_BOUND_S)


def test_the_sweep_ends_the_processes_of_one_step_and_no_other() -> None:
    with (
        tempfile.TemporaryDirectory() as raw,
        _marked_process("step-a") as target,
        _marked_process("step-ab") as longer,
        _marked_process("other") as other,
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
