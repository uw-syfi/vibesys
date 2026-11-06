"""The accuracy check of a microservices task: ``main(accuracy=Accuracy(...))`` and ``bench.py accuracy``.

The flow is the Go core's (``vibesys-evaluator accuracy run``, the same as a
Go task's ``microservices.Main``): it deploys and seeds the system as a
benchmark sample does, runs ``phases`` open-loop phases at a fixed rate with
the whole history captured (each a phase pod running ``bench.py phase
--accuracy``, whose generator is the check's own workload when it has one),
applies the faults between them, tears the system down, and runs the task's
oracle (a program on the Go SDK's ``check.Main``) on the history. This module
builds the request and turns the verdict into the exit status: 0 PASS, 1
FAIL, 2 UNKNOWN.
"""

from __future__ import annotations

import random
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from vibesys_evaluator._private import core
from vibesys_evaluator._private.environment import job_environment
from vibesys_evaluator._private.sample import SAMPLE_SEED_ENV, log
from vibesys_evaluator.microservices._k8s._deployment import app_message, probe_plan
from vibesys_evaluator.microservices._private import dataset as dataset_file
from vibesys_evaluator.microservices._private.local import LocalDeployer
from vibesys_evaluator.microservices._private.run import DATASET_FILE
from vibesys_evaluator.microservices._private.workload import Generator, Workload

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from vibesys_evaluator.microservices._private.deploy import Deployer, TaskCommand
    from vibesys_evaluator.microservices.k8s import App

#: Where the report goes by default: accuracy/ in the job's artifacts (``check.DefaultReportDir``).
DEFAULT_REPORT_DIR = Path("/vibesys/artifacts/accuracy")
#: The history's directory inside the report's.
HISTORY_DIR = "history"
#: The exit status of a run that could not complete: UNKNOWN, never a pass.
UNKNOWN_EXIT = 2

_ACTIONS = {
    "restart": "ACCURACY_FAULT_ACTION_RESTART",
    "restart_all": "ACCURACY_FAULT_ACTION_RESTART_ALL",
    "exec": "ACCURACY_FAULT_ACTION_EXEC",
    "flush": "ACCURACY_FAULT_ACTION_FLUSH",
}
_NAME = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


