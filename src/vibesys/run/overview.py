"""The run's overview as its input bundle declares it: objective, metric, check and evaluator."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.events import BenchmarkVersions, EvaluatorIdentity, ObjectiveOverview, RunOverview

if TYPE_CHECKING:
    from vibesys.inputs import InputBundle


def overview_of(bundle: InputBundle) -> RunOverview:
    """The objective text, the benchmark's metric, the correctness check and the pinned evaluator.

    The metric's unit and direction are not the bundle's facts: the run's measurements and
    objectives add them later (the server's snapshot does).
    """
    manifest = bundle.manifest
    package = None if manifest.evaluator is None else manifest.evaluator.package_requirement
    return RunOverview(
        objective=ObjectiveOverview(
            text=bundle.objective,
            metric=manifest.benchmark.result.metric if manifest.benchmark.result else None,
            correctness_check=bundle.accuracy_command,
        ),
        benchmarks=BenchmarkVersions(
            evaluator=None
            if package is None
            else EvaluatorIdentity(name=package.name, version=package.version)
        ),
    )
