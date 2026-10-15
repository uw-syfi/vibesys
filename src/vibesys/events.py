"""Presentation-neutral events emitted by the VibeSys execution core."""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, NonNegativeInt, model_validator

import vs_agent.api as _agent_api

# AgentOutputChannel, AgentStatusData, TodoItemData, and ToolResultPayload are
# used directly below. CommandResultPayload and JsonResultPayload are only the
# ToolResultPayload union members; re-exported here so existing importers of
# vibesys.events keep working.
from vs_agent.api import (
    AgentOutputChannel,
    AgentStatusData,
    TodoItemData,
    ToolResultPayload,
)
from vs_runtime.api.infrastructure import EnvironmentStartupStage

CommandResultPayload = _agent_api.CommandResultPayload
JsonResultPayload = _agent_api.JsonResultPayload


class CoreEventType(StrEnum):
    """Closed set of observations produced by a core run."""

    RUN_STARTED = "run_started"
    RUN_STARTUP_PROGRESS = "run_startup_progress"
    EXPERIMENTS_CHANGED = "experiments_changed"
    ITERATIONS_CHANGED = "iterations_changed"
    RUN_FINISHED = "run_finished"
    RUN_FAILED = "run_failed"
    INVOCATION_STARTED = "invocation_started"
    INVOCATION_FINISHED = "invocation_finished"
    AGENT_EXECUTION_STARTED = "agent_execution_started"
    AGENT_EXECUTION_ACTIVITY_CHANGED = "agent_execution_activity_changed"
    AGENT_EXECUTION_FINISHED = "agent_execution_finished"
    PHASE_STARTED = "phase_started"
    PHASE_FINISHED = "phase_finished"
    AGENT_OUTPUT_CHUNK = "agent_output_chunk"
    SUBPROCESS_OUTPUT = "subprocess_output"
    JUDGE_RESULT = "judge_result"
    BENCHMARK_RESULT = "benchmark_result"
    ROUND_FINISHED = "round_finished"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    TODO_UPDATE = "todo_update"
    USAGE_UPDATE = "usage_update"
    RATE_LIMIT_UPDATE = "rate_limit_update"
    QUOTA_PAUSED = "quota_paused"
    QUOTA_RESUMED = "quota_resumed"
    QUOTA_ABANDONED = "quota_abandoned"
    PROVIDER_SWITCHED = "provider_switched"
    GATE_STARTED = "gate_started"
    GATE_FINISHED = "gate_finished"
    WORKSPACE_SNAPSHOT = "workspace_snapshot"
    RUN_CONFIGURED = "run_configured"
    FRAMEWORK_WARNING = "framework_warning"
    WINNER_SELECTED = "winner_selected"
    WORKSTREAM_FINISHED = "workstream_finished"
    TURN_USAGE_RECORDED = "turn_usage_recorded"

    # No TURN_RETRY_WAITING member: a retry wait has no correlating identity to
    # carry yet. `_Ask._lost` (`vs_core.flows._agents`) appends a bare
    # `WakeAt(time=...)` (`vs_core.types.strategy.WakeAt`, one field, no
    # decision id) to the run's timer list, and `_search_run.py` collapses
    # every task's wait into one undifferentiated list before core ever sees
    # it. Publishing this event needs an additive `WakeAt.decision_id` (or
    # equivalent) in vs_core first, which is a core-library change out of
    # scope here, not a `vibesys.run` wiring change like the rest of this
    # table.

    # Run-control transitions (see `vs_runtime.api.infrastructure.RunControlChannel`):
    # request-time events (`*_REQUESTED`, `STEER_QUEUED`, `RESUMED`) come from
    # `RunControl` callers; boundary-consume-time events (`PAUSED`, `STOPPED`,
    # `STEER_CONSUMED`) come from the run boundary or an agent turn. A server
    # projects both onto its own status machine
    # and CONTROL journal; there is no frontend-visible wire event for them.
    STEER_QUEUED = "steer_queued"
    PAUSE_REQUESTED = "pause_requested"
    RESUMED = "resumed"
    STOP_REQUESTED = "stop_requested"
    STEER_CONSUMED = "steer_consumed"
    PAUSED = "paused"
    STOPPED = "stopped"