@dataclass(frozen=True)
class Fault:
    """A fault an accuracy check applies between two phases, recorded in the history.

    Build one with ``Fault.restart``, ``Fault.restart_all``, ``Fault.exec``
    or ``Fault.flush``: each acts on a Deployment of the ``k8s.App`` (one of
    its ``restartable`` or ``caches``; a flush, one of its ``caches``) after
    phase ``after`` (from 0), and is recorded as a fault of its kind
    (``restart``, ``restart_all``, ``exec``, ``flush``) on the Deployment,
    with its start and end and no operation in flight. The oracle's
    ``Spec.Fault`` says what it does to the state.
    """

    action: Literal["restart", "restart_all", "exec", "flush"]
    deployment: str
    after: int
    argv: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Check the fields."""
        if self.action not in _ACTIONS:
            msg = f"Fault action is restart, restart_all, exec or flush, got {self.action!r}"
            raise ValueError(msg)
        if not isinstance(self.deployment, str) or not _NAME.match(self.deployment):
            msg = f"Fault deployment must be a Deployment's name, got {self.deployment!r}"
            raise ValueError(msg)
        if isinstance(self.after, bool) or not isinstance(self.after, int) or self.after < 0:
            msg = f"Fault after must be a phase index >= 0, got {self.after!r}"
            raise ValueError(msg)
        if (self.action == "exec") != bool(self.argv):
            msg = "an exec Fault has a command (argv), and only an exec Fault has one"
            raise ValueError(msg)

    @staticmethod
    def restart(deployment: str, *, after: int) -> Fault:
        """Delete one pod of ``deployment`` (the first by name) and wait until it serves again."""
        return Fault("restart", deployment, after)

    @staticmethod
    def restart_all(deployment: str, *, after: int) -> Fault:
        """Delete every pod of ``deployment`` at once and wait until it serves again."""
        return Fault("restart_all", deployment, after)

    @staticmethod
    def exec(deployment: str, argv: Sequence[str], *, after: int) -> Fault:
        """Run ``argv`` in the first pod of ``deployment``; a nonzero exit ends the check."""
        return Fault("exec", deployment, after, tuple(argv))

    @staticmethod
    def flush(cache: str, *, after: int) -> Fault:
        """Empty ``cache`` (one of the ``k8s.App``'s ``caches``) in place, in every pod.

        Memcached's ``flush_all`` or redis's ``FLUSHALL``: the cache is cold
        afterwards and the application must still answer right.
        """
        return Fault("flush", cache, after)


@dataclass(frozen=True, kw_only=True)
class Accuracy:
    """The task's accuracy check (``bench.py accuracy``), for ``main(accuracy=...)``.

    It deploys and seeds the system as a benchmark sample does, runs
    ``phases`` open-loop phases (``rate`` operations per second, ``secs``
    after ``warmup``) with the whole history captured, applies ``faults``
    between them, tears the system down, and runs ``oracle`` (the task's
    oracle program, built on the Go SDK's ``check.Main``) on the history.
    ``bench.py accuracy`` exits 0 on PASS, 1 on FAIL and 2 on UNKNOWN (also
    when the run could not complete), with the report, the failures'
    timelines and the history in ``--out`` (``/vibesys/artifacts/accuracy``).
    A check runs at a fixed, modest rate, so that the responses are judged,
    not the capacity; no phase is a measurement, so ``main``'s ``validity``
    does not apply (a workload may send requests the system must refuse).

    ``workload`` is the check's own workload (a ``Workload`` or a
    ``Generator``), when its mix is not the benchmark's: one ``bench.py``
    then serves both. None: the phases run ``main``'s ``workload``.
    """

    phases: int
    rate: float
    secs: float
    oracle: Sequence[str]
    warmup: float = 0.0
    arrivals: Literal["poisson", "uniform"] = "poisson"
    faults: Sequence[Fault] = field(default_factory=tuple)
    workload: Workload[Any, Any] | Generator | None = None

    def __post_init__(self) -> None:
        """Check the fields."""
        if isinstance(self.phases, bool) or not isinstance(self.phases, int) or self.phases < 1:
            msg = f"Accuracy phases must be an int >= 1, got {self.phases!r}"
            raise ValueError(msg)
        for name in ("rate", "secs"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or not 0 < value < float("inf"):
                msg = f"Accuracy {name} must be a finite number > 0, got {value!r}"
                raise ValueError(msg)
        if not isinstance(self.warmup, (int, float)) or not 0 <= self.warmup < float("inf"):
            msg = f"Accuracy warmup must be a finite number >= 0, got {self.warmup!r}"
            raise ValueError(msg)
        if self.arrivals not in ("poisson", "uniform"):
            msg = f"Accuracy arrivals is 'poisson' or 'uniform', got {self.arrivals!r}"
            raise ValueError(msg)
        if isinstance(self.oracle, str) or not self.oracle:
            msg = "Accuracy oracle is the oracle's command, a non-empty sequence of strings"
            raise ValueError(msg)
        if self.workload is not None and not isinstance(self.workload, (Workload, Generator)):
            msg = f"Accuracy workload is a Workload, a Generator or None, got {self.workload!r}"
            raise ValueError(msg)
        for fault in self.faults:
            if not isinstance(fault, Fault) or fault.after >= self.phases - 1:
                msg = f"{fault!r} must be a Fault between two of the {self.phases} phases"
                raise ValueError(msg)
        object.__setattr__(self, "oracle", tuple(self.oracle))
        object.__setattr__(self, "faults", tuple(self.faults))


def _fault_message(fault: Fault) -> dict[str, Any]:
    """The ``AccuracyFault`` message of ``fault``."""
    message: dict[str, Any] = {
        "after_phase": fault.after,
        "action": _ACTIONS[fault.action],
        "deployment": fault.deployment,
    }
    if fault.argv:
        message["argv"] = list(fault.argv)
    return message


def _load(accuracy: Accuracy) -> dict[str, Any]:
    """Each phase's ``PhaseInput`` load."""
    return {
        "model": "LOAD_MODEL_OPEN",
        "rate": float(accuracy.rate),
        "arrivals": "ARRIVALS_POISSON" if accuracy.arrivals == "poisson" else "ARRIVALS_UNIFORM",
        "secs": float(accuracy.secs),
        "warmup_secs": float(accuracy.warmup),
    }


def report_dir(env: Mapping[str, str]) -> Path:
    """Where ``bench.py accuracy`` writes by default: accuracy/ in the job's artifacts."""
    artifacts = job_environment(env).artifacts
    return Path(artifacts) / "accuracy" if artifacts else DEFAULT_REPORT_DIR


def _sample_seed(flag: int | None, env: Mapping[str, str]) -> int:
    """The run's seed: ``--seed``, else the one vibesys passes, else a fresh one."""
    if flag is not None:
        return flag
    given = env.get(SAMPLE_SEED_ENV, "")
    if given:
        try:
            return int(given)
        except ValueError:
            log(f"{SAMPLE_SEED_ENV}={given!r} is not an integer: drawing a seed")
    return random.SystemRandom().getrandbits(63)


def oracle_command(oracle: Sequence[str], script: TaskCommand) -> list[str]:
    """The oracle's command; a relative program path is relative to the task script's directory."""
    program, *rest = oracle
    if "/" in program and not Path(program).is_absolute():
        program = str(Path(script.script).parent / program)
    return [program, *rest]


def _system(
    deployer: Deployer,
    app: App[Any],
    *,
    script: TaskCommand,
    digest: str,
    project_root: Path,
    artifacts: Path,
    scratch: Path,
) -> dict[str, Any]:
    """The request's system: the local one a test gives, else the App on Kubernetes."""
    seed = list(script.seed(dataset_digest=digest))
    if isinstance(deployer, LocalDeployer):
        return {"local": {"target": deployer.target, "seed": seed}}
    plan = scratch / "probe.binpb"
    plan.write_bytes(probe_plan(app.ready))
    deploy: dict[str, Any] = {
        "app": app_message(app),
        "task_dir": str(Path(script.script).parent),
        "project_root": str(project_root.resolve()),
        "work_dir": str(scratch / "core"),
        "artifacts_dir": str(artifacts),
        "probe_plan_file": str(plan),
    }
    if app.seed is not None:
        deploy["seed"] = {"argv": seed}
    return {"k8s": deploy}


def run_accuracy(
    accuracy: Accuracy,
    *,
    dataset: object,
    app: App[Any],
    project_root: Path,
    out: Path,
    seed: int | None,
    env: Mapping[str, str],
    deployer: Deployer,
    script: TaskCommand,
) -> int:
    """``bench.py accuracy``: the core's accuracy run, then the verdict's exit status."""
    history = out / HISTORY_DIR
    history.mkdir(parents=True, exist_ok=True)
    encoded = dataset_file.encode(dataset)
    digest = dataset_file.digest(encoded)
    (history / DATASET_FILE).write_bytes(encoded)
    sample_seed = _sample_seed(seed, env)
    log(f"accuracy check, seed {sample_seed}: {accuracy.phases} phases, the history in {history}")
    with tempfile.TemporaryDirectory(prefix="vse-accuracy-") as scratch:
        request = {
            **_system(
                deployer,
                app,
                script=script,
                digest=digest,
                project_root=project_root,
                # The deploy's logs go to the job's artifacts directory, which also names the
                # job in the namespace's labels; out when the check is not a job's.
                artifacts=Path(job_environment(env).artifacts or out),
                scratch=Path(scratch),
            ),
            "phase_command": [script.python, script.script, "phase", "--accuracy"],
            "sample_seed": str(sample_seed),
            "phases": accuracy.phases,
            "load": _load(accuracy),
            "faults": [_fault_message(fault) for fault in accuracy.faults],
            "history_dir": str(history),
            "oracle": oracle_command(accuracy.oracle, script),
            "out_dir": str(out),
        }
        try:
            reply = core.stream(
                "accuracy",
                "run",
                request,
                on_event=lambda event: log(str(event.get("message", ""))),
            )
        except core.CoreError as error:
            sys.stderr.write(f"UNKNOWN: the accuracy run did not complete: {error}\n")
            return UNKNOWN_EXIT
    log(f"accuracy: {reply.get('verdict', 'UNKNOWN')}")
    return int(reply.get("exit_code", UNKNOWN_EXIT))
