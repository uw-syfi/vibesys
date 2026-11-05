"""The kv task's script, deployed locally: ``main``'s commands with the local deploy kind.

The end-to-end test runs this as ``bench.py`` runs in a task: ``benchmark``
in a child process, which runs the ``phase`` commands as children of its own,
each running the real loadgen against the service at ``KV_TARGET``.
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
from vse_support import kv  # the tests directory is on PYTHONPATH

TASK = Task(
    dataset=kv.Data,
    system=dataclasses.replace(kv.SYSTEM, seed=None),  # the test seeds the service it serves
    workload=kv.WORKLOAD,
    benchmark=kv.benchmark,
    metrics=kv.METRICS,
    validity=vse.Validity(),
)


def _control(_argv: object) -> int:
    return 2


if __name__ == "__main__":
    target = os.environ["KV_TARGET"]
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
