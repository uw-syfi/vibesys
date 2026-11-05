"""The local deploy kind: phases as child processes against a system already running here.

``LocalDeployer`` deploys nothing: the system under test is started by
whoever runs the benchmark (a test, a developer), at ``target``. It runs the
seed command and each phase command as child processes on this machine, with
the same files and command lines a Kubernetes phase pod gets, so the whole
path from ``benchmark(run)`` to loadgen and back runs without a cluster.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from vibesys_evaluator._private.deploy import (
    PHASE_INPUT_FILE,
    PHASE_LOG_FILE,
    DeployError,
    PhaseRunError,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from vibesys_evaluator._private.deploy import DeployContext, PhaseJob, TaskCommand

_LOG_TAIL = 3000
_SEED_TIMEOUT_SECS = 600.0


def _tail(path: Path) -> str:
    try:
        return path.read_text(errors="replace")[-_LOG_TAIL:]
    except OSError:
        return ""


class LocalPhaseRunner:
    """Runs each phase command as a child process in a fresh work directory."""

    def __init__(
        self, *, task: TaskCommand, target: str, env: Mapping[str, str] | None = None
    ) -> None:
        """Run ``task`` against ``target``, with ``env`` as the child's environment (inherited if None)."""
        self._task = task
        self._target = target
        self._env = dict(env) if env is not None else None

    def run_phase(self, job: PhaseJob, out_dir: Path) -> None:
        """Run the phase; copy its work directory into ``out_dir``."""
        log = out_dir / PHASE_LOG_FILE
        with tempfile.TemporaryDirectory(prefix=f"vse-{job.name}-") as scratch:
            work = Path(scratch)
            (work / PHASE_INPUT_FILE).write_bytes(job.phase_input)
            command = self._task.phase(work_dir=str(work), target=self._target)
            with log.open("wb") as output:
                try:
                    done = subprocess.run(
                        command,
                        stdout=output,
                        stderr=subprocess.STDOUT,
                        env=self._env,
                        timeout=job.timeout_secs,
                        check=False,
                    )
                except subprocess.TimeoutExpired as error:
                    msg = f"{job.name} ran past {job.timeout_secs:.0f}s; log tail:\n{_tail(log)}"
                    raise PhaseRunError(msg) from error
                except OSError as error:
                    msg = f"{job.name}: cannot start {command[0]!r}: {error.strerror}"
                    raise PhaseRunError(msg) from error
            for item in work.iterdir():
                if item.is_file() and item.name != PHASE_INPUT_FILE:
                    shutil.copy2(item, out_dir / item.name)
            if done.returncode != 0:
                msg = f"{job.name} exited {done.returncode}; log tail:\n{_tail(log)}"
                raise PhaseRunError(msg)


class LocalDeployer:
    """Uses a system already running at ``target``: runs the seed, then hands out a local runner."""

    def __init__(self, *, target: str, env: Mapping[str, str] | None = None) -> None:
        """Seed and run phases against ``target``."""
        self._target = target
        self._env = env

    @contextmanager
    def deploy(self, context: DeployContext) -> Iterator[LocalPhaseRunner]:
        """Run the seed command, then yield the runner (nothing to tear down)."""
        log = context.artifacts / "seed.log"
        command = context.task.seed(dataset_digest=context.dataset_digest)
        with log.open("wb") as output:
            try:
                done = subprocess.run(
                    command,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    env=dict(self._env) if self._env else None,
                    timeout=_SEED_TIMEOUT_SECS,
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                msg = f"the seed ran past {_SEED_TIMEOUT_SECS:.0f}s; log tail:\n{_tail(log)}"
                raise DeployError(msg) from error
        if done.returncode != 0:
            msg = f"the seed exited {done.returncode}; log tail:\n{_tail(log)}"
            raise DeployError(msg)
        context.log(f"local system at {self._target}: seeded")
        yield LocalPhaseRunner(task=context.task, target=self._target, env=self._env)
