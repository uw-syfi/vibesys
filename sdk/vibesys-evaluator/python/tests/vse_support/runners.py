"""In-process implementations of the deploy interface, for the deterministic tests.

``InProcessDeployer`` stands where ``microservices.k8s`` (or the local runner)
stands: it "deploys" by seeding an in-memory service and runs each phase by
calling the phase pod's entry point in this process, with the Fake loadgen.
The files crossing the interface are the real ones.
"""

from __future__ import annotations

import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

# test-isolation: the deploy interface and the phase pod's entry point are private seams
# of the package; these Fakes implement them as microservices.k8s does.
from vibesys_evaluator.microservices._private.deploy import (
    PHASE_INPUT_FILE,
    DeployContext,
    DeployError,
    PhaseJob,
    PhaseRunError,
)
from vibesys_evaluator.microservices._private.phase import PhaseError, PodTask, run_pod

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from vibesys_evaluator._private.loadgen import Loadgen


class Ticks:
    """A clock that advances one second per reading."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 1.0
        return self.now


@dataclass
class InProcessRunner:
    task: PodTask
    loadgen: Loadgen
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
                    clock=Ticks(),
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
