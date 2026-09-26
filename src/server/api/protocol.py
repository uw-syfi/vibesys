"""Versioned transport-neutral contracts for frontend clients."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

from server.chat.options import ChatOptions
from server.diagnostics import Diagnostic, DiagnosticScope, exception_to_diagnostic
from server.events import RunEvent
from server.execution import ActiveAgentExecution
from server.run_lifecycle import RunStatus
from server.settings import InteractiveSetupDefaults
from vibesys.api import (
    CandidateDisposition,
    HypothesisOutcome,
    HypothesisResolution,
    JudgeVerdict,
    PerfDeltaReason,
)

PROTOCOL_VERSION = 1


class ProtocolModel(BaseModel):
    """Base model for versioned transport messages."""

    model_config = ConfigDict(extra="forbid")


class Request(ProtocolModel):
    """Common version, identity, and timestamp fields for client requests."""

    protocol_version: Literal[1] = PROTOCOL_VERSION
    request_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PauseCommand(Request):
    """Request pausing after the active agent call."""

    type: Literal["command.pause"] = "command.pause"
    mode: Literal["after_current_agent_call"] = "after_current_agent_call"


class ResumeCommand(Request):
    """Request resuming a paused run."""

    type: Literal["command.resume"] = "command.resume"


class SteerCommand(Request):
    """Send steering text to the active run."""

    type: Literal["command.steer"] = "command.steer"
    text: str = Field(min_length=1)


class StopCommand(Request):
    """Request stopping after the active agent call."""

    type: Literal["command.stop"] = "command.stop"
    mode: Literal["after_current_agent_call"] = "after_current_agent_call"


class SnapshotQuery(Request):
    """Request the current run snapshot."""

    type: Literal["query.snapshot"] = "query.snapshot"


class ChatQuery(Request):
    """Send a message to an experiment-chat thread."""

    type: Literal["query.chat"] = "query.chat"
    text: str
    # None targets the default thread, preserving pre-thread clients.
    thread_id: str | None = None


class ChatThreadCreateQuery(Request):
    """Create a new experiment-chat thread with its own agent selection.

    Omitted fields resolve to the run's configured driver, provider, and
    model. The response carries the resolved settings and thread identity.
    ``driver`` exists for completeness and stays validated when supplied, but
    which driver backs a run is a deployment detail: clients omit it so every
    thread inherits the run's.
    """

    type: Literal["query.chat_thread_create"] = "query.chat_thread_create"
    driver: Literal["agentshim", "omnigent"] | None = None
    provider: str | None = None
    model: str | None = None
    # Without a title the server derives one from the thread's first message.
    title: str | None = None


class ChatOptionsQuery(Request):
    """Request the agent selections this run's experiment chat offers."""

    type: Literal["query.chat_options"] = "query.chat_options"


class TuiDefaultsQuery(Request):
    """Request the launch-directory configuration defaults a TUI applies.

    A terminal client resolves its theme from the run's configuration. Asking
    over the control channel keeps TOML parsing in the server and saves the
    launcher an extra Python process on the boot path.
    """

    type: Literal["query.tui_defaults"] = "query.tui_defaults"


class HistoryQuery(Request):
    """Request persisted run history."""

    type: Literal["query.history"] = "query.history"


class PerformanceQuery(Request):
    """Request the run's performance history."""

    type: Literal["query.performance"] = "query.performance"


class ExperimentCursor(ProtocolModel):
    """Client's last completely applied experiment projection."""

    run_id: str
    projection_id: str
    revision: int = Field(ge=0)


class ExperimentQuery(Request):
    """Request the hypothesis-level experiment log for the attached run."""

    type: Literal["query.experiments"] = "query.experiments"
    # Omitted by legacy clients, which continue receiving a complete snapshot.
    after: ExperimentCursor | None = None


class DesignQuery(Request):
    """Request the per-round design log for the attached run.

    The design log is the operator's view of what each round changed in the
    system under optimization: the files the round touched and how each stage
    of the round concluded.
    """

    type: Literal["query.design"] = "query.design"