class EventStatus(StrEnum):
    """Lifecycle status attached to a core event when applicable."""

    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


OutputStream = Literal["stdout", "stderr"]
ExecutionActivityMode = Literal["thinking", "responding", "tool", "waiting"]


class GateKind(StrEnum):
    """Closed set of framework-owned gates a candidate passes through."""

    VALIDATION = "validation"
    ACCURACY = "accuracy"
    BENCHMARK = "benchmark"


class FrameworkSource(StrEnum):
    """Closed set of framework subsystems that emit framework events.

    ``source_label`` on the payloads carries a finer free-text origin (for
    example ``"skills"`` within ``LOOP``) without widening this set.
    """

    GATES = "gates"
    GIT_TRACKING = "git_tracking"
    LOOP = "loop"
    GPU = "gpu"
    SKYPILOT = "skypilot"
    OTHER = "other"


class EventPayload(BaseModel):
    """Immutable base for structured core event payloads.

    Payloads reject keys they do not declare. A payload that drops a field lists it in
    ``retired_keys``, so history recorded before the removal still replays.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    retired_keys: ClassVar[frozenset[str]] = frozenset()
    """Keys a payload once declared: dropped when recorded history replays, never written."""

    @model_validator(mode="before")
    @classmethod
    def _drop_retired_keys(cls, data: object) -> object:
        if cls.retired_keys and isinstance(data, dict):
            return {key: value for key, value in data.items() if key not in cls.retired_keys}
        return data


class InvocationStartedData(EventPayload):
    """Prompts submitted at the start of a model invocation."""

    kind: Literal["invocation_started"] = "invocation_started"
    system_prompt: str
    user_prompt: str


class InvocationFinishedData(EventPayload):
    """Result or error recorded when a model invocation ends."""

    kind: Literal["invocation_finished"] = "invocation_finished"
    result: Any = None
    error: str | None = None


class AgentExecutionActivityData(EventPayload):
    """Current activity reported for an agent execution."""

    kind: Literal["agent_execution_activity_changed"] = "agent_execution_activity_changed"
    mode: ExecutionActivityMode
    summary: str
    tool: str | None = None


class AgentExecutionStartedData(EventPayload):
    """Semantic context for a prompt-to-result agent execution."""

    retired_keys: ClassVar[frozenset[str]] = frozenset({"driver"})
    """``driver`` was always ``"agentshim"`` and was removed in #1486."""

    kind: Literal["agent_execution_started"] = "agent_execution_started"
    stage: str
    attempt: int | None = None
    system_prompt: str = ""
    user_prompt: str = ""
    activity: AgentExecutionActivityData
    provider: str | None = None
    model: str | None = None


class AgentExecutionFinishedData(EventPayload):
    """Terminal result or error for an agent execution."""

    kind: Literal["agent_execution_finished"] = "agent_execution_finished"
    result: Any = None
    error: str | None = None


class RunStartedData(EventPayload):
    """Initial input and loop settings for a core run."""

    kind: Literal["run_started"] = "run_started"
    outer_loop: str
    input: str
    max_rounds: int | None = None
    # Policy-owned role hints let frontends seed per-round placeholders.
    # Empty when the policy does not declare roles.
    expected_roles: tuple[str, ...] = ()


class StartupProgressData(EventPayload):
    """How far a startup stage has come: ``done`` of ``total`` steps, now on ``label``."""

    done: NonNegativeInt
    total: int | None = Field(default=None, ge=1)
    label: str = ""


class RunStartupProgressData(EventPayload):
    """A starting run reached ``stage``, or progressed within it.

    Emitted while the run environment opens, before the run is ready;
    ``progress`` is present when the stage reports it (an image build's steps).
    """

    kind: Literal["run_startup_progress"] = "run_startup_progress"
    stage: EnvironmentStartupStage
    progress: StartupProgressData | None = None


