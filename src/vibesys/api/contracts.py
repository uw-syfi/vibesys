"""Public contracts for `vibesys.api`: DTOs, enums, and the event sink.

No behavior lives here. Types that already exist in vibesys core are
re-exported instead of duplicated; see each re-export's note for where it
currently lives and when it is due to relocate. `WorkspaceHandle` is
deliberately not a type defined here: a run's workspace is expressed as
`vs_sandbox.HostResource` (see `vibesys.api.session.RunWorkspace`) to avoid a
lib -> core cycle.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from vibesys.agents.contracts import MCPServerSpec
from vibesys.config import Config
from vibesys.constants import DEFAULT_COMPUTE_BACKEND, ComputeBackend
from vibesys.errors import ConfigurationDiagnostic, ConfigurationError
from vibesys.evaluators.input_manifest import InputBundle
from vibesys.events import CoreEvent, EventStatus

if TYPE_CHECKING:
    from vs_sandbox import HostResource, ProjectPathPolicy, Sandbox

# Wave 5 (lib moves): Objective/MetricSpace live in vibesys.loops.metrics
# today because the metric-comparison logic they carry is loop code. The
# lib-move wave relocates the pure DTOs (Objective, MetricSpace) to
# vs_loop_state and leaves comparison logic behind; re-export from there once
# that move lands.
from vibesys.loops.evolve.search_policy import OpenEvolveSearchConfig
from vibesys.loops.metrics import MetricSpace, Objective
from vibesys.profilers import ProfilerKind
from vibesys.repository import RepositoryVisibility
from vibesys.sandbox.run_environment import RunEnvironmentSpec
from vibesys.schemas import CandidateDisposition, PerfDeltaReason

__all__ = [
    "AgentEnvironment",
    "CandidateDisposition",
    "Config",
    "ConfigurationDiagnostic",
    "ConfigurationError",
    "CoreEvent",
    "EventSink",
    "EventStatus",
    "HypothesisRoundView",
    "HypothesisView",
    "LoopKind",
    "MCPServerSpec",
    "MetricSpace",
    "Objective",
    "PerfDeltaReason",
    "ResumeRef",
    "RoundView",
    "RunRequest",
    "RunResult",
    "RunStatus",
    "RunView",
]


class LoopKind(StrEnum):
    """Closed set of outer loops a run can select.

    Mirrors the `--outer-loop` CLI choices documented in
    `entrypoints/headless.py`. No single enum unifies them in core today
    (each loop module spells its own `outer_loop` string/Literal); this is
    the canonical version new callers should use.
    """

    AGENT = "agent"
    PROFILE_GUIDED = "profile-guided"
    PLAIN = "plain"
    EVOLVE = "evolve"


class ResumeRef(BaseModel):
    """Identifies a prior run a new session should resume from."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str


class RunRequest(BaseModel):
    """Everything needed to start or resume one run, independent of transport.

    Fields mirror the union of `run_agent_loop`/`run_evolve_loop`/
    `run_plain_loop`'s keyword arguments (`vibesys.loops.{agent,evolve,plain}
    .loop`). Per-input facts that `input_bundle` already carries (task name/
    root, accuracy/benchmark commands, workspace sources, evaluator paths,
    domain, its own objective text, `profile_guided`, ...) are read from
    `input_bundle` at dispatch time instead of being duplicated here.

    Not every field applies to every `loop`: `metrics` is agent-only, `space`/
    `search_policy`/`openevolve_config`/generation budgets are evolve-only,
    `max_attempts_per_issue`/`max_issues_per_perf_eval` are plain-only, and so
    on -- each `_dispatch_*` helper in `vibesys.api._dispatch` reads only the
    subset its loop understands. `max_rounds` defaults to `None` because its
    concrete default differs by loop (24 for agent, 5 for plain, unused for
    evolve); the dispatch helper substitutes the loop's own default when unset.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    project_root: Path
    loop: LoopKind
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


class RunResult(BaseModel):
    """Terminal outcome of one run.

    TODO(wave-3): extend with a final metrics/candidate summary once
    `RunView`/`RunStore` grow the semantic facts to source it from.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    loop: LoopKind
    succeeded: bool


class RunStatus(StrEnum):
    """Lifecycle status `vibesys.api` can report for a run.

    Deliberately a narrower, separately-owned vocabulary from `EventStatus`
    (not a re-export): `RunQuery.view` on a live session reports `ACTIVE`
    while its loop is running, then `COMPLETED`/`FAILED` from the same
    `RUN_FINISHED`/`RUN_FAILED` transition `create_session` already emits as
    `EventStatus.COMPLETED`/`EventStatus.FAILED` (see `session.py`). `RunStore`
    projects a run from its durable files alone, which carry no lifecycle
    field, so it always reports `UNKNOWN` rather than guessing whether the
    process that wrote them is still attached.
    """

    UNKNOWN = "unknown"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"


