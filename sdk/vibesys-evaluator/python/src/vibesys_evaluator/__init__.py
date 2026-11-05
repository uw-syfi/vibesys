"""vibesys-evaluator: build a VibeSys benchmark from a workload and phase primitives.

A task's ``bench.py`` calls ``main`` with its dataset, system under test,
workload, benchmark function, metrics and validity rules; see the package's
``docs/spec.md`` and ``examples/``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NoReturn

from vibesys_evaluator._private.calls import Call, Response, Versions
from vibesys_evaluator._private.rand import Rand
from vibesys_evaluator._private.results import Metric, PhaseResult, Validity
from vibesys_evaluator._private.run import Run

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


def main(  # noqa: PLR0913 - the five parts of a task, keyword-only
    *,
    dataset: Callable[[], D],
    system: App[D],
    workload: Workload[D, M],
    benchmark: Callable[[Run], None],
    metrics: Mapping[str, Metric],
    validity: Validity = Validity(),  # noqa: B008 - a frozen dataclass, never mutated
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
    from vibesys_evaluator._private import cli  # noqa: PLC0415 - keep importing the package cheap
    from vibesys_evaluator.deploy import _k8s  # noqa: PLC0415 - keep importing the package cheap

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