class RunFailureKind(StrEnum):
    """Why a run failed to meet its goal, whether or not it preserved a partial result."""

    BUDGET_EXHAUSTED = "budget_exhausted"
    """Every workstream the run was allowed to start ran without completing the goal."""
    DEADLINE = "deadline"
    """The run's time limit ended it before any candidate was adopted."""
    NO_RESULT = "no_result"
    """Core recorded no terminal strategy result, or the strategy failed before exhausting its
    workstream budget."""


class RunFailure(BaseModel):
    """What a failed run did and why it stopped, as data; frontends choose the wording.

    ``reason`` is the strategy's own account of the stop. ``workstreams_started`` counts
    attempts core admitted, against the ``workstream_budget`` the run was allowed.
    ``candidates_kept`` counts settled candidates eligible for adoption.
    ``adopted_candidate`` names the verified candidate revision core adopted even though the
    strategy still reported a failed outcome. Its None default preserves replay of failure events
    recorded before the adoption fact was added to the event contract.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: RunFailureKind
    reason: str
    workstreams_started: int = Field(ge=0)
    workstream_budget: int = Field(ge=0)
    candidates_kept: int = Field(ge=0)
    adopted_candidate: str | None = Field(default=None, min_length=1, pattern=r"^\S+$")

    @model_validator(mode="after")
    def adoption_matches_failure(self) -> RunFailure:
        """Reject adoption facts that cannot describe a failed run."""
        if self.adopted_candidate is None:
            return self
        if self.candidates_kept == 0:
            message = "adopted_candidate requires at least one candidate in candidates_kept"
            raise ValueError(message)
        if self.kind is RunFailureKind.DEADLINE:
            message = "a deadline failure cannot carry adopted_candidate"
            raise ValueError(message)
        return self


class RunFailedData(EventPayload):
    """Why a run failed, what partial result it preserved, and the counts behind it."""

    kind: Literal["run_failed"] = "run_failed"
    failure: RunFailure


ExperimentsChangeReason = Literal[
    "project_attached", "active_hypothesis_changed", "round_persisted"
]


class ExperimentsChangedData(EventPayload):
    """Reason and revision for a changed experiment projection."""

    kind: Literal["experiments_changed"] = "experiments_changed"
    reason: ExperimentsChangeReason
    # Persisted experiment projection revision. None preserves events recorded
    # before revisioned experiment queries existed.
    revision: int | None = Field(default=None, ge=0)


class IterationsChangedData(EventPayload):
    """Semantic revision of the policy-owned iteration projection."""

    kind: Literal["iterations_changed"] = "iterations_changed"
    revision: str | None = None


class PhaseData(EventPayload):
    """Name and optional attempt number for a loop phase."""

    kind: Literal["phase"] = "phase"
    phase: str
    attempt: int | None = None


class AgentOutputChunkData(EventPayload):
    """Incremental text emitted during agent execution."""

    kind: Literal["agent_output_chunk"] = "agent_output_chunk"
    channel: AgentOutputChannel
    content: str
    status: AgentStatusData | None = None


class ToolCallData(EventPayload):
    """Tool name, call identity, and arguments emitted by an agent."""

    kind: Literal["tool_call"] = "tool_call"
    tool: str
    call_id: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    status: AgentStatusData | None = None


class ToolResultData(EventPayload):
    """Raw tool result and optional structured payload."""

    kind: Literal["tool_result"] = "tool_result"
    tool: str
    call_id: str | None = None
    content: str
    is_error: bool = False
    payload: ToolResultPayload | None = None


class TodoUpdateData(EventPayload):
    """Current todo list reported by an agent."""

    kind: Literal["todo_update"] = "todo_update"
    todos: list[TodoItemData] = Field(default_factory=list)


class UsageUpdateData(EventPayload):
    """Token usage reported by the active model."""

    kind: Literal["usage_update"] = "usage_update"
    input_tokens: int
    context_window: int | None = None
    model: str | None = None


class RateLimitUpdateData(EventPayload):
    """One rate-limit window a provider reported, as semantic data.

    ``exhausted`` is the resolved fact (the provider's own statement, else
    usage at or past 100%); the other fields are what the provider stated,
    ``None`` when it did not state them. ``resets_at`` is epoch seconds.
    """

    kind: Literal["rate_limit_update"] = "rate_limit_update"
    provider: str | None = None
    window: str | None = None
    limit: str | None = None
    used_fraction: float | None = None
    resets_at: float | None = None
    window_minutes: int | None = None
    exhausted: bool


class QuotaPausedData(EventPayload):
    """A turn stopped on a provider capacity limit and the run paused for it.

    ``condition`` is ``quota_exhausted`` (a usage, spend or billing limit) or
    ``rate_limited`` (sustained rate limiting); ``detail`` is the provider's
    diagnostic and ``resets_at`` the epoch second the provider said capacity
    returns, when it said. The run stays paused until it resumes; ``resumes_at`` is the
    epoch second the run's quota policy resumes it by itself, ``None`` when only the
    operator can. ``policy`` is the unattended quota policy; the fallback fields name the
    provider and model an operator may resume with, ``None`` when none is configured.
    """

    kind: Literal["quota_paused"] = "quota_paused"
    provider: str
    condition: Literal["quota_exhausted", "rate_limited"]
    detail: str
    resets_at: float | None = None
    resumes_at: float | None = None
    policy: Literal["pause", "wait", "fail", "fallback"] = "pause"
    fallback_provider: str | None = None
    fallback_model: str | None = None


class QuotaResumedData(EventPayload):
    """A run paused on a capacity limit resumed on the same provider and sent the turn again."""

    kind: Literal["quota_resumed"] = "quota_resumed"
    provider: str
    reason: Literal["operator", "wait_elapsed"] = "operator"


class ProviderSwitchedData(EventPayload):
    """The run replaced a provider by its fallback.

    Sessions opened from now on run on ``to_provider`` and ``to_model`` as fresh
    conversations; a session already open does not move. Every later event
    carries the provider and model it actually ran on.
    """

    kind: Literal["provider_switched"] = "provider_switched"
    from_provider: str
    to_provider: str
    to_model: str
    reason: Literal["policy", "operator"]
    detail: str


class QuotaAbandonedData(EventPayload):
    """The quota policy ended a turn with the quota error instead of pausing or waiting longer."""

    kind: Literal["quota_abandoned"] = "quota_abandoned"
    provider: str
    condition: Literal["quota_exhausted", "rate_limited"]
    detail: str
    reason: str


class SubprocessOutputData(EventPayload):
    """Captured output from a managed subprocess."""

    kind: Literal["subprocess_output"] = "subprocess_output"
    process_id: str
    process_kind: str
    stream: OutputStream
    content: str


class JudgeResultData(EventPayload):
    """Verdict and feedback returned by the judge."""

    kind: Literal["judge_result"] = "judge_result"
    verdict: Literal["pass", "fail"]
    feedback: str
    attempt: int


class BenchmarkResultData(EventPayload):
    """Metric result emitted by a benchmark stage."""

    kind: Literal["benchmark_result"] = "benchmark_result"
    metric: str
    value: FiniteFloat
    unit: str


class RoundFinishedData(EventPayload):
    """Summary of attempt, judge, and performance outcomes for a round."""

    kind: Literal["round_finished"] = "round_finished"
    attempts: int
    judge_verdict: Literal["pass", "fail", "skipped"]
    perf_metric: FiniteFloat | None = None
    perf_unit: str | None = None
    # True when no fresh profile ran this round; such a round records no perf
    # reading (perf_metric stays None).
    profile_skipped: bool


class GateStartedData(EventPayload):
    """One framework gate began evaluating the current candidate.

    Every ``gate_started`` is followed by exactly one ``gate_finished`` for
    the same gate (and recipe, for validation), including reused results.
    """

    kind: Literal["gate_started"] = "gate_started"
    gate: GateKind
    # The validation recipe being executed; None for accuracy and benchmark.
    recipe: str | None = None
    # The trusted command the gate runs, when one is configured.
    command: str | None = None
    source: FrameworkSource = FrameworkSource.GATES
    source_label: str | None = None


class GateFinishedData(EventPayload):
    """Outcome of one framework gate; envelope status carries pass or fail.

    ``metric``/``value``/``unit`` are set only on a passing benchmark gate.
    ``unit`` keeps the historical fallback of the metric name when the
    contract declares no unit. ``output_tail`` carries the trailing command
    output on failure. ``round_label`` groups the per-recipe local-validation
    gates of one attempt back together, so a client folds them into a
    pass-fraction itself rather than the backend aggregating one.
    """

    kind: Literal["gate_finished"] = "gate_finished"
    gate: GateKind
    recipe: str | None = None
    # True when a prior PASS for the exact same input was reused instead of
    # re-running the command.
    reused: bool = False
    metric: str | None = None
    value: FiniteFloat | None = None
    unit: str | None = None
    output_tail: str | None = None
    round_label: str | None = None
    source: FrameworkSource = FrameworkSource.GATES
    source_label: str | None = None


class WorkspaceSnapshotData(EventPayload):
    """A Git tracker outcome: a snapshot, baseline, or exclusion change.

    Exactly one aspect is populated per event: a snapshot attempt carries
    ``label`` (``commit`` is None when there was nothing to commit), a
    trusted-input baseline carries ``baseline``, and a snapshot-exclusion
    change carries ``excluded_paths``.
    """

    kind: Literal["workspace_snapshot"] = "workspace_snapshot"
    label: str = ""
    commit: str | None = None
    baseline: str | None = None
    excluded_paths: tuple[str, ...] = ()
    source: FrameworkSource = FrameworkSource.GIT_TRACKING


class RunConfiguredData(EventPayload):
    """One per run: the resolved configuration a loop starts with."""

    kind: Literal["run_configured"] = "run_configured"
    run_log_path: str
    project_root: str
    model: str | None = None
    # First line of the objective only; the full text lives in run state.
    objective: str | None = None
    search_policy: str | None = None
    benchmark_contract: bool = False
    pareto_objectives: str | None = None
    source: FrameworkSource = FrameworkSource.LOOP


class FrameworkWarningData(EventPayload):
    """A non-fatal framework fault an operator should see."""

    kind: Literal["framework_warning"] = "framework_warning"
    summary: str
    detail: str | None = None
    source: FrameworkSource = FrameworkSource.OTHER
    source_label: str | None = None


class WinnerSelectedData(EventPayload):
    """The run's result was chosen: a retained candidate, or the unchanged trusted input.

    ``selection`` says which; ``revision`` is the revision that becomes the run's result and
    ``settlement_id`` names the settled attempt that produced a candidate (None for the baseline).
    Emitted once per selection, when core records it, before adoption is verified.
    """

    kind: Literal["winner_selected"] = "winner_selected"
    selection: Literal["candidate", "baseline"]
    revision: str
    settlement_id: str | None = None


class WorkstreamFailureKind(StrEnum):
    """Why a workstream ended without a measured candidate. Closed.

    Mirrors ``dynamic.search.api.FailureKind`` member-for-member: a frozen
    wire vocabulary core's own enum can't silently rename underneath.
    """

    STOPPED = "stopped"
    PARENT_UNVERIFIED = "parent-unverified"
    REFUSED = "refused"
    ENDED = "ended"
    OUT_OF_BUDGET = "out-of-budget"
    TURN_FAILED = "turn-failed"
    INVALID_REPLY = "invalid-reply"
    RENDER_FAILED = "render-failed"
    WAIT_REFUSED = "wait-refused"
    CHECKPOINT_DECLINED = "checkpoint-declined"
    NO_CHANGED_CANDIDATE = "no-changed-candidate"
    IMPLEMENTATION_FAILED = "implementation-failed"
    REVIEW_REJECTED = "review-rejected"
    NO_TRUSTED_EVIDENCE = "no-trusted-evidence"
    EVIDENCE_REFUSED = "evidence-refused"
    EVIDENCE_UNDECODABLE = "evidence-undecodable"
    PROFILE_CAPTURE_UNAVAILABLE = "profile-capture-unavailable"
    TURN_LIMIT = "turn-limit"
    REPEATED_TURN = "repeated-turn"


class WorkstreamFailureData(BaseModel):
    """One workstream's typed account of why it ended without a measured candidate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: WorkstreamFailureKind
    role: str | None = None
    turns: int | None = None
    limit: int | None = None
    text: str = ""


