"""The kv task's script, deployed locally: ``main``'s commands with the local deploy kind.

The end-to-end test runs this as ``bench.py`` runs in a task: ``benchmark``
in a child process, which runs the ``phase`` commands as children of its own,
each running the real loadgen against the service at ``KV_TARGET``, which
runs the generator. The generator is this script's ``generate`` (the Python
workload), or, with ``KV_GO_GENERATOR`` set, that binary: the kv-store
example's Go generator (``KV_STORE_SCALE=test`` selects this task's scale).
With ``KV_CAPTURE`` set the sample records the accuracy history, with a fault
between its phases. ``accuracy`` is the task's accuracy check, with the
oracle at ``KV_ORACLE``; ``KV_ACCURACY=restart`` gives it a restart between its
phases, which a local system cannot apply.
"""

from __future__ import annotations

import dataclasses
import os
import sys
from pathlib import Path

import vibesys_evaluator as vse
from vibesys_evaluator import microservices as ms
from vibesys_evaluator.microservices import Generator
from vibesys_evaluator.microservices._private.deploy import TaskCommand

# test-isolation: main() deploys with the k8s kind; this script swaps in the local kind
# through main's private entry, keeping every command and file the same.
from vibesys_evaluator.microservices._private.entry import Task, run
from vibesys_evaluator.microservices._private.local import LocalDeployer
from vse_support import kv  # the tests directory is on PYTHONPATH

GO_GENERATOR = os.environ.get("KV_GO_GENERATOR", "")
CAPTURE = bool(os.environ.get("KV_CAPTURE"))
FAULTS = [ms.Fault.restart("kv", after=0)] if os.environ.get("KV_ACCURACY") == "restart" else []
ACCURACY = ms.Accuracy(
    phases=2,
    rate=40,
    secs=1,
    warmup=0.5,
    arrivals="uniform",
    faults=FAULTS,
    oracle=[os.environ.get("KV_ORACLE", "kv-oracle")],
)


def _benchmark(run: ms.Run) -> None:
    closed = run.closed_loop(clients=16, secs=1, warmup=0.5)  # 16 x 20 ops/s at SERVICE_SECS
    if CAPTURE:
        run.fault("pause", "kv", lambda: None)
    opened = run.open_loop(rate=0.5 * closed.ops_per_sec, secs=1, warmup=0.5, arrivals="uniform")
    run.report({"operations_per_second": closed.ops_per_sec, "latency_ms.p99": opened.p99_ms})


TASK = Task(
    dataset=kv.Data,
    system=dataclasses.replace(kv.SYSTEM, seed=None),  # the test seeds the service it serves
    workload=Generator([GO_GENERATOR]) if GO_GENERATOR else kv.WORKLOAD,
    benchmark=_benchmark,
    metrics=kv.METRICS,
    validity=vse.Validity(),
    capture=CAPTURE,
    accuracy=ACCURACY,
)


def _control(_argv: object) -> int:
    return 2


if __name__ == "__main__":
    target = os.environ.get("KV_TARGET", "")  # only benchmark deploys (generate and check do not)
    sys.exit(
        run(
            TASK,
            sys.argv[1:],
            deployer=lambda: LocalDeployer(target=target),
            control=_control,
            env=os.environ,
            script=TaskCommand(python=sys.executable, script=str(Path(__file__).resolve())),
        )
    )
