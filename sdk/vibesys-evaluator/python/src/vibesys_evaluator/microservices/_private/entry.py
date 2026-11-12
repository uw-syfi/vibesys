"""The microservices task script's command line, composed on the core sample lifecycle.

    bench.py benchmark --project-root DIR --vs-output FILE [--seed N]   one sample (vibesys)
    bench.py phase [--accuracy] --work DIR --target URL                 one phase (a phase pod)
    bench.py generate --phase-input F --dataset F --state DIR           the generator (under loadgen)
    bench.py accuracy-workload generate ...                             Accuracy(workload=)'s generator
    bench.py seed --dataset-digest HEX                                  the seed (a seed Job)
    bench.py accuracy [--project-root DIR] [--out DIR] [--seed N]      the accuracy check
    bench.py ctl ARGS...                                                deploy hooks (accuracy)
    bench.py trace [--project-root DIR] [--out DIR] [--seed N]         a trace (never scored)

``benchmark`` is the core's benchmark sample (records, seed, signals) whose
body deploys the system, runs ``benchmark(run)`` on a microservices ``Run``
and tears the system down on every path. ``phase`` writes the dataset file
and hands the phase to the core (``vibesys-evaluator phase pod``), which runs
loadgen and the generator. ``accuracy`` hands the task's ``Accuracy`` to the
core (``vibesys-evaluator accuracy run``): phases with the history captured
and faults between them, then the oracle's verdict as the exit status. Its
phase pods run ``phase --accuracy``, whose generator is the check's own
workload when it has one (``accuracy-workload generate`` for a Python one).
``trace`` is the core's trace sample (no records) whose body deploys the
system as ``benchmark`` does, instruments it with stitch, runs the task's
``Trace`` body and writes stitch's reports (``_private/trace.py``).
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

from vibesys_evaluator._private import core
from vibesys_evaluator._private.files import SchemaFileError, read_message
from vibesys_evaluator._private.sample import (
    Mode,
    SampleError,
    benchmark_sample,
    log,
    unscored_sample,
    with_signals,
)
from vibesys_evaluator._proto.vibesys.evaluator.v1 import phase_pb
from vibesys_evaluator.microservices._private import accuracy as accuracy_check
from vibesys_evaluator.microservices._private import dataset as dataset_file
from vibesys_evaluator.microservices._private import trace as tracing
from vibesys_evaluator.microservices._private.deploy import (
    DeployContext,
    NamespacedRunner,
    TaskCommand,
)
from vibesys_evaluator.microservices._private.phase import PhaseError, generate
from vibesys_evaluator.microservices._private.run import PhaseDriver, new_run
from vibesys_evaluator.microservices._private.workload import Generator

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence
    from typing import IO

    from vibesys_evaluator._private.results import Metric, Validity
    from vibesys_evaluator._private.sample import Sample
    from vibesys_evaluator.microservices._private.deploy import Deployer, PhaseRunner
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
    accuracy: accuracy_check.Accuracy | None = None
    trace: tracing.Trace | None = None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bench.py", description="A vibesys-evaluator benchmark.")
    commands = parser.add_subparsers(dest="command", required=True)
    bench = commands.add_parser("benchmark", help="measure one sample")
    bench.add_argument("--project-root", type=Path, required=True)
    bench.add_argument("--vs-output", type=Path, required=True)
    bench.add_argument("--seed", type=int, default=None, help="replay the sample with this seed")
    phase = commands.add_parser("phase", help="run one phase (in a phase pod)")
    phase.add_argument(
        "--accuracy", action="store_true", help="an accuracy check's phase: its workload"
    )
    phase.add_argument("--work", type=Path, required=True)
    phase.add_argument("--target", required=True)
    generate_command = commands.add_parser("generate", help="the request generator (in a phase)")
    generate_command.add_argument("--phase-input", type=Path, required=True)
    generate_command.add_argument("--dataset", type=Path, required=True)
    generate_command.add_argument("--state", type=Path, required=True)
    accuracy_workload = commands.add_parser(
        "accuracy-workload", help="the accuracy check's own generator: accuracy-workload generate"
    )
    accuracy_workload.add_argument("args", nargs=argparse.REMAINDER)
    check = commands.add_parser(
        "accuracy", help="run the accuracy check (exit 0 PASS, 1 FAIL, 2 UNKNOWN)"
    )
    check.add_argument("--project-root", type=Path, default=Path())
    check.add_argument("--out", type=Path, default=None, help="the report and the history")
    check.add_argument("--seed", type=int, default=None, help="replay the check with this seed")
    seed = commands.add_parser("seed", help="write the dataset into the system (in a seed Job)")
    seed.add_argument("--dataset-digest", required=True)
    ctl = commands.add_parser("ctl", help="the deploy layer's hooks for accuracy checkers")
    ctl.add_argument("args", nargs=argparse.REMAINDER)
    trace = commands.add_parser(
        "trace", help="run the task's trace: spans of a short load, analysed (never scored)"
    )
    trace.add_argument("--project-root", type=Path, default=Path())
    trace.add_argument(
        "--out", type=Path, default=None, help="the reports (default: trace/ in the artifacts)"
    )
    trace.add_argument("--seed", type=int, default=None, help="replay the trace with this seed")
    return parser


@contextmanager
def _deployed(
    task: Task, sample: Sample, *, project_root: Path, deployer: Deployer, script: TaskCommand
) -> Iterator[PhaseRunner]:
    """The system deployed for ``sample`` (the dataset written into its artifacts first)."""
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
        yield runner


def _deploy_and_run(
    task: Task, sample: Sample, *, project_root: Path, deployer: Deployer, script: TaskCommand
) -> None:
    with _deployed(
        task, sample, project_root=project_root, deployer=deployer, script=script
    ) as runner:
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


def accuracy(
    task: Task,
    *,
    project_root: Path,
    out: Path | None,
    seed_flag: int | None,
    deployer: Deployer,
    env: Mapping[str, str],
    script: TaskCommand,
) -> int:
    """Run the task's accuracy check; the exit status is the verdict's (2, UNKNOWN, without one)."""
    if task.accuracy is None:
        log("this task has no accuracy check: pass accuracy=Accuracy(...) to microservices.main")
        return accuracy_check.UNKNOWN_EXIT
    return accuracy_check.run_accuracy(
        task.accuracy,
        dataset=task.dataset(),
        app=task.system,
        project_root=project_root,
        out=out if out is not None else accuracy_check.report_dir(env),
        seed=seed_flag,
        env=env,
        deployer=deployer,
        script=script,
    )


def _deploy_and_trace(
    task: Task,
    trace_spec: tracing.Trace,
    sample: Sample,
    *,
    stitch_binary: str,
    deployments: Sequence[str],
    out: Path | None,
    project_root: Path,
    deployer: Deployer,
    script: TaskCommand,
) -> str | None:
    reports = out if out is not None else sample.artifacts / tracing.TRACE_DIR
    reports.mkdir(parents=True, exist_ok=True)
    stitch = tracing.Stitch(stitch_binary, reports / tracing.LOG_FILE)
    try:
        with _deployed(
            task, sample, project_root=project_root, deployer=deployer, script=script
        ) as runner:
            if not isinstance(runner, NamespacedRunner):
                return tracing.incomplete(
                    reports,
                    [
                        "bench.py trace instruments an application on Kubernetes (a "
                        "microservices.k8s.App, deployed in a namespace of its own); this deploy "
                        "has no namespace"
                    ],
                    log,
                )
            driver = PhaseDriver(
                runner=runner, sample=sample, validity=task.validity, log=log, measured=False
            )
            failed = tracing.run_trace(
                trace_spec,
                stitch=stitch,
                namespace=runner.namespace,
                deployments=deployments,
                out=reports,
                rollout_secs=task.system.wait_secs,
                body=lambda: trace_spec.body(new_run(sample, driver)),
                windows=lambda: driver.windows,
                log=log,
            )
            if failed is not None:
                # Leave the deploy as failed, so its teardown keeps the pods' state and logs
                # (pods.json, logs/) beside the trace's README.
                _fail_the_deploy(failed)
            return None
    except _TraceStepError as failure:
        return failure.reason
    except SampleError as error:
        return tracing.incomplete(reports, [f"the deploy failed: {error}"], log)


class _TraceStepError(Exception):
    """A trace step failed after the deploy; raised through the deploy so it tears down as failed."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _fail_the_deploy(reason: str) -> NoReturn:
    raise _TraceStepError(reason)


