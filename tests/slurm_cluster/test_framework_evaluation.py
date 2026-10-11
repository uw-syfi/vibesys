"""An older loop's framework evaluation runs on a real cluster through the gate (#1649).

Every loop but the dynamic one evaluates each round through the framework: its trusted
accuracy and benchmark run as the gate commands the environment hands it, through the
container's sandbox, the host broker, and a real Slurm job. The path is deleted per loop as
each is ported to the core path (roadmap #1952).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

import pytest
from tests.slurm_cluster.harness import BENCHMARK_OUTPUT_ARGUMENT

from vs_runtime.api.infrastructure import (
    ScalarBenchmarkContract,
    TrustedEvaluationExecutor,
    TrustedEvaluationPlan,
    create_trusted_evaluation_executor,
)

if TYPE_CHECKING:
    from tests.slurm_cluster.harness import OpenRun

    from vs_project.api import GitTracker

pytestmark = pytest.mark.slurm_cluster


class _UntouchedInputs:
    """The git tracker of a candidate that changed no evaluator-owned file."""

    def trusted_input_changes(self) -> list[str]:
        return []


def _executor(run: OpenRun) -> TrustedEvaluationExecutor:
    return create_trusted_evaluation_executor(
        TrustedEvaluationPlan(
            accuracy_command=run.session.view.paths.accuracy_command,
            benchmark_command=run.session.view.paths.benchmark_command,
            benchmark_contract=ScalarBenchmarkContract(
                output_argument=BENCHMARK_OUTPUT_ARGUMENT, metric="score"
            ),
        ),
        workspace=run.workspace,
        sandbox=run.session.sandbox,
        git=cast("GitTracker", _UntouchedInputs()),
    )


def test_the_framework_accuracy_of_an_older_loop_runs_in_a_real_job(run: OpenRun) -> None:
    run.set_mode("accuracy", "pass")

    result = asyncio.run(_executor(run).accuracy())

    assert result.passed, result.output
    assert "accuracy ok" in result.output


def test_the_framework_benchmark_of_an_older_loop_returns_its_measurement(run: OpenRun) -> None:
    run.set_mode("benchmark", "pass")

    result = asyncio.run(_executor(run).benchmark())

    assert result.passed, result.output
    assert result.row is not None
    assert result.row["score"] == 42


def test_the_framework_reports_a_failing_accuracy_of_an_older_loop_as_failed(
    run: OpenRun,
) -> None:
    run.set_mode("accuracy", "exit:3")

    result = asyncio.run(_executor(run).accuracy())

    assert not result.passed
    assert "accuracy failed" in result.output
