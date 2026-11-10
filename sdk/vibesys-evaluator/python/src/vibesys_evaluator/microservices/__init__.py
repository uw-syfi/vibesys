"""Microservices over HTTP: the workload, its requests, load phases and the system's deploy.

A ``Workload`` is a model and a weighted mix of ``Operation``s. Each
operation's ``build(model, rand)`` returns a ``Request`` of one or more
``Step``s (``http``, or its shorthands ``get``, ``post``, ``put``); a later
step can send a value an earlier one captured (``ref``, a ``Template``). A
``Generator`` stands for a workload written in another language (the Go SDK,
``go/workload``). The task's ``benchmark(run)`` composes load phases
(``Run.closed_loop``, ``Run.open_loop``), each returning a ``PhaseResult``;
the phases run in the Go core (``vibesys-evaluator phase``). A benchmark only
measures: whether the responses are right is for the task's accuracy check
(``main(accuracy=Accuracy(...))``, ``bench.py accuracy``) to judge: phases at
a fixed rate with the whole history captured and ``Fault``s between them,
judged by the task's oracle (the Go SDK's ``check``). Where the time goes,
hop by hop, is for the task's trace (``main(trace=Trace(...))``, ``bench.py
trace``): the same kind of phases with the application's OpenTelemetry spans
captured and analysed by stitch. The system under test is a
``microservices.k8s.App``. See the package's ``docs/spec.md``.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, NoReturn

from vibesys_evaluator._private.http import Request, Step, Template, get, http, post, put, ref
from vibesys_evaluator._private.results import Validity
from vibesys_evaluator.microservices import _k8s
from vibesys_evaluator.microservices._private import entry
from vibesys_evaluator.microservices._private.accuracy import Accuracy, Fault
from vibesys_evaluator.microservices._private.results import PhaseResult
from vibesys_evaluator.microservices._private.run import Run
from vibesys_evaluator.microservices._private.trace import Trace
from vibesys_evaluator.microservices._private.workload import Generator, Operation, Workload

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from vibesys_evaluator._private.results import Metric
    from vibesys_evaluator._private.typevars import D, M
    from vibesys_evaluator.microservices.k8s import App

__all__ = [
    "Accuracy",
    "Fault",
    "Generator",
    "Operation",
    "PhaseResult",
    "Request",
    "Run",
    "Step",
    "Template",
    "Trace",
    "Workload",
    "get",
    "http",
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
    capture: bool = False,
    accuracy: Accuracy | None = None,
    trace: Trace | None = None,
) -> NoReturn:
    """Run the task script's command (``benchmark``, ``accuracy``, ``trace``, ``phase``, ``generate``, ``accuracy-workload``, ``seed`` or ``ctl``) and exit.

    Args:
        dataset: Builds the initial data; a pure function of the task's constants.
        system: The system under test.
        workload: The operations and their model, or the ``Generator`` command
            of a workload written in Go.
        benchmark: The scoring definition: runs load phases on ``run`` and calls ``run.report``.
        metrics: The metrics ``run.report`` reports, by name.
        validity: When a phase is a measurement; ``Validity()`` when None.
        capture: Record every phase's whole history for an accuracy check,
            which benchmarks leave off: every response body, the loadgen
            client of each operation, each request's ``info``, and the faults
            of ``Run.fault`` (``history.binpb`` in the artifacts; see
            ``docs/spec.md``, "Accuracy history").
        accuracy: The task's accuracy check, which ``bench.py accuracy``
            runs: phases with the history captured and faults between them,
            then the task's oracle; it exits 0 on PASS, 1 on FAIL and 2 on
            UNKNOWN (see ``docs/spec.md``, "Accuracy run").
        trace: The task's trace, which ``bench.py trace`` runs: the system
            deployed as for a benchmark, its Deployments instrumented with
            OpenTelemetry, the trace's phases, and stitch's reports (call
            tree, critical path, per-hop latency) in ``trace/`` of the
            artifacts; it exits 0 with the reports, 1 when a step failed and
            2 without a trace (see ``docs/spec.md``, "Trace run").
    """
    task = entry.Task(
        dataset=dataset,
        system=system,
        workload=workload,
        benchmark=benchmark,
        metrics=metrics,
        validity=validity if validity is not None else Validity(),
        capture=capture,
        accuracy=accuracy,
        trace=trace,
    )
    entry.main(
        task,
        deployer=lambda: _k8s.deployer(system),
        control=lambda argv: _k8s.control(system, argv),
        env=os.environ,
    )