def trace(
    task: Task,
    *,
    project_root: Path,
    out: Path | None,
    seed_flag: int | None,
    deployer: Deployer,
    env: Mapping[str, str],
    script: TaskCommand,
) -> int:
    """Run the task's trace: exit 0 with stitch's reports written, 1 when a step failed, 2 without one."""
    if task.trace is None:
        log("this task has no trace: pass trace=Trace(body=...) to microservices.main")
        return 2
    trace_spec = task.trace
    try:
        deployments = tracing.traced_deployments(trace_spec, task.system)
    except ValueError as error:
        log(str(error))
        return 2
    stitch_binary = core.find_binary("stitch")
    return unscored_sample(
        Mode.TRACE,
        seed_flag=seed_flag,
        env=env,
        body=lambda sample: _deploy_and_trace(
            task,
            trace_spec,
            sample,
            stitch_binary=stitch_binary,
            deployments=deployments,
            out=out,
            project_root=project_root,
            deployer=deployer,
            script=script,
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


def _workload(task: Task, *, accuracy: bool) -> Workload[Any, Any] | Generator:
    """The workload of a phase: the accuracy check's own in its phases, if it has one."""
    if accuracy and task.accuracy is not None and task.accuracy.workload is not None:
        return task.accuracy.workload
    return task.workload


def generator_command(task: Task, script: TaskCommand, *, accuracy: bool = False) -> list[str]:
    """The generator command of the phase's workload, as the phase pod runs it."""
    workload = _workload(task, accuracy=accuracy)
    if not isinstance(workload, Generator):
        if workload is not task.workload:
            return [script.python, script.script, "accuracy-workload"]
        return [script.python, script.script]
    program, *rest = workload.command
    if "/" in program and not Path(program).is_absolute():
        program = str(Path(script.script).parent / program)
    return [program, *rest]


def phase(
    task: Task, *, work: Path, target: str, script: TaskCommand, accuracy: bool = False
) -> int:
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
            "generator": generator_command(task, script, accuracy=accuracy),
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


def generator(task: Task, argv: Sequence[str], *, stdout: IO[bytes], accuracy: bool = False) -> int:
    """The Python workload's ``generate`` (``docs/spec.md``, "Generator command line").

    ``argv`` starts with the command name. The workload is the task's, or
    with ``accuracy`` the accuracy check's own (``accuracy-workload
    generate``). The dataset is ``task.dataset()``, built here (``--dataset``
    is its file, for generators in other languages). Returns the exit status.
    """
    args = _parser().parse_args(list(argv))
    if args.command != "generate":
        log(f"not a generator command: {args.command}")
        return 2
    if accuracy and (task.accuracy is None or task.accuracy.workload is None):
        log("this task's Accuracy has no workload of its own: pass Accuracy(workload=...)")
        return 2
    workload = _workload(task, accuracy=accuracy)
    if isinstance(workload, Generator):
        log(f"the workload is the generator {list(workload.command)!r}: run it instead")
        return 2
    try:
        phase_input = read_message(args.phase_input, phase_pb.PhaseInput)
        written = generate(workload, task.dataset(), phase_input, out=stdout, state=args.state)
    except (PhaseError, SchemaFileError) as error:
        log(f"generate failed: {error}")
        return 1
    log(f"generate: the stream was closed after {written} operations")
    return 0


def _generator_on_stdout(task: Task, argv: Sequence[str], *, accuracy: bool = False) -> int:
    """``generator`` on this process's standard output, which loadgen closes to end the stream."""
    stdout = sys.stdout.buffer
    try:
        code = generator(task, argv, stdout=stdout, accuracy=accuracy)
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

    commands: dict[str, Callable[[], int]] = {
        "benchmark": lambda: benchmark(
            task,
            project_root=args.project_root,
            output=args.vs_output,
            seed_flag=args.seed,
            deployer=deployer(),
            env=env,
            script=script,
        ),
        "accuracy": lambda: accuracy(
            task,
            project_root=args.project_root,
            out=args.out,
            seed_flag=args.seed,
            deployer=deployer(),
            env=env,
            script=script,
        ),
        "phase": lambda: phase(
            task, work=args.work, target=args.target, script=script, accuracy=args.accuracy
        ),
        "generate": lambda: _generator_on_stdout(task, argv),
        "accuracy-workload": lambda: _generator_on_stdout(task, args.args, accuracy=True),
        "trace": lambda: trace(
            task,
            project_root=args.project_root,
            out=args.out,
            seed_flag=args.seed,
            deployer=deployer(),
            env=env,
            script=script,
        ),
        "seed": lambda: seed(task, expected_digest=args.dataset_digest),
        "ctl": lambda: control(args.args),
    }
    return with_signals(commands[args.command])


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
