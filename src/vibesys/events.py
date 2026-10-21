"""Presentation-neutral events emitted by the VibeSys execution core."""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Literal, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    NonNegativeFloat,
    NonNegativeInt,
    field_validator,
    model_validator,
)

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
from vs_core.api import ComparisonVerdict, EndReason, SpanId, TaskObjective, WireSpan
from vs_redaction.api import redact_diagnostic_text
from vs_runtime.api.infrastructure import (
    AgentReply,
    EnvironmentStartupStage,
    EnvironmentStartupState,
    EnvironmentWaitCondition,
    SteerDelivery,
)

CommandResultPayload = _agent_api.CommandResultPayload
JsonResultPayload = _agent_api.JsonResultPayload


class CoreEventType(StrEnum):
    """Closed set of observations produced by a core run."""

    RUN_STARTED = "run_started"
    RUN_STARTUP_PROGRESS = "run_startup_progress"
    WORKSPACE_ROOT_PROBE = "workspace_root_probe"
    EXPERIMENTS_CHANGED = "experiments_changed"
    ITERATIONS_CHANGED = "iterations_changed"
    RUN_FINISHED = "run_finished"
    RUN_FAILED = "run_failed"
    RUN_ENDING = "run_ending"
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
    QUOTA_WAITING = "quota_waiting"
    QUOTA_WAIT_CANCELLED = "quota_wait_cancelled"
    PROVIDER_SWITCHED = "provider_switched"
    GATE_STARTED = "gate_started"
    GATE_FINISHED = "gate_finished"
    WORKSPACE_SNAPSHOT = "workspace_snapshot"
    RUN_CONFIGURED = "run_configured"
    FRAMEWORK_WARNING = "framework_warning"
    WINNER_SELECTED = "winner_selected"
    INPUT_MEASUREMENT_FAILED = "input_measurement_failed"
    MEASUREMENT_COMPARED = "measurement_compared"
    COMPARISONS_READ = "comparisons_read"
    WORKSTREAM_FINISHED = "workstream_finished"
    TURN_USAGE_RECORDED = "turn_usage_recorded"
    SPAN_STARTED = "span_started"
    SPAN_UPDATED = "span_updated"
    SPAN_ENDED = "span_ended"

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
    # request-time events (`*_REQUESTED`, `STEER_QUEUED`, `GUIDANCE_QUEUED`, `WITHDRAW_QUEUED`,
    # `ACTION_QUEUED`, `RESUMED`) come from
    # `RunControl` callers; boundary-consume-time events (`PAUSED`, `STOPPED`,
    # `STEER_CONSUMED`, `STEER_DELIVERED_MID_TURN`, `GUIDANCE_REACHED`, `STEER_DROPPED`,
    # `GUIDANCE_ENDED`, `CONTROL_REFUSED`, `REQUEST_REJECTED`, `ACTION_APPLIED`) come from the
    # run boundary or an agent turn. A server
    # projects both onto its own status machine
    # and CONTROL journal. Each steer-lifecycle one also carries a `SteerDeliveryData`, which the
    # server publishes as the typed `steer_delivery` wire event.
    STEER_QUEUED = "steer_queued"
    GUIDANCE_QUEUED = "guidance_queued"
    WITHDRAW_QUEUED = "withdraw_queued"
    GUIDANCE_ENDED = "guidance_ended"
    GUIDANCE_REACHED = "guidance_reached"
    PAUSE_REQUESTED = "pause_requested"
    PARK_REQUESTED = "park_requested"
    PARKED = "parked"
    RESUMED = "resumed"
    STOP_REQUESTED = "stop_requested"
    STEER_CONSUMED = "steer_consumed"
    STEER_DELIVERED_MID_TURN = "steer_delivered_mid_turn"
    STEER_DROPPED = "steer_dropped"
    CONTROL_REFUSED = "control_refused"
    REQUEST_REJECTED = "request_rejected"
    PAUSED = "paused"
    STOPPED = "stopped"
    ACTION_QUEUED = "action_queued"
    ACTION_APPLIED = "action_applied"


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
    BUILD = "build"
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


