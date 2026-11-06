"""Microservices over HTTP: the workload, its requests, load phases and the system's deploy.

A ``Workload`` is a model and a weighted mix of ``Operation``s. Each
operation's ``build(model, rand)`` returns a ``Request`` of one or more
``Step``s (``get``, ``post``, ``put``); a later step can send a value an
earlier one captured (``ref``, a ``Template``). A ``Generator`` stands for a
workload written in another language (the Go SDK, ``go/workload``). Checks and applies see each
operation as a ``Call`` with its ``Response``s; ``Versions`` says what a read
may show. The task's ``benchmark(run)`` composes load phases
(``Run.closed_loop``, ``Run.open_loop``), each returning a ``PhaseResult``.
The system under test is a ``microservices.k8s.App``. See the package's
``docs/spec.md``.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, NoReturn

from vibesys_evaluator._private.http import Request, Step, Template, get, post, put, ref
from vibesys_evaluator._private.results import Validity
from vibesys_evaluator.microservices import _k8s
from vibesys_evaluator.microservices._private import entry
from vibesys_evaluator.microservices._private.calls import Call, Response, Versions
from vibesys_evaluator.microservices._private.results import PhaseResult
from vibesys_evaluator.microservices._private.run import Run
from vibesys_evaluator.microservices._private.workload import Generator, Operation, Workload

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from vibesys_evaluator._private.results import Metric
    from vibesys_evaluator._private.typevars import D, M
    from vibesys_evaluator.microservices.k8s import App

__all__ = [
    "Call",
    "Generator",
    "Operation",
    "PhaseResult",
    "Request",
    "Response",
    "Run",
    "Step",
    "Template",
    "Versions",
    "Workload",
    "get",
    "main",
    "post",
    "put",
    "ref",
]


def main(
    *,
    dataset: Callable[[], D],
    system: App[D],
    workload: Workload[D, M] | Generator,
    benchmark: Callable[[Run], None],
    metrics: Mapping[str, Metric],
    validity: Validity | None = None,
) -> NoReturn:
    """Run the task script's command (``benchmark``, ``phase``, ``generate``, ``check``, ``seed`` or ``ctl``) and exit.

    Args:
        dataset: Builds the initial data; a pure function of the task's constants.
        system: The system under test.
        workload: The operations and their model, or the ``Generator`` command
            of a workload written in Go.
        benchmark: The scoring definition: runs load phases on ``run`` and calls ``run.report``.
        metrics: The metrics ``run.report`` reports, by name.
        validity: When a phase is a measurement; ``Validity()`` when None.
    """
    task = entry.Task(
        dataset=dataset,
        system=system,
        workload=workload,
        benchmark=benchmark,
        metrics=metrics,
        validity=validity if validity is not None else Validity(),
    )
    entry.main(
        task,
        deployer=lambda: _k8s.deployer(system),
        control=lambda argv: _k8s.control(system, argv),
        env=os.environ,
    )