class WorkstreamFinishedData(EventPayload):
    """One workstream (one dynamic-run round) ended; its typed failure, if any."""

    kind: Literal["workstream_finished"] = "workstream_finished"
    repeated_error: bool
    last_error: WorkstreamFailureData | None = None


class TurnUsageData(EventPayload):
    """Settled token and cost accounting for one finished agent turn.

    Field names and nullability mirror ``vs_agent.contracts.AgentUsage``
    exactly. Kept separate from ``UsageUpdateData``/``USAGE_UPDATE``: that one
    is a live, overwritten in-flight snapshot (progress), this one is the
    settled per-turn accounting (a durable record), emitted once per turn.
    """

    kind: Literal["turn_usage_recorded"] = "turn_usage_recorded"
    input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    output_tokens: int | None = None
    total_cost_usd: float | None = None
    duration_ms: int | None = None


CoreEventData = Annotated[
    InvocationStartedData
    | InvocationFinishedData
    | AgentExecutionStartedData
    | AgentExecutionActivityData
    | AgentExecutionFinishedData
    | RunStartedData
    | RunStartupProgressData
    | RunFailedData
    | ExperimentsChangedData
    | IterationsChangedData
    | PhaseData
    | AgentOutputChunkData
    | SubprocessOutputData
    | JudgeResultData
    | BenchmarkResultData
    | RoundFinishedData
    | ToolCallData
    | ToolResultData
    | TodoUpdateData
    | UsageUpdateData
    | RateLimitUpdateData
    | QuotaPausedData
    | QuotaResumedData
    | QuotaAbandonedData
    | ProviderSwitchedData
    | GateStartedData
    | GateFinishedData
    | WorkspaceSnapshotData
    | RunConfiguredData
    | FrameworkWarningData
    | WinnerSelectedData
    | WorkstreamFinishedData
    | TurnUsageData,
    Field(discriminator="kind"),
]


