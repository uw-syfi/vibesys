"""In-process implementations of the deploy interface, for the deterministic tests.

``InProcessDeployer`` stands where ``microservices.k8s`` (or the local runner)
stands: it "deploys" by seeding an in-memory service and runs each phase in
this process: the Fake loadgen with the command line the core's ``phase pod``
gives loadgen, then the files the pod leaves. ``InProcessGenerator`` runs the
task script's ``generate`` in this process. The files crossing the interface
are the real ones; the core judges them (``phase judge``).
"""

from __future__ import annotations

import contextlib
import gzip
import io
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from vibesys_evaluator._private.files import read_summary, write_message
from vibesys_evaluator._private.rand import Rand
from vibesys_evaluator._proto.vibesys.evaluator.v1 import loadgen_pb as pb
from vibesys_evaluator._proto.vibesys.evaluator.v1 import phase_pb

# test-isolation: the deploy interface and the generator's entry point are private seams
# of the package; these Fakes implement them as microservices.k8s does.
from vibesys_evaluator.microservices._private.deploy import (
    PHASE_INPUT_FILE,
    PHASE_OUTPUT_FILE,
    DeployContext,
    DeployError,
    PhaseJob,
    PhaseRunError,
)
from vibesys_evaluator.microservices._private.entry import Task, generator

from .fake_loadgen import FakeLoadgen, LoadgenExit

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from typing import IO

#: The generator command the in-process phase pod runs (a Python task script's).
GENERATOR = ("python3", "/task/bench.py")
KEPT_FILE = "kept.binpb"


@dataclass
class InProcessGenerator:
    """The task script as the generator command (``GENERATOR``), run in this process."""

    task: Task
    runs: list[list[str]] = field(default_factory=list)

    def __call__(self, argv: Sequence[str], out: IO[bytes]) -> LoadgenExit:
        """``generate``, writing the stream to ``out`` (the Fake loadgen's)."""
        assert tuple(argv[: len(GENERATOR)]) == GENERATOR, argv
        args = list(argv[len(GENERATOR) :])
        self.runs.append(args)
        log = io.StringIO()
        with contextlib.redirect_stderr(log):
            code = generator(self.task, args, stdout=out)
        return LoadgenExit(code=code, stderr=log.getvalue())


def _repr(value: float) -> str:
    return repr(float(value))


def loadgen_args(
    phase: phase_pb.PhaseInput,
    *,
    results: Path,
    summary: Path,
    min_sustained: float,
    generator: Sequence[str],
) -> list[str]:
    """loadgen's command line for the phase, as the core's ``phase pod`` writes it."""
    common = [
        "--secs",
        _repr(phase.secs),
        "--warmup",
        _repr(phase.warmup_secs),
        "--results",
        str(results),
        "--summary",
        str(summary),
        "--timeout",
        "10.0",
    ]
    if phase.capture:
        common.append("--record-clients")
    if phase.model == pb.LoadModel.CLOSED:
        head = ["closed", "--target", "http://app", "--clients", str(phase.clients)]
    else:
        seed = Rand(phase.sample_seed, "phase", phase.phase_index).child("arrivals").below(1 << 63)
        mode = "poisson" if phase.arrivals == pb.Arrivals.POISSON else "uniform"
        head = [
            "open",
            "--target",
            "http://app",
            "--rate",
            _repr(phase.rate),
            "--arrivals",
            mode,
            "--seed",
            str(seed),
            "--min-sustained",
            _repr(min_sustained),
        ]
    return [*head, *common, "--", *generator]


@dataclass
class InProcessRunner:
    """Runs each phase as the core's ``phase pod`` does, with the Fake loadgen, in this process."""

    loadgen: FakeLoadgen
    min_sustained_ratio: float
    jobs: list[PhaseJob] = field(default_factory=list)

    def run_phase(self, job: PhaseJob, out_dir: Path) -> None:
        self.jobs.append(job)
        with (
            tempfile.TemporaryDirectory() as work_dir,
            tempfile.TemporaryDirectory() as scratch_dir,
        ):
            work, scratch = Path(work_dir), Path(scratch_dir)
            (work / PHASE_INPUT_FILE).write_bytes(job.phase_input)
            phase = phase_pb.PhaseInput.from_binary(job.phase_input)
            state = scratch / "state"
            state.mkdir()
            results = (work if phase.capture else scratch) / "results.binpb"
            command = [
                *GENERATOR,
                "generate",
                "--phase-input",
                str(work / PHASE_INPUT_FILE),
                "--dataset",
                str(scratch / "dataset.json"),
                "--state",
                str(state),
            ]
            args = loadgen_args(
                phase,
                results=results,
                summary=work / "summary.binpb",
                min_sustained=self.min_sustained_ratio,
                generator=command,
            )
            try:
                done = self.loadgen.run(args, timeout_secs=job.timeout_secs)
                if done.code != 0:
                    msg = (
                        f"{job.name} exited 1; log tail:\nloadgen exited {done.code}: {done.stderr}"
                    )
                    raise PhaseRunError(msg)
                if phase.capture:
                    shutil.copy2(state / KEPT_FILE, work / KEPT_FILE)
                else:
                    with (
                        results.open("rb") as raw,
                        gzip.open(work / "results.binpb.gz", "wb") as out,
                    ):
                        shutil.copyfileobj(raw, out)
                    results.unlink()
                summary = read_summary(work / "summary.binpb")
                write_message(
                    work / PHASE_OUTPUT_FILE,
                    phase_pb.PhaseOutput(summary=summary, loadgen_secs=1.0),
                )
            finally:
                for item in work.iterdir():
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