class SteerDeliveryData(EventPayload):
    """One step in an operator steer's life, by the control id the operator sent it under.

    Carried by the steer-lifecycle run-control events (queued, reached a turn, in force,
    dropped, ended, withdrawal queued, refused); ``SteerDelivery`` is its one definition.
    """

    kind: Literal["steer_delivery"] = "steer_delivery"
    delivery: SteerDelivery


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
    reasoning_effort: str | None = None
    """The reasoning effort the session was configured with; ``None`` when the provider's default."""
    harness_version: str | None = None
    """The provider CLI's version when a driver reported it; ``None`` when unknown."""


class AgentExecutionFinishedData(EventPayload):
    """Terminal result or error for an agent execution."""

    kind: Literal["agent_execution_finished"] = "agent_execution_finished"
    result: Any = None
    error: str | None = None
    reply: AgentReply | None = Field(default=None, exclude_if=lambda value: value is None)
    """The typed reply of a structured turn, read through its role's reply model; ``result`` is
    the raw text it came from. Absent for an unstructured turn or one whose text was no valid
    reply."""


class EvaluatorIdentity(BaseModel):
    """The evaluator package a run measures with, as the task's manifest pins it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)


class ObjectiveOverview(BaseModel):
    """What the run optimizes: the task's objective and the metric it is judged on.

    ``unit`` and ``direction`` come from the run's own measurements and objectives once it has
    them; ``correctness_check`` is the argv of the check a candidate must pass (empty when the
    task gates on none).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    """The task's ``OBJECTIVE.md``, as Markdown."""
    metric: str | None = None
    unit: str | None = None
    direction: Literal["max", "min"] | None = None
    objectives: tuple[TaskObjective, ...] = ()
    """Every axis candidates are ranked on, headline first, as the run's metric space declares
    them (empty when it declares none). Each candidate's gate span carries a reading per axis,
    with its unit."""
    correctness_check: tuple[str, ...] = ()


class BenchmarkVersions(BaseModel):
    """What measured the run.

    ``evaluator`` is the evaluator package when the task pins one; a source-tree evaluator has
    no version, so it is None.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    evaluator: EvaluatorIdentity | None = None


class RunOverview(BaseModel):
    """The run's objective and what measures it: the Overview's header facts.

    One definition for the core event and the wire: the run's ``run_started`` event carries it,
    and the server's snapshot carries it with the unit and direction its measurements add.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    overview_version: Literal[1] = 1
    objective: ObjectiveOverview
    benchmarks: BenchmarkVersions = BenchmarkVersions()


class RunStartedData(EventPayload):
    """Initial input and loop settings for a core run."""

    kind: Literal["run_started"] = "run_started"
    outer_loop: str
    input: str
    max_rounds: int | None = None
    # Policy-owned role hints let frontends seed per-round placeholders.
    # Empty when the policy does not declare roles.
    expected_roles: tuple[str, ...] = ()
    overview: RunOverview | None = Field(default=None, exclude_if=lambda value: value is None)
    """The run's objective and evaluator, as the task's input bundle declares them."""


class StartupProgressData(EventPayload):
    """How far a startup stage has come: ``done`` of ``total`` steps, now on ``label``."""

    done: NonNegativeInt
    total: int | None = Field(default=None, ge=1)
    label: str = ""


