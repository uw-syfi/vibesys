"""vibesys-evaluator: build a VibeSys benchmark from a workload and phase primitives.

A task's ``bench.py`` calls ``main`` with its dataset, system under test,
workload, benchmark function, metrics and validity rules; see the package's
``docs/spec.md`` and ``examples/``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NoReturn

from vibesys_evaluator._private import cli
from vibesys_evaluator._private.calls import Call, Response, Versions
from vibesys_evaluator._private.rand import Rand
from vibesys_evaluator._private.results import Metric, PhaseResult, Validity
from vibesys_evaluator._private.run import Run
from vibesys_evaluator.deploy import _k8s

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from vibesys_evaluator._private.typevars import D, M
    from vibesys_evaluator.deploy.k8s import App
    from vibesys_evaluator.service import Workload

__all__ = [
    "Call",
    "Metric",
    "PhaseResult",
    "Rand",
    "Response",
    "Run",
    "Validity",
    "Versions",
    "main",
]


def main(
    *,
    dataset: Callable[[], D],
    system: App[D],
    workload: Workload[D, M],
    benchmark: Callable[[Run], None],
    metrics: Mapping[str, Metric],
    validity: Validity = Validity(),  # noqa: B008  # LW-150401 [B008]; the default is a frozen dataclass.
    # > The approved signature (api/public-api.txt) shows the default as Validity(); a module
    # > constant would show its private name instead, and None would widen the type.
) -> NoReturn:
    """Run the task script's command (``benchmark``, ``phase``, ``seed`` or ``ctl``) and exit.

    Args:
        dataset: Builds the initial data; a pure function of the task's constants.
        system: The system under test.
        workload: The operations and their model.
        benchmark: The scoring definition: runs phases on ``run`` and calls ``run.report``.
        metrics: The metrics ``run.report`` reports, by name.
        validity: When a phase is a measurement.
    """
    task = cli.Task(
        dataset=dataset,
        system=system,
        workload=workload,
        benchmark=benchmark,
        metrics=metrics,
        validity=validity,
    )
    cli.main(
        task,
        deployer=lambda: _k8s.deployer(system),
        control=lambda argv: _k8s.control(system, argv),
    )
