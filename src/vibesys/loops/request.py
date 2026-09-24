"""Structural interface for run settings consumed by the loop implementations.

The public ``RunRequest`` DTO is owned by ``vibesys.api`` and includes
loop-specific types. Core loop modules cannot import that facade without
creating a dependency cycle, so they type against this read-only structural
view. The API DTO satisfies it directly; no settings are copied or stored
here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.config import Config
    from vibesys.constants import ComputeBackend
    from vibesys.evaluators.input_manifest import InputBundle
    from vibesys.loops.evolve.search_policy import OpenEvolveSearchConfig
    from vibesys.loops.metrics import MetricSpace
    from vibesys.profilers import ProfilerKind
    from vibesys.repository import RepositoryVisibility
    from vibesys.sandbox.run_environment import RunEnvironmentSpec


class ResumeReference(Protocol):
    """Read-only run id used by the resume field."""

    @property
    def run_id(self) -> str:
        """Identifier of the run selected for resume."""
        ...


class LoopKindValue(Protocol):
    """Read-only enum shape needed to select an outer loop."""

    @property
    def value(self) -> str:
        """String value used by loop-specific run metadata."""
        ...


class LoopRunRequest(Protocol):
    """Read-only subset of a public run request consumed by loop code."""

    @property
    def config(self) -> Config:
        """Validated application configuration for this run."""
        ...

    @property
    def input_bundle(self) -> InputBundle:
        """Resolved task and evaluator inputs for this run."""
        ...

    @property
    def loop(self) -> LoopKindValue:
        """Selected outer-loop kind."""
        ...

    @property
    def objective(self) -> str | None:
        """Objective text, when this loop requires one."""
        ...

    @property
    def resume(self) -> ResumeReference | None:
        """Prior run selected for resume, if any."""
        ...

    @property
    def exp_name(self) -> str | None:
        """Requested name for a fresh run."""
        ...

    @property
    def runs_dir(self) -> Path | None:
        """Root directory for this run's artifacts."""
        ...

    @property
    def debug(self) -> bool:
        """Whether the run pauses for debug interaction."""
        ...

    @property
    def profiler_kind(self) -> ProfilerKind:
        """Profiler selected for this run."""
        ...

    @property
    def skills_dirs(self) -> list[str] | None:
        """Additional skill directories visible to agents."""
        ...

    @property
    def run_environment(self) -> RunEnvironmentSpec | None:
        """Requested execution environment, if explicitly selected."""
        ...

    @property
    def agent_backend(self) -> str | None:
        """Requested agent backend override."""
        ...

    @property
    def cli_provider(self) -> str | None:
        """Requested CLI provider override."""
        ...

    @property
    def backend(self) -> ComputeBackend:
        """Compute backend selected for the run."""
        ...

    @property
    def modality(self) -> str | None:
        """Optional task modality supplied by the operator."""
        ...

    @property
    def remote_repo(self) -> str | None:
        """Optional remote Git repository to update."""
        ...

    @property
    def repo_visibility(self) -> RepositoryVisibility:
        """Visibility to assign when creating a remote repository."""
        ...

    @property
    def max_rounds(self) -> int | None:
        """Optional round budget used by the plain loop."""
        ...

    @property
    def metrics(self) -> MetricSpace:
        """Metric space used by the agent loop."""
        ...

    @property
    def operator_constraints(self) -> tuple[str, ...]:
        """Additional constraints folded into the agent objective."""
        ...

    @property
    def max_retries_per_round(self) -> int:
        """Maximum implementation retries in one agent round."""
        ...

    @property
    def judge_every(self) -> int:
        """Round interval for judge calls."""
        ...

    @property
    def official_eval_every(self) -> int:
        """Round interval for official evaluations."""
        ...

    @property
    def memory_layout(self) -> str:
        """Issue-board memory representation selected for the run."""
        ...

    @property
    def inner_loop(self) -> str:
        """Agent execution mode selected for each round."""
        ...

    @property
    def interface(self) -> str:
        """Evaluator-to-candidate process boundary mode."""
        ...

    @property
    def max_attempts_per_issue(self) -> int:
        """Maximum implementer attempts per issue."""
        ...

    @property
    def max_issues_per_perf_eval(self) -> int:
        """Maximum new issues filed by one performance evaluation."""
        ...

    @property
    def space(self) -> MetricSpace:
        """Metric space used by evolutionary selection."""
        ...

    @property
    def max_generations(self) -> int:
        """Evolutionary generation budget."""
        ...

    @property
    def children_per_generation(self) -> int:
        """Candidate count in each evolutionary generation."""
        ...

    @property
    def k_top_inspirations(self) -> int:
        """Number of top candidates used as inspirations."""
        ...

    @property
    def k_random_inspirations(self) -> int:
        """Number of randomly selected inspirations."""
        ...

    @property
    def selection_temperature(self) -> float:
        """Temperature used when sampling candidate parents."""
        ...

    @property
    def seed(self) -> int | None:
        """Seed for reproducible search policy and selection."""
        ...

    @property
    def frontier_bias(self) -> float:
        """Probability of sampling from the metric frontier."""
        ...

    @property
    def bootstrap_max_attempts(self) -> int:
        """Maximum seed construction attempts before aborting."""
        ...

    @property
    def keep_deployments(self) -> bool:
        """Whether candidate deployments remain after evaluation."""
        ...

    @property
    def max_parallelism(self) -> int:
        """Maximum concurrent candidate evaluations."""
        ...

    @property
    def search_policy(self) -> str | None:
        """Requested evolutionary search policy."""
        ...

    @property
    def openevolve_config(self) -> OpenEvolveSearchConfig | None:
        """Optional configuration for the OpenEvolve search policy."""
        ...