class CoreEventWriter(Protocol):
    """Product event surface independent of storage and subscription mechanics."""

    def emit(
        self,
        event_type: CoreEventType,
        text: str = "",
        *,
        data: CoreEventData | None = None,
        **fields: object,
    ) -> CoreEvent:
        """Create and publish one semantic event."""
        ...

    def record(self, event: CoreEvent) -> CoreEvent:
        """Publish one already-created semantic event."""
        ...


class CoreEvent(BaseModel):
    """One immutable core observation, optionally assigned a durable cursor."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int = Field(default=0, ge=0)
    run_id: str = ""
    timestamp: datetime
    type: CoreEventType
    text: str = ""
    status: EventStatus | None = None
    round_label: str | None = None
    agent_kind: str | None = None
    execution_id: str | None = None
    data: CoreEventData | None = None


def make_core_event(
    event_type: CoreEventType,
    text: str = "",
    *,
    timestamp: datetime,
    **fields: object,
) -> CoreEvent:
    """Create an unrecorded event that happened at ``timestamp``."""
    return CoreEvent.model_validate(
        {"timestamp": timestamp, "type": event_type, "text": text, **fields}
    )


def json_value(value: object) -> object:
    """Return a JSON-compatible value without losing useful diagnostics."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    try:
        json.dumps(value)
    except TypeError:
        return repr(value)
    else:
        return value
