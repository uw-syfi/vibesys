"""The kv task's script, deployed locally: ``main``'s commands with the local deploy kind.

The end-to-end test runs this as ``bench.py`` runs in a task: ``benchmark``
in a child process, which runs the ``phase`` commands as children of its own,
each running the real loadgen against the service at ``KV_TARGET``, which
runs the generator. The generator is this script's ``generate`` (the Python
workload), or, with ``KV_GO_GENERATOR`` set, that binary: the kv-store
example's Go generator (``KV_STORE_SCALE=test`` selects this task's scale).
"""

from __future__ import annotations

import dataclasses
import os
import sys
from pathlib import Path

import vibesys_evaluator as vse

# test-isolation: main() deploys with the k8s kind; this script swaps in the local kind
# through main's private entry, keeping every command and file the same.
from vibesys_evaluator._private.cli import Task, run
from vibesys_evaluator._private.deploy import TaskCommand
from vibesys_evaluator._private.local import LocalDeployer
from vibesys_evaluator.service import Generator
from vse_support import kv  # the tests directory is on PYTHONPATH

GO_GENERATOR = os.environ.get("KV_GO_GENERATOR", "")


def _benchmark(run: vse.Run) -> None:
    closed = run.closed_loop(clients=16, secs=1, warmup=0.5)  # 16 x 20 ops/s at SERVICE_SECS
    opened = run.open_loop(rate=0.5 * closed.ops_per_sec, secs=1, warmup=0.5, arrivals="uniform")
    run.report({"operations_per_second": closed.ops_per_sec, "latency_ms.p99": opened.p99_ms})


TASK = Task(
    dataset=kv.Data,
    system=dataclasses.replace(kv.SYSTEM, seed=None),  # the test seeds the service it serves
    workload=Generator([GO_GENERATOR]) if GO_GENERATOR else kv.WORKLOAD,
    benchmark=_benchmark,
    metrics=kv.METRICS,
    validity=vse.Validity(),
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