class RunStartupProgressData(EventPayload):
    """A starting run's ``stage`` began, moved, waited, or finished.

    Emitted while the run environment opens, before the run is ready. Each
    stage reports ``running`` when it begins (and at each step of its
    ``progress``, an image build's steps), ``waiting`` while it is blocked on
    something outside this process (``condition``, ``elapsed_seconds`` waited
    so far), and ``finished`` with the ``elapsed_seconds`` it took. A stage that
    fails reports no ``finished``: the run's failure says why it ended.
    """

    kind: Literal["run_startup_progress"] = "run_startup_progress"
    stage: EnvironmentStartupStage
    progress: StartupProgressData | None = None
    state: EnvironmentStartupState = EnvironmentStartupState.RUNNING
    condition: EnvironmentWaitCondition | None = None
    """What a ``waiting`` stage waits on; present exactly then."""
    elapsed_seconds: NonNegativeFloat | None = None
    """Seconds waited (``waiting``) or taken (``finished``); absent while ``running``."""

    @model_validator(mode="after")
    def _detail_matches_state(self) -> RunStartupProgressData:
        waiting = self.state is EnvironmentStartupState.WAITING
        running = self.state is EnvironmentStartupState.RUNNING
        if (self.condition is not None) != waiting:
            message = "condition is present exactly when state is waiting"
            raise ValueError(message)
        if (self.elapsed_seconds is None) != running:
            message = "elapsed_seconds is present exactly when state is not running"
            raise ValueError(message)
        if self.progress is not None and not running:
            message = "progress is present only while state is running"
            raise ValueError(message)
        return self


class WorkspaceRootProbeData(EventPayload):
    """The run's probe of its local Slurm workspace root, which runs beside the run.

    The probe is a small Slurm job that proves the compute nodes share the root the run
    stages into. The run starts without it; each Slurm submission (a trusted evaluation,
    an agent's GPU job) waits for it. ``queued``: the probe job ``job_id`` still waits in
    the scheduler's queue, for the scheduler's ``pending_reason`` (None when it gave
    none), after ``waited_seconds`` of at most ``queue_timeout_seconds``; it is reported
    at once, then on a bounded cadence or when the reason changes. ``retrying``: an
    attempt ended with ``failure`` without saying anything about the root (the queue
    never scheduled it), and attempt number ``attempt`` follows in ``retry_in_seconds``;
    Slurm work keeps waiting. ``proven``: Slurm work goes ahead. ``failed``: every Slurm
    submission of the run is refused with ``failure``, and the run ends as failed.
    """

    kind: Literal["workspace_root_probe"] = "workspace_root_probe"
    state: Literal["queued", "retrying", "proven", "failed"]
    job_id: str | None = None
    pending_reason: str | None = None
    waited_seconds: FiniteFloat | None = Field(default=None, ge=0)
    queue_timeout_seconds: FiniteFloat | None = Field(default=None, ge=1)
    failure: str | None = None
    attempt: int | None = Field(default=None, ge=2)
    retry_in_seconds: FiniteFloat | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _fields_follow_the_state(self) -> WorkspaceRootProbeData:
        queued = self.state == "queued"
        if queued != (self.job_id is not None and self.waited_seconds is not None):
            message = "job_id and waited_seconds are set exactly when the probe is queued"
            raise ValueError(message)
        if (self.state in {"failed", "retrying"}) != (self.failure is not None):
            message = "failure is set exactly when the probe failed or retries"
            raise ValueError(message)
        retrying = self.state == "retrying"
        if retrying != (self.attempt is not None and self.retry_in_seconds is not None):
            message = "attempt and retry_in_seconds are set exactly when the probe retries"
            raise ValueError(message)
        return self


class RunFailureKind(StrEnum):
    """Why a run failed to meet its goal, whether or not it preserved a partial result."""

    BUDGET_EXHAUSTED = "budget_exhausted"
    """Every workstream the run was allowed to start ran without completing the goal."""
    DEADLINE = "deadline"
    """The run's time limit ended it before any candidate was adopted."""
    NO_RESULT = "no_result"
    """Core recorded no terminal strategy result, or the strategy failed before exhausting its
    workstream budget."""
    RUN_ENVIRONMENT_UNAVAILABLE = "run_environment_unavailable"
    """The run environment could run none of the run's work (a local Slurm root its probe
    could not prove shared), so the run stopped; ``reason`` is the environment's error."""


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


