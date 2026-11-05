"""The microservices task script's command line, composed on the core sample lifecycle.

    bench.py benchmark --project-root DIR --vs-output FILE [--seed N]   one sample (vibesys)
    bench.py phase --work DIR --target URL                              one phase (a phase pod)
    bench.py seed --dataset-digest HEX                                  the seed (a seed Job)
    bench.py ctl ARGS...                                                deploy hooks (accuracy)

``benchmark`` is the core's benchmark sample (records, seed, signals) whose
body deploys the system, runs ``benchmark(run)`` on a microservices ``Run``
and tears the system down on every path.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

from vibesys_evaluator._private.loadgen import ProcessLoadgen
from vibesys_evaluator._private.sample import benchmark_sample, log, with_signals
from vibesys_evaluator.microservices._private import dataset as dataset_file
from vibesys_evaluator.microservices._private.deploy import DeployContext, TaskCommand
from vibesys_evaluator.microservices._private.phase import PhaseError, PodTask, run_pod
from vibesys_evaluator.microservices._private.run import PhaseDriver, new_run

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from vibesys_evaluator._private.results import Metric, Validity
    from vibesys_evaluator._private.sample import Sample
    from vibesys_evaluator.microservices._private.deploy import Deployer
    from vibesys_evaluator.microservices._private.run import Run
    from vibesys_evaluator.microservices._private.workload import Workload
    from vibesys_evaluator.microservices.k8s import App


@dataclass(frozen=True, kw_only=True)
class Task:
    """Everything the task script passes to ``microservices.main``."""

    dataset: Callable[[], Any]
    system: App[Any]
    workload: Workload[Any, Any]
    benchmark: Callable[[Run], None]
    metrics: Mapping[str, Metric]
    validity: Validity


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bench.py", description="A vibesys-evaluator benchmark.")
    commands = parser.add_subparsers(dest="command", required=True)
    bench = commands.add_parser("benchmark", help="measure one sample")
    bench.add_argument("--project-root", type=Path, required=True)
    bench.add_argument("--vs-output", type=Path, required=True)
    bench.add_argument("--seed", type=int, default=None, help="replay the sample with this seed")
    phase = commands.add_parser("phase", help="run one phase (in a phase pod)")
    phase.add_argument("--work", type=Path, required=True)
    phase.add_argument("--target", required=True)
    seed = commands.add_parser("seed", help="write the dataset into the system (in a seed Job)")
    seed.add_argument("--dataset-digest", required=True)
    ctl = commands.add_parser("ctl", help="the deploy layer's hooks for accuracy checkers")
    ctl.add_argument("args", nargs=argparse.REMAINDER)
    return parser


def _deploy_and_run(
    task: Task, sample: Sample, *, project_root: Path, deployer: Deployer, script: TaskCommand
) -> None:
    encoded = dataset_file.encode(task.dataset())
    digest = dataset_file.digest(encoded)
    (sample.artifacts / "dataset.json").write_bytes(encoded)
    log(f"dataset {digest[:16]} ({len(encoded)} bytes)")
    context = DeployContext(
        project_root=project_root.resolve(),
        artifacts=sample.artifacts,
        task=script,
        dataset_digest=digest,
        log=log,
    )
    with deployer.deploy(context) as runner:
        driver = PhaseDriver(
            runner=runner,
            sample=sample,
            operations=[op.name for op in task.workload.operations],
            validity=task.validity,
            log=log,
        )
        task.benchmark(new_run(sample, driver))


def benchmark(
    task: Task,
    *,
    project_root: Path,
    output: Path,
    seed_flag: int | None,
    deployer: Deployer,
    env: Mapping[str, str],
    script: TaskCommand,
) -> int:
    """Measure one sample; write its records to ``output``. Returns the exit status."""
    return benchmark_sample(
        metrics=task.metrics,
        output=output,
        seed_flag=seed_flag,
        env=env,
        body=lambda sample: _deploy_and_run(
            task, sample, project_root=project_root, deployer=deployer, script=script
        ),
    )


def seed(task: Task, *, expected_digest: str) -> int:
    """Regenerate the dataset, check it is the orchestrator's, and seed the system."""
    encoded = dataset_file.encode(task.dataset())
    digest = dataset_file.digest(encoded)
    if digest != expected_digest:
        log(
            f"the dataset here is {digest[:16]}, the orchestrator's {expected_digest[:16]}: "
            "dataset() must be a pure function of the task's constants"
        )
        return 1
    if task.system.seed is None:
        log("no seed function: nothing to write")
        return 0
    task.system.seed(task.dataset())
    log(f"seeded dataset {digest[:16]}")
    return 0


def phase(task: Task, *, work: Path, target: str) -> int:
    """Run one phase in this process (the phase pod's command)."""
    with tempfile.TemporaryDirectory(prefix="vibesys-phase-") as scratch:
        try:
            run_pod(
                PodTask(
                    dataset=task.dataset,
                    workload=task.workload,
                    min_sustained_ratio=task.validity.min_sustained_ratio,
                ),
                work=work,
                scratch=Path(scratch),
                target=target,
                loadgen=ProcessLoadgen(),
                clock=time.monotonic,
            )
        except PhaseError as error:
            log(f"phase failed: {error}")
            return 1
    return 0


def run(
    task: Task,
    argv: Sequence[str],
    *,
    deployer: Callable[[], Deployer],
    control: Callable[[Sequence[str]], int],
    env: Mapping[str, str],
    script: TaskCommand,
) -> int:
    """Run one command of the task script; returns the exit status."""
    args = _parser().parse_args(list(argv))

    def command() -> int:
        if args.command == "benchmark":
            return benchmark(
                task,
                project_root=args.project_root,
                output=args.vs_output,
                seed_flag=args.seed,
                deployer=deployer(),
                env=env,
                script=script,
            )
        if args.command == "phase":
            return phase(task, work=args.work, target=args.target)
        if args.command == "seed":
            return seed(task, expected_digest=args.dataset_digest)
        return control(args.args)

    return with_signals(command)


def main(
    task: Task,
    *,
    deployer: Callable[[], Deployer],
    control: Callable[[Sequence[str]], int],
    env: Mapping[str, str],
) -> NoReturn:
    """Run the command in ``sys.argv`` and exit with its status."""
    script = TaskCommand(python=sys.executable, script=str(Path(sys.argv[0]).resolve()))
    sys.exit(run(task, sys.argv[1:], deployer=deployer, control=control, env=env, script=script))