class HypothesisRoundView(BaseModel):
    """One hypothesis's round, copied verbatim from its authoritative record.

    A one-way projection, matching `server.api.experiments._round`: it copies
    fields, never groups rounds, selects a baseline, or infers a resolution.
    `hypothesis_outcome`/`candidate_disposition` are pre-resolved to their
    plain string value (rather than exposing the enum members
    `server.api.experiments` resolves them to) because a round's outcome can
    legitimately hold either of two distinct core-private vocabularies
    (`HypothesisOutcome` or `HypothesisResolution`), which a single typed
    field on a boundary DTO cannot express without leaking those types.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    round_number: int
    passed: bool
    reviewed: bool
    hypothesis_outcome: str | None = None
    judge_verdict: Literal["pass", "fail", "deferred"] | None = None
    perf_metric: float | None = None
    perf_unit: str | None = None
    perf_delta_pct: float | None = None
    commit: str | None = None
    official_evaluation: bool = False
    candidate_disposition: str | None = None


class HypothesisView(BaseModel):
    """One hypothesis's full history, matching `server.api.experiments.HypothesisEntry`.

    `title`, `resolved_outcome`, `strategy_disposition`, and `perf_delta_reason`
    are precomputed from `vibesys.loops.agent.model.Hypothesis` (core-private:
    `plan: OrchestratorPlan`, `strategy: HypothesisStrategy`, ...) so this DTO
    exposes only plain strings and the already-boundary-safe `PerfDeltaReason`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    hypothesis_id: str
    title: str | None = None
    claim: str | None = None
    action: str | None = None
    first_round: int
    last_round: int
    rounds: list[HypothesisRoundView] = Field(default_factory=list)
    resolved_outcome: str | None = None
    judge_verdict: Literal["pass", "fail"] | None = None
    # The measurement fields below are copied as one tuple from the
    # hypothesis's single headline measurement: pairing a newer per-round
    # metric with an older causal delta would misreport the measurement.
    perf_metric: float | None = None
    perf_unit: str | None = None
    perf_delta_pct: float | None = None
    # The round that produced the measurement above: `server.api.performance.
    # _latest_measurement` selects the newest headline measurement across
    # hypotheses by this round number, without needing `HypothesisMeasurement`
    # itself.
    perf_metric_round: int | None = None
    perf_metric_name: str | None = None
    perf_direction: Literal["max", "min"] | None = None
    perf_baseline_value: float | None = None
    perf_baseline_round: int | None = None
    perf_baseline_commit: str | None = None
    perf_delta_reason: PerfDeltaReason | None = None
    kept: bool | None = None
    strategy_disposition: str
    strategy_reason: str | None = None
    active: bool
    last_experiment_revision: int
    parent_commit: str | None = None


class RoundView(BaseModel):
    """One run-wide round, in chronological order across all hypotheses.

    Unlike `server.api.service.performance_rounds`, this does not drop rounds
    with no recorded measurement: it is the general-purpose round history,
    not the performance-plot series.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    round_number: int
    commit: str | None = None
    perf_metric: float | None = None
    perf_unit: str | None = None
    passed: bool
    profile_skipped: bool = False
    official_evaluation: bool = False


class RunView(BaseModel):
    """Read-only snapshot of a run's authoritative facts.

    `hypotheses` preserves persisted append order (the order
    `AgentRunState.hypotheses` stores them in), not `HypothesisEntry`'s
    `(first_round, hypothesis_id)` sort: the sort is a
    `server.api.experiments.build_experiment_log` presentation choice, not a
    fact about the run.

    Deliberately excludes `latest_execution`/`configuration_failure`: those
    describe a server-journal-tracked process attached to a run, not a fact
    `vibesys` core state ever records.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    loop: LoopKind
    status: RunStatus
    current_round: int
    active_hypothesis_id: str | None = None
    experiment_revision: int
    hypotheses: list[HypothesisView] = Field(default_factory=list)
    rounds: list[RoundView] = Field(default_factory=list)


class AgentEnvironment(Protocol):
    """A live agent-construction environment opened for one run.

    Returned by `vibesys.api.session.RunAgentHost.open_agent_environment`.
    Carries exactly what `server.chat.factory.build_chat_agent` reads today
    from a `RunAttachment`'s `agent_runtime` plus the `RunEnvironmentSession`
    it opens by hand: the construction inputs (`config`, `compute_backend`,
    `skill_source_dirs`, `project_path_policy`, `host_resources`), the opened
    sandbox's shape (`backends`, `use_docker`, `isolated`), its path
    translation (`agent_path`), and its lifetime (`close`).
    """

    config: Config
    compute_backend: ComputeBackend
    skill_source_dirs: tuple[Path, ...]
    project_path_policy: ProjectPathPolicy
    host_resources: tuple[HostResource, ...]
    backends: dict[str, Sandbox] | None
    use_docker: bool
    isolated: bool

    def agent_path(self, host: Path) -> str:
        """Map a host path to its path inside this environment's sandbox."""
        ...

    def investigation_tools(self) -> tuple[MCPServerSpec, ...]:
        """Return the read-only MCP tool servers for investigating this run's history.

        Each spec launches a `vibesys.api.chat_tools_server` subprocess scoped
        to this environment's run, exposing its read-model
        (`vibesys.api.RunStore`) as MCP tools instead of materializing files
        into the sandbox for a shell to `rg`/`tail`.
        """
        ...

    def close(self) -> None:
        """Release the opened environment session."""
        ...


class EventSink(Protocol):
    """Receives the semantic core event stream for one run.

    Matches the duck-typed subscriber shape already used by
    `vibesys.render.sink.EventHandler` and
    `vibesys.run.event_journal.EventSubscriber`
    (`Callable[[CoreEvent], None]`): any plain function or bound method with
    this signature satisfies it, including `HeadlessRenderer().handle`.
    """

    def __call__(self, event: CoreEvent) -> None:
        """Handle one emitted core event."""
        ...
