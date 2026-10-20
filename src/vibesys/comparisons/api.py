"""Comparison baselines: measure them once on request, and read what was measured.

A task declares the engines it is compared with (``[[comparison]]``). Measuring them is an
explicit, one-off act (``vibesys compare``): it runs the task's accuracy check and benchmark
against each engine, served from its pinned image on the operator's cluster, three times on
different nodes, and keeps the reading in the project's machine-local cache. A run never measures
one. It reads the cache (``stored_comparisons``) and shows what it finds, "not measured" included.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibesys.comparisons._checkout import RepositoryCheckouts
from vibesys.comparisons._slurm import SlurmSampleExecutors
from vibesys.comparisons._subjects import comparison_subjects
from vibesys.domains.registry import resolve_domain
from vibesys.run.evaluation import trusted_evaluation_plan
from vs_project.api import comparison_directory, comparison_namespace, load_objectives
from vs_runtime.api.comparisons import (
    ComparisonFailed,
    ComparisonMeasurer,
    ComparisonPlan,
    ComparisonReading,
    ComparisonStore,
    bounded_reason,
)
from vs_runtime.api.infrastructure import prepare_trusted_evaluator, slurm_evaluation_plan

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.inputs import InputBundle
    from vs_project.api import GitRepository, Project
    from vs_sandbox.api.slurm import SlurmOperatorSettings
    from vs_sim.api import BlockingRunner, Clock, Processes, Sleeper
    from vs_slurm.api import Cluster

_DEFAULT_QUEUE_SECONDS = 6 * 3600
"""How long a sample may queue when the operator set no queue limit."""
SAMPLES = 3
"""How many runs the one measurement takes, on different nodes, for its noise estimate."""


@dataclass(frozen=True, slots=True)
class ComparisonReport:
    """One comparison and what is known of it."""

    name: str
    label: str
    reading: ComparisonReading
    cached: bool = False
    """Whether the reading was stored earlier rather than measured now."""
    visible_to_agents: bool = False


@dataclass(frozen=True, slots=True)
class ComparisonHost:
    """What measuring needs from the machine: time, processes, the repository and a scratch."""

    clock: Clock
    sleeper: Sleeper
    blocking: BlockingRunner
    repository: GitRepository
    scratch: Path
    """A fresh directory for the checkout and the plan; the caller removes it."""
    processes: Processes | None
    cluster: Cluster | None
    """An already-open cluster; without one, the settings' own transport is reached by *processes*."""


def _headline(bundle: InputBundle) -> str | None:
    """The metric the task headlines: its declared ``metric``, else its first objective's."""
    if bundle.manifest.metric is not None:
        return bundle.manifest.metric
    axes = load_objectives(bundle.task_root).objective
    return axes[0].name if axes else None


def _store(project: Project) -> ComparisonStore:
    directory = comparison_directory(project.state)
    directory.mkdir(parents=True, exist_ok=True)
    return ComparisonStore(directory)


def stored_comparisons(
    project: Project, bundle: InputBundle, settings: SlurmOperatorSettings
) -> tuple[ComparisonReport, ...]:
    """What a run shows: each declared comparison's stored reading, or "not measured".

    Starts no job and never blocks.
    """
    store = _store(project)
    return tuple(
        ComparisonReport(
            subject.spec.name,
            subject.spec.label,
            store.lookup(subject),
            cached=True,
            visible_to_agents=subject.spec.visible_to_agents,
        )
        for subject in comparison_subjects(bundle, settings)
    )


async def measure_comparisons(  # noqa: PLR0913  # lint-waiver: LW-930097 [PLR0913]; the project, task, machine, host and the user's choices are independent
    project: Project,
    bundle: InputBundle,
    settings: SlurmOperatorSettings,
    host: ComparisonHost,
    *,
    only: tuple[str, ...] = (),
    force: bool = False,
    samples: int = SAMPLES,
) -> tuple[ComparisonReport, ...]:
    """Measure the task's comparisons (or those named in *only*), reusing stored readings.

    *force* measures again whatever is stored. A comparison that cannot be measured is a report
    with its reason, never an exception.
    """
    unknown = sorted(set(only) - {spec.name for spec in bundle.manifest.comparison})
    if unknown:
        message = f"the task declares no comparison named {', '.join(unknown)}"
        raise ValueError(message)
    subjects = tuple(
        subject
        for subject in comparison_subjects(bundle, settings)
        if not only or subject.spec.name in only
    )
    revision = host.repository.resolve_commit("HEAD")
    if revision is None:
        message = "the project has no commit to check out; commit the input first"
        raise ValueError(message)
    state = comparison_directory(project.state)
    prepared = prepare_trusted_evaluator(
        bundle.evaluator_package_root, project.state.machine_cache_directory("evaluator-tools")
    )
    evaluation = slurm_evaluation_plan(
        settings,
        prepared.requirements,
        workspace=bundle.root,
        accuracy_command=bundle.accuracy_command_display,
        benchmark_command=bundle.benchmark_command_display,
        benchmark_output_argument=bundle.benchmark_output_argument,
        state_directory=host.scratch / "plan",
    )
    trusted = trusted_evaluation_plan(
        bundle,
        accuracy_command=bundle.accuracy_command_display,
        benchmark_command=bundle.benchmark_command_display,
        framework_setup_timeout_seconds=settings.config.job_timeout_seconds,
        candidate_startup_causes=resolve_domain(bundle.domain).candidate_startup_causes,
    )
    executors = SlurmSampleExecutors(
        settings,
        evaluation,
        trusted,
        RepositoryCheckouts(
            project.root, host.repository, host.scratch / "checkouts", host.blocking
        ),
        comparison_namespace(project.state),
        state / "slurm-handles",
        clock=host.clock,
        sleeper=host.sleeper,
        processes=host.processes,
        cluster=host.cluster,
    )
    measurer = ComparisonMeasurer(
        _store(project), executors, clock=host.clock, sleeper=host.sleeper
    )
    queue = settings.config.queue_timeout_seconds or _DEFAULT_QUEUE_SECONDS
    budget = float(queue + settings.config.job_timeout_seconds)
    reports: list[ComparisonReport] = []
    for subject in subjects:
        try:
            plan = ComparisonPlan(
                revision=revision,
                samples=samples,
                budget_seconds=budget,
                headline_metric=_headline(bundle),
                remeasure=force,
            )
            outcome = await measurer.measure(subject, plan)
            reading: ComparisonReading = outcome.reading
            cached = outcome.cached
        except OSError as error:
            reading, cached = (
                ComparisonFailed(reason=bounded_reason(f"{type(error).__name__}: {error}")),
                False,
            )
        reports.append(
            ComparisonReport(
                subject.spec.name,
                subject.spec.label,
                reading,
                cached=cached,
                visible_to_agents=subject.spec.visible_to_agents,
            )
        )
    return tuple(reports)


__all__ = [
    "SAMPLES",
    "ComparisonHost",
    "ComparisonReport",
    "measure_comparisons",
    "stored_comparisons",
]
