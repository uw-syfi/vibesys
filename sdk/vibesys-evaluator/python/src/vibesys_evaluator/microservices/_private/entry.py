"""The microservices task script's command line, composed on the core sample lifecycle.

    bench.py benchmark --project-root DIR --vs-output FILE [--seed N]   one sample (vibesys)
    bench.py phase --work DIR --target URL                              one phase (a phase pod)
    bench.py generate --phase-input F --dataset F --state DIR           the generator (under loadgen)
    bench.py seed --dataset-digest HEX                                  the seed (a seed Job)
    bench.py ctl ARGS...                                                deploy hooks (accuracy)

``benchmark`` is the core's benchmark sample (records, seed, signals) whose
body deploys the system, runs ``benchmark(run)`` on a microservices ``Run``
and tears the system down on every path. ``phase`` writes the dataset file
and hands the phase to the core (``vibesys-evaluator phase pod``), which runs
loadgen and the generator.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

from vibesys_evaluator._private import core
from vibesys_evaluator._private.files import SchemaFileError, read_message
from vibesys_evaluator._private.sample import benchmark_sample, log, with_signals
from vibesys_evaluator._proto.vibesys.evaluator.v1 import phase_pb
from vibesys_evaluator.microservices._private import dataset as dataset_file
from vibesys_evaluator.microservices._private.deploy import DeployContext, TaskCommand
from vibesys_evaluator.microservices._private.phase import PhaseError, generate
from vibesys_evaluator.microservices._private.run import PhaseDriver, new_run
from vibesys_evaluator.microservices._private.workload import Generator

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from typing import IO

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
    workload: Workload[Any, Any] | Generator
    benchmark: Callable[[Run], None]
    metrics: Mapping[str, Metric]
    validity: Validity
    capture: bool = False


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
    generate_command = commands.add_parser("generate", help="the request generator (in a phase)")
    generate_command.add_argument("--phase-input", type=Path, required=True)
    generate_command.add_argument("--dataset", type=Path, required=True)
    generate_command.add_argument("--state", type=Path, required=True)
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
            validity=task.validity,
            log=log,
            capture=task.capture,
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


def generator_command(task: Task, script: TaskCommand) -> list[str]:
    """The generator command of the task's workload, as the phase pod runs it."""
    if not isinstance(task.workload, Generator):
        return [script.python, script.script]
    program, *rest = task.workload.command
    if "/" in program and not Path(program).is_absolute():
        program = str(Path(script.script).parent / program)
    return [program, *rest]


def phase(task: Task, *, work: Path, target: str, script: TaskCommand) -> int:
    """Run one phase here (the phase pod's command): the core's ``phase pod``."""
    with tempfile.TemporaryDirectory(prefix="vibesys-phase-") as scratch:
        try:
            encoded = dataset_file.encode(task.dataset())
        except Exception as error:
            log(f"phase failed: dataset raised {type(error).__name__}: {error}")
            return 1
        dataset_path = Path(scratch) / "dataset.json"
        dataset_path.write_bytes(encoded)
        try:
            loadgen = core.find_binary("loadgen")
        except core.CoreNotFoundError as error:
            log(f"phase failed: {error}")
            return 1
        request = {
            "work_dir": str(work),
            "target": target,
            "generator": generator_command(task, script),
            "dataset_file": str(dataset_path),
            "min_sustained_ratio": task.validity.min_sustained_ratio,
            "loadgen": loadgen,
        }
        try:
            core.stream(
                "phase", "pod", request, on_event=lambda event: log(str(event.get("message", "")))
            )
        except core.CoreError as error:
            log(f"phase failed: {error}")
            return 1
    return 0


def generator(task: Task, argv: Sequence[str], *, stdout: IO[bytes]) -> int:
    """The Python workload's ``generate`` (``docs/spec.md``, "Generator command line").

    ``argv`` starts with the command name. The dataset is ``task.dataset()``,
    built here (``--dataset`` is its file, for generators in other languages).
    Returns the exit status.
    """
    args = _parser().parse_args(list(argv))
    if args.command != "generate":
        log(f"not a generator command: {args.command}")
        return 2
    if isinstance(task.workload, Generator):
        log(f"the workload is the generator {list(task.workload.command)!r}: run it instead")
        return 2
    try:
        phase_input = read_message(args.phase_input, phase_pb.PhaseInput)
        written = generate(task.workload, task.dataset(), phase_input, out=stdout, state=args.state)
    except (PhaseError, SchemaFileError) as error:
        log(f"generate failed: {error}")
        return 1
    log(f"generate: the stream was closed after {written} operations")
    return 0


def _generator_on_stdout(task: Task, argv: Sequence[str]) -> int:
    """``generator`` on this process's standard output, which loadgen closes to end the stream."""
    stdout = sys.stdout.buffer
    try:
        code = generator(task, argv, stdout=stdout)
        stdout.flush()
    except BrokenPipeError:
        code = 0
    if code == 0:
        # The buffer holds operations loadgen will never read: drop them, so the
        # interpreter's own flush at exit does not fail on the closed pipe.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        os.close(devnull)
    return code


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
            return phase(task, work=args.work, target=args.target, script=script)
        if args.command == "generate":
            return _generator_on_stdout(task, argv)
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