class RunEndingData(EventPayload):
    """How a core run's durable record stands as the run ends, emitted once just before its end.

    ``reason`` is core's ``EndReason`` when the run recorded a result, ``None`` when it ended
    without one (an error, or a stop that left work unsettled). ``resumable`` is whether a
    resume continues it: its record is not terminal. ``rounds_finished`` counts its
    workstreams (rounds) that reached their end. ``summary`` is core's account of the result.
    ``uncompared`` is whether the result adopted a candidate never compared with the input (it
    failed its own measurement), however the run ended.
    """

    kind: Literal["run_ending"] = "run_ending"
    reason: EndReason | None
    summary: str = ""
    resumable: bool
    rounds_finished: NonNegativeInt
    uncompared: bool = False


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

    ``condition`` is ``quota_exhausted`` (a usage, spend or billing limit),
    ``rate_limited`` (sustained rate limiting) or ``unavailable`` (a provider outage:
    only the stopped turn waits, under its outage budget, and the run is not paused);
    ``detail`` is the provider's
    diagnostic and ``resets_at`` the epoch second the provider said capacity
    returns, when it said. The run stays paused until it resumes; ``resumes_at`` is the
    epoch second the run's quota policy resumes it by itself, ``None`` when only the
    operator can. ``policy`` is the unattended quota policy; the fallback fields name the
    provider and model an operator may resume with, ``None`` when none is configured.
    """

    kind: Literal["quota_paused"] = "quota_paused"
    provider: str
    condition: Literal["quota_exhausted", "rate_limited", "unavailable"]
    detail: str
    resets_at: float | None = None
    resumes_at: float | None = None
    policy: Literal["pause", "wait", "fail", "fallback"] = "pause"
    fallback_provider: str | None = None
    fallback_model: str | None = None


class QuotaResumedData(EventPayload):
    """A turn stopped on a capacity limit is sent again on the same provider.

    ``condition`` is the limit it stopped on, as in ``quota_paused`` (``None`` in events
    recorded before the field existed). ``waited_seconds`` is how long that stop held the
    turn: for ``unavailable`` the turn's own backoff, during which the rest of the run went
    on; otherwise the run's pause.
    """

    kind: Literal["quota_resumed"] = "quota_resumed"
    provider: str
    condition: Literal["quota_exhausted", "rate_limited", "unavailable"] | None = None
    reason: Literal["operator", "wait_elapsed"] = "operator"
    waited_seconds: float = 0.0


class QuotaWaitingData(EventPayload):
    """At the operator's request, the run paused on a capacity limit resumes when capacity returns.

    ``resumes_at`` is the epoch second it resumes (the provider's reset time plus a margin),
    ``None`` when the provider gave no reset time and the run tries again after the quota
    policy's retry interval. A later request replaces this one.
    """

    kind: Literal["quota_waiting"] = "quota_waiting"
    provider: str
    resumes_at: float | None = None


class QuotaWaitCancelledData(EventPayload):
    """The operator ended a wait for capacity: ``resumed`` the run now, or ``paused`` it again."""

    kind: Literal["quota_wait_cancelled"] = "quota_wait_cancelled"
    provider: str
    reason: Literal["resumed", "paused"]


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
    """The quota policy ended a turn with the quota error instead of pausing or waiting longer.

    ``waited_seconds`` is how long this last stop held the turn before it ended (zero when
    it ended without waiting, and in events recorded before the field existed).
    """

    kind: Literal["quota_abandoned"] = "quota_abandoned"
    provider: str
    condition: Literal["quota_exhausted", "rate_limited", "unavailable"]
    detail: str
    reason: str
    waited_seconds: float = 0.0


class SubprocessOutputData(EventPayload):
    """Captured output from a managed subprocess."""

    kind: Literal["subprocess_output"] = "subprocess_output"
    span_id: SpanId | None = Field(default=None, exclude_if=lambda value: value is None)
    """The operation that owns this stage, when its submission named one."""
    process_id: str
    process_kind: str
    stream: OutputStream
    content: str

    @field_validator("content")
    @classmethod
    def _redact_content(cls, value: str) -> str:
        return redact_diagnostic_text(value)


class JudgeResultData(EventPayload):
    """Verdict and feedback returned by the judge.

    ``skipped``/``skip_reason`` cover a round that never reached review
    (``RoundFinishedData.judge_verdict == "skipped"`` is the existing
    round-level fact this mirrors at the judge layer). ``implementer_outcome``
    is the implementer's own self-assessed status shown in the judge's place
    when review was skipped, using ``HypothesisOutcome``'s closed vocabulary
    (``vibesys.hypothesis.history.HypothesisOutcome``) since that is the
    existing record of it (``RoundRecord.outcome``).

    No code in this tree constructs ``JUDGE_RESULT`` today (verified by a
    repo-wide search): a judge verdict currently reaches the wire only as
    ``RoundFinishedData.judge_verdict``. These three fields are additive and
    safe to declare regardless, but, like ``RunConfiguredData``'s new fields
    above, they carry no data until a producer exists; a separate,
    pre-existing gap.
    """

    kind: Literal["judge_result"] = "judge_result"
    verdict: Literal["pass", "fail"]
    feedback: str
    attempt: int
    skipped: bool = False
    skip_reason: str | None = None
    implementer_outcome: (
        Literal["continue", "supported", "nominated", "disproven", "implementation_failed"] | None
    ) = None


class BenchmarkResultData(EventPayload):
    """Metric result emitted by a benchmark stage."""

    kind: Literal["benchmark_result"] = "benchmark_result"
    metric: str
    value: FiniteFloat
    unit: str


class RoundFinishedData(EventPayload):
    """Summary of judge and performance outcomes for one recorded round.

    A projected dynamic implementation round emits this alongside
    ``workstream_finished`` with the same non-null envelope ``round_label``.
    ``attempts`` is strategy-neutral; dynamic currently reports one and does
    not use it as an implementer-turn count. The CLI glossary owns the public
    round, workstream, and attempt definitions.
    """

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
    span_id: SpanId | None = Field(default=None, exclude_if=lambda value: value is None)
    """The operation that owns this stage, when its submission named one."""
    gate: GateKind
    # The validation recipe being executed; None for build, accuracy and benchmark.
    recipe: str | None = None
    # The trusted command the gate runs, when one is configured.
    command: str | None = None
    source: FrameworkSource = FrameworkSource.GATES
    source_label: str | None = None


class GateFinishedData(EventPayload):
    """Outcome of one framework gate; envelope status carries pass or fail.

    ``metric``/``value``/``unit`` are set only on a passing benchmark gate.
    ``unit`` keeps the historical fallback of the metric name when the
    contract declares no unit. ``output_tail`` carries bounded, credential-redacted
    trailing command output on failure. ``round_label`` groups the per-recipe local-validation
    gates of one attempt back together, so a client folds them into a
    pass-fraction itself rather than the backend aggregating one.
    """

    kind: Literal["gate_finished"] = "gate_finished"
    span_id: SpanId | None = Field(default=None, exclude_if=lambda value: value is None)
    """The operation that owns this stage, when its submission named one."""
    gate: GateKind
    recipe: str | None = None
    # True when a prior result for the same input was reused instead of
    # re-running the command (a prior pass, or an earlier run's pass or failure).
    reused: bool = False
    metric: str | None = None
    value: FiniteFloat | None = None
    unit: str | None = None
    output_tail: str | None = None
    round_label: str | None = None
    source: FrameworkSource = FrameworkSource.GATES
    source_label: str | None = None

    @field_validator("output_tail")
    @classmethod
    def _redact_output_tail(cls, value: str | None) -> str | None:
        return None if value is None else redact_diagnostic_text(value)


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
    """One per run: the resolved configuration a loop starts with.

    ``max_continuation_rounds`` and ``max_unmeasured_turns`` are Hypothesis-
    and Workstream-granularity limits (``DynamicConfig.max_continuation_rounds``
    and ``DynamicConfig.max_unmeasured_turns``); they are run-config facts set
    once, so they live here rather than on the narrower event that interprets
    them. ``None`` for a policy that declares no such limit. The specific
    limit value in force when a limit actually triggers survives separately
    as ``WorkstreamFailureData.limit`` on ``WorkstreamFinishedData``.

    No code in this tree constructs ``RUN_CONFIGURED`` today (verified by a
    repo-wide search): the emitter that `src/headless/framework_events.py`
    already renders for was removed in a past refactor and never replaced.
    These two fields are additive and safe to declare regardless, but they
    will carry no data until a run-startup producer is restored; that is a
    separate, pre-existing gap, not introduced here.
    """

    kind: Literal["run_configured"] = "run_configured"
    run_log_path: str
    project_root: str
    model: str | None = None
    # First line of the objective only; the full text lives in run state.
    objective: str | None = None
    search_policy: str | None = None
    benchmark_contract: bool = False
    pareto_objectives: str | None = None
    max_continuation_rounds: int | None = None
    max_unmeasured_turns: int | None = None
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


class InputMeasurementFailureKind(StrEnum):
    """Why the run's input revision failed its own trusted measurement. Closed.

    Mirrors ``vibesys.candidates.api.InputFailureKind`` member-for-member: a frozen wire
    vocabulary the candidates library's own enum can't silently rename underneath.
    """

    NO_EVIDENCE = "no-evidence"
    READINGS_REFUSED = "readings-refused"
    READINGS_UNDECODABLE = "readings-undecodable"
    ACCURACY_FAILED = "accuracy-failed"
    BENCHMARK_FAILED = "benchmark-failed"
    WAIT_ENDED = "wait-ended"


class InputMeasurementFailedData(EventPayload):
    """The run's input revision failed its trusted measurement, so it has no trusted baseline.

    ``failure`` says how, ``stage`` names the gate stage whose reading failed (None when no
    stage reading did), and ``summary`` is the redacted tail of what that stage printed.
    ``attempts`` is 2 when the first failure showed no fault of the input and it was measured
    again. ``compared`` is whether candidates are still compared with the input: its accuracy
    check failed but its benchmark passed, so its numbers stay the bar. Otherwise candidates are
    judged on the task's gates alone, and a run that adopts one ends "adopted without an input
    comparison". Emitted once per run, when core first commits the failure; the run goes on.
    """

    kind: Literal["input_measurement_failed"] = "input_measurement_failed"
    failure: InputMeasurementFailureKind
    stage: str | None = None
    summary: str = ""
    attempts: int = Field(default=1, ge=1)
    compared: bool = False


class MeasurementComparedData(EventPayload):
    """A candidate's measurement was compared with its base's: ratio, noise and verdict.

    Emitted when core records a comparison (and again when more samples change it). ``ratio``
    is the candidate's mean over the base's, as measured, and ``direction`` says which way is
    better; ``noise`` is one standard error of that ratio, and ``verdict`` needs two of them.
    The values are means over ``candidate_samples`` and ``base_samples`` runs of one measured
    input each, and ``base_commit`` is the revision the base was measured at.
    """

    kind: Literal["measurement_compared"] = "measurement_compared"
    metric: str
    unit: str | None = None
    direction: Literal["max", "min"]
    candidate_commit: str
    base_commit: str
    candidate_value: FiniteFloat
    base_value: FiniteFloat
    ratio: FiniteFloat
    noise: FiniteFloat
    verdict: ComparisonVerdict
    candidate_samples: int
    base_samples: int
    source: FrameworkSource = FrameworkSource.GATES


class ComparisonStatus(StrEnum):
    """What a run knows of one comparison baseline. Closed; mirrored by ``server.events``."""

    MEASURED = "measured"
    FAILED = "failed"
    NOT_CONFIGURED = "not_configured"
    NOT_MEASURED = "not_measured"


class ComparisonReadingData(BaseModel):
    """One comparison baseline as the run read it from the project's store.

    ``value`` is the mean of ``samples`` runs of the task's benchmark against the engine, and
    ``spread`` their relative standard deviation (absent for one run). ``reason`` explains a
    failed or unconfigured comparison. A hidden-from-agents comparison is still shown here:
    the operator sees it; only the planner's prompt omits it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    label: str
    status: ComparisonStatus
    metric: str | None = None
    unit: str | None = None
    direction: Literal["max", "min"] | None = None
    value: FiniteFloat | None = None
    spread: FiniteFloat | None = None
    samples: int | None = None
    hosts: tuple[str, ...] = ()
    reason: str | None = None


class ComparisonsReadData(EventPayload):
    """The comparison baselines a run read when it started or resumed; the newest event wins.

    A run never measures a comparison: ``vibesys compare`` does, once, and stores the reading.
    """

    kind: Literal["comparisons_read"] = "comparisons_read"
    comparisons: tuple[ComparisonReadingData, ...]


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

    @field_validator("text")
    @classmethod
    def _redact_text(cls, value: str) -> str:
        return redact_diagnostic_text(value)


class WorkstreamFinishedData(EventPayload):
    """One projected, recorded policy round ended; its typed failure, if any.

    The strategy-neutral commit producer emits this alongside
    ``round_finished`` for each newly projected ``RoundSummary``, including
    serial policy rounds. It currently sets ``setup_failure`` and ``refunded``
    to ``False`` because its input is already a recorded round and contains no
    per-attempt setup-failure projection. The fields remain declared for a
    future producer that can publish such evidence.

    In the dynamic policy specifically, a workstream whose workspace never
    opened produces no recorded round, while profile and merge workstreams are
    projected only as iterations. Those dynamic workstreams therefore do not
    produce this event today.
    """

    kind: Literal["workstream_finished"] = "workstream_finished"
    setup_failure: bool
    refunded: bool
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
    reasoning_output_tokens: int | None = None
    total_cost_usd: float | None = None
    duration_ms: int | None = None
    span_id: SpanId | None = None
    """The agent-turn span of the run trace this turn ran as; the server names it."""


class _SpanData(EventPayload):
    """A span of the run trace, whole, as it is after the change the event reports.

    Spans come from the runtime (``vs_core`` records them where it journals each transition);
    a search's labels ride in ``span.label``. Folding the span events of a run by ``span.id``,
    in sequence order, gives the run's spans.
    """

    span: WireSpan


class SpanStartedData(_SpanData):
    """A span started (a flow task began, or a flow issued an operation)."""

    kind: Literal["span_started"] = "span_started"


class SpanUpdatedData(_SpanData):
    """A span changed: what it waits on, its attempts, its stop or its label."""

    kind: Literal["span_updated"] = "span_updated"


class SpanEndedData(_SpanData):
    """A span ended: ``span.status`` and ``span.failure`` say how."""

    kind: Literal["span_ended"] = "span_ended"


CoreEventData = Annotated[
    SteerDeliveryData
    | InvocationStartedData
    | InvocationFinishedData
    | AgentExecutionStartedData
    | AgentExecutionActivityData
    | AgentExecutionFinishedData
    | RunStartedData
    | RunStartupProgressData
    | RunFailedData
    | RunEndingData
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
    | QuotaWaitingData
    | QuotaWaitCancelledData
    | WorkspaceRootProbeData
    | ProviderSwitchedData
    | GateStartedData
    | GateFinishedData
    | WorkspaceSnapshotData
    | RunConfiguredData
    | FrameworkWarningData
    | WinnerSelectedData
    | InputMeasurementFailedData
    | MeasurementComparedData
    | ComparisonsReadData
    | WorkstreamFinishedData
    | TurnUsageData
    | SpanStartedData
    | SpanUpdatedData
    | SpanEndedData,
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
