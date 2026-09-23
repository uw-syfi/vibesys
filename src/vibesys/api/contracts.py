"""Public contracts for `vibesys.api`: DTOs, enums, and the event sink.

No behavior lives here. Types that already exist in vibesys core are
re-exported instead of duplicated. `WorkspaceHandle` is deliberately not a
type defined here: a run's workspace is expressed as `vs_sandbox.HostResource`
(see `vibesys.api.session.RunWorkspace`) to avoid a lib -> core cycle.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

# Legacy public names remain here for import compatibility.
from vibesys.api._orchestrations.legacy_request import LoopKind, RunRequest
from vibesys.api.run_request import OrchestrationRunRequest, ResumeRef, RunRequestLike
from vibesys.config import Config
from vibesys.errors import ConfigurationDiagnostic, ConfigurationError
from vibesys.events import CoreEvent, EventStatus
from vs_agent.api import MCPServerSpec
from vs_project.api import OrchestrationDescriptor

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.skills import SkillSelection
    from vs_sandbox.api import HostResource, ProjectPathPolicy, Sandbox

# Objective/MetricSpace live in vibesys.loops.metrics because the
# metric-comparison logic they carry is loop code.
from vibesys.loops.metrics import MetricSpace, Objective
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
    "OrchestrationDescriptor",
    "OrchestrationRunRequest",
    "PerfDeltaReason",
    "ResumeRef",
    "RoundView",
    "RunRequest",
    "RunRequestLike",
    "RunResult",
    "RunStatus",
    "RunView",
]


# Compatibility alias; generic framework code accepts ``RunRequestLike``.
type AnyRunRequest = RunRequest | OrchestrationRunRequest


class RunResult(BaseModel):
    """Terminal outcome of one run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    loop: LoopKind | str = Field(union_mode="left_to_right")
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
    loop: LoopKind | str = Field(union_mode="left_to_right")
    status: RunStatus
    current_round: int
    active_hypothesis_id: str | None = None
    experiment_revision: int
    hypotheses: list[HypothesisView] = Field(default_factory=list)
    rounds: list[RoundView] = Field(default_factory=list)


class AgentEnvironment(Protocol):
    """A live agent-construction environment opened for one run.

    Returned by `vibesys.api.session.RunAgentHost.open_agent_environment`.
    Carries exactly what `server.chat.factory.build_chat_agent` needs to build
    a sibling agent over the run's workspace: the construction inputs (`config`,
    `skill_selection`,
    `skill_source_dirs`, `project_path_policy`, `host_resources`), the opened
    sandbox's shape (`backends`, `use_docker`, `isolated`), its path
    translation (`agent_path`), and its lifetime (`close`).

    Every data member is a read-only property, not a plain attribute: no
    caller writes any of them, and this protocol's sole real implementation
    (`vibesys.api.session._OpenedAgentEnvironment`) is a frozen dataclass, so
    a plain attribute (implicitly read-write) would make it structurally
    incompatible with this protocol.
    """

    @property
    def config(self) -> Config:
        """This environment's agent configuration."""
        ...

    @property
    def skill_selection(self) -> SkillSelection:
        """The skill-pruning policy a sibling agent should apply while copying."""
        ...

    @property
    def skill_source_dirs(self) -> tuple[Path, ...]:
        """Directories a sibling agent should load skills from."""
        ...

    @property
    def project_path_policy(self) -> ProjectPathPolicy:
        """The path policy governing this environment's project access."""
        ...

    @property
    def host_resources(self) -> tuple[HostResource, ...]:
        """Host resources mounted into this environment."""
        ...

    @property
    def backends(self) -> dict[str, Sandbox] | None:
        """Sandbox handles for this environment's execution surfaces, if sandboxed."""
        ...

    @property
    def use_docker(self) -> bool:
        """Whether this environment's CLI runs sandboxed under Docker."""
        ...

    @property
    def isolated(self) -> bool:
        """Whether this environment runs with an isolated (non-host-mounted) workspace."""
        ...

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
    this signature satisfies it, including a headless renderer's bound
    `.handle` method.
    """

    def __call__(self, event: CoreEvent) -> None:
        """Handle one emitted core event."""
        ...