class DesignPatchQuery(Request):
    """Request one file's unified patch from a round's commit range.

    ``base`` and ``head`` are a round's own range exactly as ``query.design``
    published it (``DesignRound.base`` and ``DesignRound.commit``), and
    ``path`` must be one of that round's listed file changes. The server
    validates all three, so a client cannot diff arbitrary revisions or read
    paths the design log filtered out.
    """

    type: Literal["query.design_patch"] = "query.design_patch"
    base: str
    head: str
    path: str


class EventsQuery(Request):
    """Request run events in a sequence interval."""

    type: Literal["query.events"] = "query.events"
    after_sequence: int = Field(default=0, ge=0)
    # Exclusive upper bound: the result is ``after_sequence < sequence <
    # before_sequence``. None keeps the open-ended read. This is the backfill
    # query for history older than a tail subscription's floor.
    before_sequence: int | None = Field(default=None, ge=1)
    timeout_ms: int = Field(default=0, ge=0, le=30_000)


class SubscribeRequest(Request):
    """Subscribe to run events, optionally replaying a recent tail."""

    type: Literal["subscribe"] = "subscribe"
    after_sequence: int = Field(default=0, ge=0)
    # Replay from ``max(after_sequence, latest_sequence - tail)`` instead of
    # ``after_sequence``. An old server forbids the field, so the rejection is
    # the capability probe.
    tail: int | None = Field(default=None, ge=1)
    # The store the client's ``after_sequence`` numbers, as the last batch named
    # it. A resume across a dropped connection carries it so the server can tell
    # whether that cursor still belongs to the live store: if a durable log was
    # attached while the client was gone, the cursor numbers a store that is
    # gone, so the server drops it and bootstraps the live store instead of
    # extending a fold with another log's sequences. Empty (the default, and a
    # fresh dial that has seen no store) means resume the cursor as before, and
    # an old server forbids the field, so the client falls back to that.
    store_id: str = ""


ProtocolRequest = Annotated[
    PauseCommand
    | ResumeCommand
    | SteerCommand
    | StopCommand
    | SnapshotQuery
    | ChatQuery
    | ChatThreadCreateQuery
    | ChatOptionsQuery
    | TuiDefaultsQuery
    | HistoryQuery
    | PerformanceQuery
    | ExperimentQuery
    | DesignQuery
    | DesignPatchQuery
    | EventsQuery
    | SubscribeRequest,
    Field(discriminator="type"),
]


class ChatThreadInfo(ProtocolModel):
    """Resolved identity and agent settings of one experiment-chat thread."""

    thread_id: str
    title: str = ""
    driver: str
    provider: str
    model: str


class RunSnapshot(ProtocolModel):
    """Current server projection of a run's public state."""

    protocol_version: Literal[1] = PROTOCOL_VERSION
    run_id: str
    sequence: int
    status: RunStatus
    agent_kind: str | None = None
    round_label: str | None = None
    active_executions: list[ActiveAgentExecution] = Field(default_factory=list)
    # Server-owned projection: a thread's title is backfilled from a later
    # CHAT event, so a client that folds only a tail cannot rebuild the
    # registry from the events it holds.
    chat_threads: list[ChatThreadInfo] = Field(default_factory=list)


class CommandAck(ProtocolModel):
    """Acknowledgment of a requested run command."""

    action: Literal["pause", "resume", "steer", "stop"]
    status: Literal["pending", "consumed"]


class ChatResult(ProtocolModel):
    """Answer and thread identity returned by experiment chat."""

    question: str
    answer: str
    effect: Literal["none"] = "none"
    # Echoes the requested thread; None is the default thread.
    thread_id: str | None = None


class PerformanceRound(ProtocolModel):
    """One measured performance result in a run."""

    round: int
    perf_metric: FiniteFloat
    perf_unit: str
    passed: bool
    profile_skipped: bool = False


