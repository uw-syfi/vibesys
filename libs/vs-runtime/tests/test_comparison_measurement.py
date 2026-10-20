"""A comparison baseline is measured through the evaluation executor, cached, and never raises.

Every test drives the production path (the semantic Slurm executor over the sandbox's Slurm
executor) over the Fake cluster, so what is checked is the measurer's behaviour: which jobs it
submits, what it keeps, and what it reports when a job fails, never finishes or is stopped.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from vs_project.api import ComparisonInput
from vs_runtime.api.comparisons import (
    ComparisonFailed,
    ComparisonMeasured,
    ComparisonMeasurer,
    ComparisonNotConfigured,
    ComparisonPlan,
    ComparisonStore,
    ComparisonSubject,
    ComparisonUnavailableError,
)
from vs_runtime.api.slurm_evaluation_stack import ScenarioCluster, Stack, build_stack
from vs_sandbox.api.slurm import (
    BaselineServer,
    ComparisonMachine,
    SlurmExecutionPolicy,
    comparison_policy,
)
from vs_sim.api.testing import ManualClock, yield_until
from vs_slurm.api import SlurmJobStatus, SlurmService

if TYPE_CHECKING:
    from pathlib import Path

    from vs_runtime.api.comparisons import ComparisonExecutor

pytestmark = pytest.mark.asyncio

_IMAGE = "docker://rocm/vllm@sha256:" + "a" * 64


def _spec(**changes: object) -> ComparisonInput:
    table: dict[str, object] = {
        "name": "vllm",
        "label": "vLLM",
        "image": _IMAGE,
        "serve": ["vllm", "serve", "--port", "${PORT}"],
        **changes,
    }
    return ComparisonInput.model_validate(table)


def _subject(spec: ComparisonInput | None = None, **changes: str) -> ComparisonSubject:
    fields = {"identity": "a" * 64, "evaluator": "evaluator@1", "hardware": "mi210"} | changes
    return ComparisonSubject(
        spec=spec or _spec(), benchmark=("measure", "--mode", "quick"), **fields
    )


class _Executors:
    """One executor per sample, each over its own stack on one Fake cluster."""

    def __init__(self, stacks: list[Stack], *, unavailable: str | None = None) -> None:
        self._stacks = list(stacks)
        self._unavailable = unavailable
        self.avoiding: list[tuple[str, ...]] = []

    def executor_for(
        self, subject: ComparisonSubject, *, avoiding: tuple[str, ...]
    ) -> ComparisonExecutor:
        del subject
        if self._unavailable is not None:
            raise ComparisonUnavailableError(self._unavailable)
        self.avoiding.append(avoiding)
        return self._stacks.pop(0).executor


async def _rig(
    root: Path,
    cluster: ScenarioCluster,
    samples: int = 1,
    *,
    unavailable: str | None = None,
    policy: SlurmExecutionPolicy | None = None,
) -> tuple[ComparisonMeasurer, _Executors, ComparisonStore, ManualClock, Stack]:
    stacks = [await build_stack(root, cluster, policy=policy) for _ in range(samples)]
    store_dir = root / "comparisons"
    store_dir.mkdir(exist_ok=True)
    store = ComparisonStore(store_dir)
    clock = ManualClock()
    executors = _Executors(stacks, unavailable=unavailable)
    measurer = ComparisonMeasurer(store, executors, clock=clock, sleeper=clock)
    return measurer, executors, store, clock, stacks[0]


def _plan(
    stack: Stack,
    *,
    samples: int = 1,
    budget_seconds: float = 3600.0,
    spread_across_nodes: bool = True,
    remeasure: bool = False,
) -> ComparisonPlan:
    return ComparisonPlan(
        revision=stack.snapshot,
        samples=samples,
        budget_seconds=budget_seconds,
        headline_metric="throughput",
        spread_across_nodes=spread_across_nodes,
        remeasure=remeasure,
    )


async def test_a_sample_is_one_job_whose_benchmark_reading_is_the_comparison(
    tmp_path: Path,
) -> None:
    cluster = ScenarioCluster()
    cluster.nodes = ["node-a"]
    measurer, _, _, _, stack = await _rig(tmp_path, cluster)

    outcome = await measurer.measure(_subject(), _plan(stack))

    assert isinstance(outcome.reading, ComparisonMeasured)
    assert outcome.cached is False
    assert outcome.reading.metric == "throughput"
    assert [s.value for s in outcome.reading.samples] == [12]
    assert outcome.reading.hosts == ("node-a",)
    assert len(cluster.batches) == 1
    assert [stage.name for stage in cluster.batches[0].stages] == ["accuracy", "benchmark"]


async def test_the_job_serves_the_baselines_engine_in_place_of_the_candidates(
    tmp_path: Path,
) -> None:
    candidate = SlurmExecutionPolicy(
        service=SlurmService(
            command=("python", "-m", "engine.server", "--port", "VIBESYS_DYNAMIC_PORT"),
            readiness_url="http://127.0.0.1:VIBESYS_DYNAMIC_PORT/v1/models",
            startup_timeout_seconds=900,
        ),
        setup_script="/cluster/setup.sh",
        benchmark_arguments=("--base-url", "http://127.0.0.1:VIBESYS_DYNAMIC_PORT/v1"),
    )
    machine = ComparisonMachine(
        gpu="rocm", image_cache_dir="/cluster/images", model_cache_dir="/cluster/hf"
    )
    server = BaselineServer(
        image=_IMAGE,
        argv=("vllm", "serve", "--port", "${PORT}"),
        ready_timeout_seconds=1200,
        benchmark_args=("--backend", "vllm"),
    )
    cluster = ScenarioCluster()
    measurer, _, _, _, stack = await _rig(
        tmp_path, cluster, policy=comparison_policy(candidate, machine, server)
    )

    await measurer.measure(_subject(), _plan(stack))

    (batch,) = cluster.batches
    assert batch.service is not None
    assert "engine.server" not in " ".join(batch.service.command)
    assert "a" * 64 in " ".join(batch.service.command)
    assert batch.service.startup_timeout_seconds == 1200
    assert batch.setup_script == "/cluster/setup.sh"
    benchmark = next(stage for stage in batch.stages if stage.name == "benchmark")
    assert "VIBESYS_DYNAMIC_PORT/v1 --backend vllm" in " ".join(benchmark.command)


async def test_each_sample_avoids_the_nodes_of_the_earlier_ones(tmp_path: Path) -> None:
    cluster = ScenarioCluster()
    cluster.nodes = ["node-a", "node-b", "node-c"]
    measurer, executors, _, _, stack = await _rig(tmp_path, cluster, samples=3)

    outcome = await measurer.measure(_subject(), _plan(stack, samples=3))

    assert isinstance(outcome.reading, ComparisonMeasured)
    assert outcome.reading.hosts == ("node-a", "node-b", "node-c")
    assert executors.avoiding == [(), ("node-a",), ("node-a", "node-b")]
    assert len(set(cluster.submissions)) == 3


async def test_spreading_across_nodes_can_be_turned_off(tmp_path: Path) -> None:
    cluster = ScenarioCluster()
    cluster.nodes = ["node-a", "node-a"]
    measurer, executors, _, _, stack = await _rig(tmp_path, cluster, samples=2)

    await measurer.measure(_subject(), _plan(stack, samples=2, spread_across_nodes=False))

    assert executors.avoiding == [(), ()]


async def test_a_reading_is_cached_by_key_and_reused_without_a_job(tmp_path: Path) -> None:
    cluster = ScenarioCluster()
    measurer, executors, store, _, stack = await _rig(tmp_path, cluster)
    first = await measurer.measure(_subject(), _plan(stack))

    again = await measurer.measure(_subject(), _plan(stack))

    assert again.cached is True
    assert again.reading == first.reading
    assert len(cluster.submissions) == 1
    assert len(executors.avoiding) == 1
    assert store.has_measured("vllm")


async def test_an_evaluator_bump_does_not_force_a_new_measurement(tmp_path: Path) -> None:
    cluster = ScenarioCluster()
    measurer, _, store, _, stack = await _rig(tmp_path, cluster)
    await measurer.measure(_subject(), _plan(stack))

    outcome = await measurer.measure(_subject(evaluator="evaluator@2"), _plan(stack))

    assert outcome.cached is True
    assert len(cluster.submissions) == 1
    stored = store.get(_subject())
    assert stored is not None
    assert stored.evaluator == "evaluator@1"


@pytest.mark.parametrize(
    "changed",
    [
        {"identity": "b" * 64},
        {"hardware": "mi300"},
    ],
)
async def test_a_changed_key_is_measured_again(tmp_path: Path, changed: dict[str, str]) -> None:
    cluster = ScenarioCluster()
    measurer, _, _, _, stack = await _rig(tmp_path, cluster, samples=2)
    await measurer.measure(_subject(), _plan(stack))

    outcome = await measurer.measure(_subject(**changed), _plan(stack))

    assert outcome.cached is False
    assert len(cluster.submissions) == 2


async def test_remeasuring_runs_new_jobs_and_replaces_the_reading(tmp_path: Path) -> None:
    cluster = ScenarioCluster()
    measurer, _, store, _, stack = await _rig(tmp_path, cluster, samples=2)
    await measurer.measure(_subject(), _plan(stack))
    cluster.benchmark_output = (
        "__VIBESYS_FRAMEWORK_BENCHMARK_JSON__\n"
        '{"throughput": 40}\n'
        "__VIBESYS_FRAMEWORK_BENCHMARK_JSON_END__"
    )

    outcome = await measurer.measure(_subject(), _plan(stack, remeasure=True))

    assert outcome.cached is False
    assert isinstance(outcome.reading, ComparisonMeasured)
    assert outcome.reading.value == 40
    assert len(set(cluster.submissions)) == 2
    stored = store.get(_subject())
    assert stored is not None
    assert stored.generation == 1
    assert stored.reading.value == 40


async def test_a_failing_benchmark_is_a_failed_comparison_with_its_reason(tmp_path: Path) -> None:
    cluster = ScenarioCluster()
    cluster.benchmark_exit = 1
    measurer, _, store, _, stack = await _rig(tmp_path, cluster)

    outcome = await measurer.measure(_subject(), _plan(stack))

    assert isinstance(outcome.reading, ComparisonFailed)
    assert "sample 1 of 1" in outcome.reading.reason
    assert not store.has_measured("vllm")


async def test_a_failing_accuracy_check_is_a_failed_comparison(tmp_path: Path) -> None:
    cluster = ScenarioCluster()
    cluster.accuracy_exit = 1
    cluster.accuracy_output = "token mismatch at position 12"
    measurer, _, _, _, stack = await _rig(tmp_path, cluster)

    outcome = await measurer.measure(_subject(), _plan(stack))

    assert isinstance(outcome.reading, ComparisonFailed)
    assert "accuracy" in outcome.reading.reason


async def test_a_server_that_never_answers_is_a_failed_comparison(tmp_path: Path) -> None:
    cluster = ScenarioCluster()
    cluster.job_exit_code = 71
    cluster.service_log_tail = "ImportError: no module named engine"
    measurer, _, _, _, stack = await _rig(tmp_path, cluster)

    outcome = await measurer.measure(_subject(), _plan(stack))

    assert isinstance(outcome.reading, ComparisonFailed)


async def test_a_machine_that_cannot_serve_it_reports_not_configured(tmp_path: Path) -> None:
    measurer, _, store, _, stack = await _rig(
        tmp_path, ScenarioCluster(), unavailable="the operator file has no [vibesys.comparison]"
    )

    outcome = await measurer.measure(_subject(), _plan(stack))

    assert isinstance(outcome.reading, ComparisonNotConfigured)
    assert "vibesys.comparison" in outcome.reading.reason
    assert not store.has_measured("vllm")


async def test_a_job_that_outlasts_its_budget_is_cancelled_and_failed(tmp_path: Path) -> None:
    cluster = ScenarioCluster()
    cluster.states = (SlurmJobStatus.RUNNING,)
    measurer, _, _, clock, stack = await _rig(tmp_path, cluster)

    outcome = await measurer.measure(_subject(), _plan(stack, budget_seconds=120.0))

    assert isinstance(outcome.reading, ComparisonFailed)
    assert "120 seconds" in outcome.reading.reason
    assert clock.now() >= 120.0
    assert cluster.cancelled


async def test_a_stop_cancels_the_job_and_propagates(tmp_path: Path) -> None:
    cluster = ScenarioCluster()
    cluster.states = (SlurmJobStatus.RUNNING,)
    measurer, _, store, _, stack = await _rig(tmp_path, cluster)
    task = asyncio.create_task(measurer.measure(_subject(), _plan(stack, budget_seconds=1e9)))

    async def submitted() -> int:
        return len(cluster.submissions)

    await yield_until(submitted, lambda count: count > 0, "the job's submission")

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert cluster.cancelled
    assert not store.has_measured("vllm")
