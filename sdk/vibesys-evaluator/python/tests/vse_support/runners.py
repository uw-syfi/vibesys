"""In-process implementations of the deploy interface, for the deterministic tests.

``InProcessDeployer`` stands where ``deploy.k8s`` (or the local runner)
stands: it "deploys" by seeding an in-memory service and runs each phase by
calling the phase pod's entry point in this process, with the Fake loadgen.
``InProcessGenerator`` runs the task script's ``generate`` and ``check``
commands in this process. The files crossing the interface are the real ones.
"""

from __future__ import annotations

import contextlib
import io
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from vibesys_evaluator._private.cli import Task, generator

# test-isolation: the deploy interface and the phase pod's entry point are private seams
# of the package; these Fakes implement them as deploy.k8s does.
from vibesys_evaluator._private.deploy import (
    PHASE_INPUT_FILE,
    DeployContext,
    DeployError,
    PhaseJob,
    PhaseRunError,
)
from vibesys_evaluator._private.loadgen import LoadgenExit
from vibesys_evaluator._private.phase import PhaseError, PodTask, run_pod

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from typing import IO

    from vibesys_evaluator._private.loadgen import Loadgen

#: The generator command the in-process phase pod runs (a Python task script's).
GENERATOR = ("python3", "/task/bench.py")


class Ticks:
    """A clock that advances one second per reading."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 1.0
        return self.now


@dataclass
class InProcessGenerator:
    """The task script as the generator command (``GENERATOR``), run in this process."""

    task: Task
    runs: list[list[str]] = field(default_factory=list)

    def _args(self, argv: Sequence[str]) -> list[str]:
        assert tuple(argv[: len(GENERATOR)]) == GENERATOR, argv
        self.runs.append(list(argv[len(GENERATOR) :]))
        return list(argv[len(GENERATOR) :])

    def _run(self, argv: Sequence[str], out: IO[bytes]) -> LoadgenExit:
        args = self._args(argv)
        log = io.StringIO()
        with contextlib.redirect_stderr(log):
            code = generator(self.task, args, stdout=out, clock=Ticks())
        return LoadgenExit(code=code, stderr=log.getvalue())

    def __call__(self, argv: Sequence[str], out: IO[bytes]) -> LoadgenExit:
        """``generate``, writing the stream to ``out`` (the Fake loadgen's)."""
        return self._run(argv, out)

    def run(self, argv: Sequence[str], *, timeout_secs: float) -> LoadgenExit:
        """``check`` (the phase pod's ``Commands``)."""
        assert timeout_secs > 0
        return self._run(argv, io.BytesIO())


@dataclass
class InProcessRunner:
    task: PodTask
    loadgen: Loadgen
    commands: InProcessGenerator
    jobs: list[PhaseJob] = field(default_factory=list)

    def run_phase(self, job: PhaseJob, out_dir: Path) -> None:
        self.jobs.append(job)
        with tempfile.TemporaryDirectory() as work, tempfile.TemporaryDirectory() as scratch:
            (Path(work) / PHASE_INPUT_FILE).write_bytes(job.phase_input)
            try:
                run_pod(
                    self.task,
                    work=Path(work),
                    scratch=Path(scratch),
                    target="http://app",
                    loadgen=self.loadgen,
                    commands=self.commands,
                )
            except PhaseError as error:
                msg = f"{job.name} exited 1; log tail:\n{error}"
                raise PhaseRunError(msg) from error
            finally:
                for item in Path(work).iterdir():
                    if item.name != PHASE_INPUT_FILE:
                        shutil.copy2(item, out_dir / item.name)


@dataclass
class InProcessDeployer:
    runner: InProcessRunner
    seed: Callable[[], None] = lambda: None
    fail: str = ""
    contexts: list[DeployContext] = field(default_factory=list)
    torn_down: int = 0

    @contextmanager
    def deploy(self, context: DeployContext) -> Iterator[InProcessRunner]:
        self.contexts.append(context)
        if self.fail:
            raise DeployError(self.fail)
        self.seed()
        try:
            yield self.runner
        finally:
            self.torn_down += 1