class PerformanceContext(ProtocolModel):
    """What the performance plot measures and how to read it.

    Copied from recorded run state and the run manifest, never recomputed.
    Every field is optional so the section can describe the objective before
    the first measurement and omit facts a run never recorded; a run whose
    prose is known before its metric still gets a description-only context.
    """

    objective_metric: str | None = None
    objective_unit: str | None = None
    objective_direction: Literal["max", "min"] | None = None
    objective_baseline_value: FiniteFloat | None = None
    objective_baseline_round: int | None = None
    objective_baseline_commit: str | None = None
    objective_description: str | None = None


class HypothesisRound(ProtocolModel):
    """One round belonging to a hypothesis, for the experiment-log drill-down.

    This is the single source for every per-round fact the server publishes.
    Surfaces that need more about a round (the design log's file list, for
    example) join to this row by ``round`` rather than restating its fields.

    ``hypothesis_outcome`` and ``candidate_disposition`` are closed sets, so
    the generated client union is closed too. A round record written before a
    member existed, or carrying a value the framework no longer defines, is
    projected as ``None``: unreadable and unrecorded are the same thing to a
    client, and a stale string must not take down the whole log.
    """

    round: int
    passed: bool
    reviewed: bool
    # Either vocabulary can reach a round record: the implementer declares an
    # outcome, and the framework may overwrite it with its own resolution.
    hypothesis_outcome: HypothesisOutcome | HypothesisResolution | None = None
    # The round's own review state, decided by its final implementer attempt.
    # ``deferred`` means sparse-review policy skipped the judge; None marks a
    # legacy record written before the framework stored a verdict. This is
    # per-round and distinct from ``HypothesisEntry.judge_verdict``, which is
    # the hypothesis-level review.
    judge_verdict: JudgeVerdict | None = None
    perf_metric: FiniteFloat | None = None
    perf_unit: str | None = None
    # Causal delta the round recorded against its own baseline. None when the
    # round made no comparable measurement.
    perf_delta_pct: FiniteFloat | None = None
    commit: str | None = None
    official_evaluation: bool = False
    candidate_disposition: CandidateDisposition | None = None


class HypothesisEntry(ProtocolModel):
    """One unit of investigation: a hypothesis and every round it spans.

    ``resolved_outcome`` is copied from the server's typed hypothesis state,
    never recomputed by the server or client.
    """

    hypothesis_id: str
    # False when the underlying records carry no ``hypothesis_id``, e.g. a log
    # directory written before hypothesis tracking. The row is still returned
    # so history stays complete; clients render it as an explicit placeholder.
    identified: bool = True
    # Server-derived display title: the orchestrator's own title, or a
    # fallback derived from ``claim`` when the orchestrator gave none. None
    # when there is no text to title at all.
    title: str | None = None
    claim: str | None = None
    action: str | None = None
    first_round: int
    last_round: int
    rounds: list[HypothesisRound] = Field(default_factory=list)
    resolved_outcome: str | None = None
    # Independent review from the authoritative hypothesis state.
    judge_verdict: Literal["pass", "fail"] | None = None
    perf_metric: FiniteFloat | None = None
    perf_unit: str | None = None
    # Causal delta paired with ``perf_metric`` by the hypothesis state. None
    # means no official comparison is available.
    perf_delta_pct: FiniteFloat | None = None
    # Identity of the measured metric, so clients can label the bare number.
    # Legacy rounds recorded only a unit, so this may repeat ``perf_unit``.
    perf_metric_name: str | None = None
    # Which way improvement points for the metric. None when the run recorded
    # no objective direction; clients must not guess one.
    perf_direction: Literal["max", "min"] | None = None
    # The other side of ``perf_delta_pct``, from the same official
    # measurement, so the comparison stays interpretable in absolute terms.
    perf_baseline_value: FiniteFloat | None = None
    # Causal identity of that baseline, so the comparison is auditable from
    # the client: which round it was, and which workspace commit it measured.
    perf_baseline_round: int | None = None
    perf_baseline_commit: str | None = None
    # Why ``perf_delta_pct`` is absent, when the server can say. None when a
    # delta is present, when no official metric was recorded at all, or for
    # records predating provenance tracking, which keep reading as deliberate
    # absolute measurements.
    perf_delta_reason: PerfDeltaReason | None = None
    # Integration, not truth: the framework's explicit retention decision.
    # None means legacy or not yet assessed, never "official evaluation ran".
    kept: bool | None = None
    # Orchestrator strategy is separate from empirical resolution and
    # candidate retention. It is structured server state, not roadmap prose.
    strategy_disposition: Literal["available", "parked", "abandoned"] | None = None
    strategy_reason: str | None = None
    active: bool = False


