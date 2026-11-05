"""The task script's command line: what ``main`` does in each process of a sample.

    bench.py benchmark --project-root DIR --vs-output FILE [--seed N]   one sample (vibesys)
    bench.py phase --work DIR --target URL                              one phase (a phase pod)
    bench.py seed --dataset-digest HEX                                  the seed (a seed Job)
    bench.py ctl ARGS...                                                deploy hooks (accuracy)

``benchmark`` writes ``hello`` first, then exactly one outcome: the result
``benchmark(run)`` reports, or an error record naming what failed. The
system is torn down on every path; the first SIGTERM, SIGINT or SIGHUP ends
the sample as an error and later ones are ignored, so teardown completes.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

from vibesys_evaluator._private import dataset as dataset_file
from vibesys_evaluator._private.deploy import DeployContext, DeployError, PhaseRunError, TaskCommand
from vibesys_evaluator._private.loadgen import ProcessLoadgen
from vibesys_evaluator._private.phase import PhaseError, PodTask, run_pod
from vibesys_evaluator._private.records import RecordError, RecordStream
from vibesys_evaluator._private.run import BenchmarkError, PhaseDriver, new_run

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from types import FrameType

    from vibesys_evaluator._private.deploy import Deployer
    from vibesys_evaluator._private.results import Metric, Validity
    from vibesys_evaluator._private.run import Run
    from vibesys_evaluator._private.workload import Workload
    from vibesys_evaluator.deploy.k8s import App

#: The sample seed vibesys may pass; drawn fresh (``secrets``) when unset.
SAMPLE_SEED_ENV = "VIBESYS_SAMPLE_SEED"
#: The directory vibesys keeps with the job.
ARTIFACTS_ENV = "VIBESYS_ARTIFACTS_DIR"
_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
_MAX_SEED = (1 << 64) - 1


def log(message: str) -> None:
    """One progress line on standard error."""
    print(f"vibesys-evaluator: {message}", file=sys.stderr, flush=True)  # noqa: T201 - the job's log


class Stopped(BaseException):
    """A signal asked the process to stop."""

    def __init__(self, signum: int) -> None:
        """Record which signal."""
        super().__init__(signum)
        self.signum = signum


def _stop(signum: int, _frame: FrameType | None) -> None:
    # The first signal stops the work; later ones are ignored so that cleanup completes.
    for sig in _SIGNALS:
        signal.signal(sig, signal.SIG_IGN)
    raise Stopped(signum)


@dataclass(frozen=True, kw_only=True)
class Task:
    """Everything the task script passes to ``main``."""

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


def _sample_seed(flag: int | None, env: Mapping[str, str]) -> tuple[int, str]:
    if flag is not None:
        if not 0 <= flag <= _MAX_SEED:
            msg = f"--seed must be in [0, 2**64), got {flag}"
            raise ValueError(msg)
        return flag, "--seed"
    value = env.get(SAMPLE_SEED_ENV, "").strip()
    if value:
        try:
            seed = int(value, 0)
        except ValueError:
            seed = -1
        if not 0 <= seed <= _MAX_SEED:
            msg = f"${SAMPLE_SEED_ENV}={value!r} is not an integer in [0, 2**64)"
            raise ValueError(msg)
        return seed, SAMPLE_SEED_ENV
    return secrets.randbits(63), "drawn"


def _artifacts(env: Mapping[str, str], scratch: Path) -> Path:
    value = env.get(ARTIFACTS_ENV, "")
    path = Path(value) if value else scratch / "artifacts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def benchmark(  # noqa: PLR0913 - the sample's inputs, named
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
    records = RecordStream(output)
    driver: PhaseDriver | None = None
    try:
        records.hello(task.metrics)
    except RecordError as error:
        records.error(f"main(metrics=...): {error}")
        records.close()
        return 1
    with tempfile.TemporaryDirectory(prefix="vibesys-evaluator-") as scratch:
        try:
            seed, source = _sample_seed(seed_flag, env)
            artifacts = _artifacts(env, Path(scratch))
            (artifacts / "seeds.json").write_text(
                json.dumps({"sample_seed": seed, "source": source}) + "\n"
            )
            log(f"sample seed {seed} ({source})")
            encoded = dataset_file.encode(task.dataset())
            digest = dataset_file.digest(encoded)
            (artifacts / "dataset.json").write_bytes(encoded)
            log(f"dataset {digest[:16]} ({len(encoded)} bytes)")
            context = DeployContext(
                project_root=project_root.resolve(),
                artifacts=artifacts,
                task=script,
                dataset_digest=digest,
                log=log,
            )
            with deployer.deploy(context) as runner:
                driver = PhaseDriver(
                    runner=runner,
                    sample_seed=seed,
                    operations=[op.name for op in task.workload.operations],
                    validity=task.validity,
                    artifacts=artifacts,
                    records=records,
                    log=log,
                )
                task.benchmark(new_run(driver))
            if records.outcome is None:
                records.error("benchmark(run) returned without calling run.report(...)")
        except BenchmarkError as error:
            log(f"failed: {error}")
            records.error(str(error), partial=error.partial)
        except (DeployError, PhaseRunError) as error:
            log(f"failed: {error}")
            records.error(str(error), partial=driver.partial if driver else None)
        except Stopped as stop:
            log(f"stopped by signal {stop.signum}")
            records.error(
                f"stopped by signal {signal.Signals(stop.signum).name} before the sample finished",
                partial=driver.partial if driver else None,
            )
        except Exception as error:  # noqa: BLE001 - any failure of the task's code is the sample's error
            traceback.print_exc()
            records.error(
                f"{type(error).__name__}: {error}", partial=driver.partial if driver else None
            )
        finally:
            records.close()
    return 0 if records.outcome == "result" else 1


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


def run(  # noqa: PLR0913 - the task and the process's environment, named
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
    for sig in _SIGNALS:
        signal.signal(sig, _stop)
    try:
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
    except Stopped as stop:
        return 128 + stop.signum


def main(
    task: Task, *, deployer: Callable[[], Deployer], control: Callable[[Sequence[str]], int]
) -> NoReturn:
    """Run the command in ``sys.argv`` and exit with its status."""
    script = TaskCommand(python=sys.executable, script=str(Path(sys.argv[0]).resolve()))
    sys.exit(
        run(task, sys.argv[1:], deployer=deployer, control=control, env=os.environ, script=script)
    )
