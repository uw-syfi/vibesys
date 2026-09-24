"""Deprecated request and selector for the built-in loop adapters.

New orchestrations use ``OrchestrationRunRequest`` with versioned descriptor
options. This module retains the existing built-in CLI request contract.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vibesys.config import Config
from vibesys.constants import DEFAULT_COMPUTE_BACKEND, ComputeBackend
from vibesys.evaluators.input_manifest import InputBundle
from vibesys.loops.evolve.search_policy import OpenEvolveSearchConfig
from vibesys.loops.metrics import MetricSpace
from vibesys.orchestration.request import ResumeRef
from vibesys.profilers import ProfilerKind
from vibesys.repository import RepositoryVisibility
from vibesys.sandbox.run_environment import RunEnvironmentSpec
from vs_project.api import OrchestrationDescriptor


class LoopKind(StrEnum):
    """Deprecated for new policies: built-in outer-loop choices exposed by the CLI.

    Custom orchestrations use `RunRequest.orchestration` with a versioned
    `OrchestrationDescriptor` instead of extending this enum.
    """

    AGENT = "agent"
    PROFILE_GUIDED = "profile-guided"
    PLAIN = "plain"
    EVOLVE = "evolve"


class RunRequest(BaseModel):
    """Deprecated for new policies: built-in loop request with legacy options.

    Select a built-in CLI loop with `loop`, or a registered custom policy
    with `orchestration`; exactly one selector is required.

    Fields mirror the union of `run_agent_loop`/`run_evolve_loop`/
    `run_plain_loop`'s keyword arguments (`vibesys.loops.{agent,evolve,plain}
    .loop`). Per-input facts that `input_bundle` already carries (task name/
    root, accuracy/benchmark commands, workspace sources, evaluator paths,
    domain, its own objective text, `profile_guided`, ...) are read from
    `input_bundle` at dispatch time instead of being duplicated here.

    Not every field applies to every built-in `loop`: `metrics` is agent-only, `space`/
    `search_policy`/`openevolve_config`/generation budgets are evolve-only,
    `max_attempts_per_issue`/`max_issues_per_perf_eval` are plain-only, and so
    on: each built-in entrypoint reads only the subset its loop understands.
    `max_rounds` defaults to `None` because its
    concrete default differs by loop (24 for agent, 5 for plain, unused for
    evolve); the dispatch helper substitutes the loop's own default when unset.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    project_root: Path
    loop: LoopKind | None = None
    orchestration: OrchestrationDescriptor | None = None
    config: Config
    input_bundle: InputBundle
    objective: str | None = None
    resume: ResumeRef | None = None
    exp_name: str | None = None
    runs_dir: Path | None = None

    # Metric spaces (agent: metrics, evolve: space post `--objective` override).
    metrics: MetricSpace = Field(default_factory=MetricSpace)
    space: MetricSpace = Field(default_factory=MetricSpace)

    # Operator-supplied constraints (agent only; already folded into
    # `objective`'s text, also passed structured for the loop's own use).
    operator_constraints: tuple[str, ...] = ()

    # Environment and runtime selection.
    debug: bool = False
    profiler_kind: ProfilerKind = ProfilerKind.AUTO
    skills_dirs: list[str] | None = None
    run_environment: RunEnvironmentSpec | None = None
    agent_backend: str | None = None
    cli_provider: str | None = None
    backend: ComputeBackend = DEFAULT_COMPUTE_BACKEND
    modality: str | None = None
    interface: str = "inprocess"
    inner_loop: str = "multi-agent"
    remote_repo: str | None = None
    repo_visibility: RepositoryVisibility = RepositoryVisibility.PRIVATE

    # Agent-loop budgets.
    max_rounds: int | None = None
    max_retries_per_round: int = 3
    judge_every: int = 3
    official_eval_every: int = 3
    memory_layout: str = "files"

    # Plain-loop budgets.
    max_attempts_per_issue: int = 3
    max_issues_per_perf_eval: int = 3

    # Evolve-loop budgets and search configuration.
    max_generations: int = 8
    children_per_generation: int = 2
    k_top_inspirations: int = 2
    k_random_inspirations: int = 2
    selection_temperature: float = 0.5
    seed: int | None = None
    frontier_bias: float = 0.7
    bootstrap_max_attempts: int = 5
    keep_deployments: bool = False
    max_parallelism: int = 1
    search_policy: str | None = None
    openevolve_config: OpenEvolveSearchConfig | None = None

    @model_validator(mode="after")
    def _validate_selection(self) -> Self:
        if (self.loop is None) == (self.orchestration is None):
            message = "select exactly one of loop or orchestration"
            raise ValueError(message)
        if self.orchestration is not None and self.orchestration.id in {
            kind.value for kind in LoopKind
        }:
            message = "built-in orchestration IDs must use loop selection"
            raise ValueError(message)
        return self

    @property
    def orchestration_id(self) -> str:
        """Return the selected stable ID without assuming a built-in loop."""
        if self.orchestration is not None:
            return self.orchestration.id
        if self.loop is None:
            message = "RunRequest has no orchestration selection"
            raise ValueError(message)
        return self.loop.value

    @property
    def selected_loop(self) -> LoopKind | str:
        """Keep the enum for built-ins and expose custom IDs as strings."""
        return self.loop if self.loop is not None else self.orchestration_id