class ExperimentUpdate(ProtocolModel):
    """How to apply ``Response.experiments`` to a client's prior snapshot.

    A reset replaces the entire list. A delta replaces entries by stable
    hypothesis ID and then removes the named IDs. ``from_revision`` is None for
    a reset because no prior client state is trusted.
    """

    run_id: str
    projection_id: str
    from_revision: int | None = Field(default=None, ge=0)
    through_revision: int = Field(ge=0)
    reset: bool
    removed_hypothesis_ids: list[str] = Field(default_factory=list)


class DesignFileChange(ProtocolModel):
    """One workspace file a round's commit range touched."""

    path: str
    change: Literal["added", "modified", "deleted", "renamed"]
    # The pre-rename path, present exactly when ``change`` is "renamed".
    renamed_from: str | None = None


class DesignRound(ProtocolModel):
    """What one round changed in the workspace.

    Deliberately narrow: every other per-round fact (outcome, review,
    official evaluation, candidate disposition, measurement) already crosses
    the protocol on ``HypothesisRound``, and a client joins the two by
    ``round``. Publishing a second copy here let the two fetches disagree
    about the same round.

    ``files`` is derived from the run workspace's git history. None means the
    round's commit range could not be resolved (no checkpoint recorded, or the
    workspace history no longer has it), which is distinct from an empty list,
    a resolved range that touched nothing outside framework bookkeeping.
    """

    round: int
    # The round's end-of-round checkpoint, and the head of the diffed range.
    commit: str | None = None
    # The other end of that range, as the projection derived it. Publishing it
    # lets a client ask ``query.design_patch`` for exactly the range ``files``
    # describes instead of re-deriving one. None when no base resolved, in
    # which case ``files`` is None too.
    base: str | None = None
    files: list[DesignFileChange] | None = None


class DesignPatch(ProtocolModel):
    """One file's unified patch text from a round's commit range.

    ``patch`` is the raw ``git diff`` output for the one file (rename
    detection on, so a renamed file arrives as a single patch spanning both
    paths). None means the workspace repository could not produce the text
    (repository missing or unreadable), which is distinct from an empty
    string, a file the range lists but whose content did not change.

    ``truncated`` marks a patch cut at the server's size bound. The echoed
    range and paths let a client show the exact ``git diff`` command that
    reproduces the full output externally.
    """

    base: str
    head: str
    path: str
    # The pre-rename path, echoed from the round's file list when the change
    # is a rename, so the external-command hint can name both sides.
    renamed_from: str | None = None
    patch: str | None = None
    truncated: bool = False


