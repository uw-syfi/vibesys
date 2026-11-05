"""vibesys-evaluator: the sample lifecycle every VibeSys task evaluator shares.

The core knows no domain. A task's ``bench.py`` calls ``main`` with its
metrics and its ``check``, ``benchmark`` and ``profile`` functions; each gets a
``Run`` (its phases and its report). Domains add what their tasks need on top:
``vibesys_evaluator.microservices`` (workloads over HTTP, load phases, deploys)
and ``vibesys_evaluator.llm_serving`` (the candidate server, AgentX replay,
golden accuracy checks). See the package's ``docs/spec.md`` and ``examples/``.
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING, NoReturn

from vibesys_evaluator._private import cli
from vibesys_evaluator._private.rand import Rand
from vibesys_evaluator._private.results import Metric, Validity
from vibesys_evaluator._private.run import Run

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

__all__ = ["Metric", "Rand", "Run", "Validity", "main"]


def main(
    *,
    metrics: Mapping[str, Metric],
    benchmark: Callable[[Run], None],
    check: Callable[[Run], str | None] | None = None,
    profile: Callable[[Run], None] | None = None,
) -> NoReturn:
    """Run the task script's command (``check``, ``benchmark`` or ``profile``) and exit.

    ``benchmark --vs-output FILE`` writes result-protocol-2 records: ``hello``
    with ``metrics``, then the result ``benchmark`` reports with
    ``run.report`` or an error naming what failed. ``check`` exits 0 when the
    function returns None and 1 with the reason it returns. ``profile`` runs
    the load a profiler records; it is never scored.

    Args:
        metrics: The metrics ``run.report`` reports, by name.
        benchmark: The scoring definition: runs phases and calls ``run.report``.
        check: The correctness check: None when the system is correct, else what is wrong.
        profile: The standard load to profile the system under.
    """
    task = cli.Task(metrics=metrics, benchmark=benchmark, check=check, profile=profile)
    sys.exit(cli.run(task, sys.argv[1:], env=os.environ))