class Response(ProtocolModel):
    """Response envelope for all protocol requests."""

    protocol_version: Literal[1] = PROTOCOL_VERSION
    request_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    ok: bool = True
    error: str | None = None
    diagnostic: Diagnostic | None = None
    ack: CommandAck | None = None
    chat: ChatResult | None = None
    chat_thread: ChatThreadInfo | None = None
    # None means the run has not attached its agent selection yet, which is
    # distinct from a run that offers no provider at all.
    chat_options: ChatOptions | None = None
    # None means this server was started without a defaults provider, so the
    # client keeps its own built-in defaults. It never means "no defaults".
    tui_defaults: InteractiveSetupDefaults | None = None
    snapshot: RunSnapshot | None = None
    events: list[RunEvent] = Field(default_factory=list)
    performance: list[PerformanceRound] = Field(default_factory=list)
    # None means the run has not recorded what its metric is, which is
    # distinct from a run whose plot is merely empty so far.
    performance_context: PerformanceContext | None = None
    experiments: list[HypothesisEntry] = Field(default_factory=list)
    # Present on revision-aware servers. The experiments list is complete when
    # reset is true and contains only replacements when reset is false.
    experiment_update: ExperimentUpdate | None = None
    # False means canonical project/run state is not attached yet. Keeping the
    # readiness marker separate preserves the protocol-v1 list contract while
    # distinguishing bootstrap from an authoritative empty experiment log.
    experiments_ready: bool | None = None
    design: list[DesignRound] = Field(default_factory=list)
    # Same bootstrap-versus-empty distinction as ``experiments_ready``.
    design_ready: bool | None = None
    # None means the run has not attached yet, so no patch can be produced.
    design_patch: DesignPatch | None = None

    @classmethod
    def from_exception(
        cls,
        request_id: str,
        error: BaseException,
        *,
        operation: str = "Request",
        scope: DiagnosticScope = DiagnosticScope.REQUEST,
        code: str | None = None,
    ) -> Response:
        """Build a failed response with consistent legacy and typed errors."""
        diagnostic = exception_to_diagnostic(error, scope=scope, operation=operation, code=code)
        return cls(request_id=request_id, ok=False, error=diagnostic.summary, diagnostic=diagnostic)


class SubscribedMessage(ProtocolModel):
    """Initial acknowledgment for an event subscription."""

    type: Literal["subscribed"] = "subscribed"
    request_id: str
    run_id: str
    latest_sequence: int


class EventMessage(ProtocolModel):
    """Single-event message for the legacy streaming protocol."""

    type: Literal["event"] = "event"
    event: RunEvent


class EventBatchMessage(ProtocolModel):
    """Event batch and cursor metadata sent to subscribers."""

    type: Literal["event_batch"] = "event_batch"
    events: list[RunEvent]
    through_sequence: int = Field(default=0, ge=0)
    active_executions: list[ActiveAgentExecution] = Field(default_factory=list)
    # Names the event store these sequences number. A run attaches its durable
    # log after clients subscribe, and sequences are only comparable within one
    # store, so a batch whose id differs from the previous one supersedes what
    # the client folded rather than extending it. Empty means the server does
    # not report store identity, leaving the client on watermark comparison
    # alone, which is what it had before this field existed.
    store_id: str = ""
    # "Every event in this stream's history has sequence > this." 0 means the
    # full history was delivered, which is the default and today's behavior.
    # Carried on every batch of the subscription, live ones included.
    history_after_sequence: int = Field(default=0, ge=0)


class ProtocolErrorMessage(ProtocolModel):
    """Structured error envelope for protocol failures."""

    type: Literal["protocol_error"] = "protocol_error"
    request_id: str | None = None
    code: str
    message: str
    diagnostic: Diagnostic | None = None

    @classmethod
    def from_exception(
        cls,
        error: BaseException,
        *,
        request_id: str | None = None,
        operation: str = "Protocol operation",
        scope: DiagnosticScope = DiagnosticScope.PROTOCOL,
        code: str | None = None,
    ) -> ProtocolErrorMessage:
        """Build a protocol error with consistent legacy and typed errors."""
        diagnostic = exception_to_diagnostic(error, scope=scope, operation=operation, code=code)
        return cls(
            request_id=request_id,
            code=diagnostic.code,
            message=diagnostic.summary,
            diagnostic=diagnostic,
        )


ServerMessage = Annotated[
    SubscribedMessage | EventMessage | EventBatchMessage | ProtocolErrorMessage,
    Field(discriminator="type"),
]
