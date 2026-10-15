"""Versioned transport-neutral contracts for frontend clients."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from typing import TYPE_CHECKING, Annotated, Literal, Never, TypeAliasType

from pydantic import ConfigDict, Field, FiniteFloat, JsonValue, TypeAdapter, model_validator

from server.api.iterations import MAX_ITERATION_PAGE_SIZE, IterationSlice
from server.chat.options import ChatOptions
from server.control_envelope import (
    ProtocolModel,
    Request,
    ResponseEnvelope,
    StopCommand,
)
from server.diagnostics import Diagnostic, DiagnosticScope, exception_to_diagnostic
from server.events import ChatThreadId, RunEvent
from server.execution import ActiveAgentExecution
from server.host_api import PROTOCOL_VERSION
from server.run_lifecycle import RunStatus
from server.settings import InteractiveSetupDefaults
from vibesys.api import IterationUnavailable
from vibesys.api.hypothesis import (
    CandidateDisposition,
    HypothesisOutcome,
    HypothesisResolution,
    JudgeVerdict,
    PerfDeltaReason,
)
from vs_decode.api import Unreadable, UnreadableKind, decode_json
from vs_sim.api import utc_datetime

if TYPE_CHECKING:
    from collections.abc import Iterator
    from datetime import datetime

    from pydantic.json_schema import JsonSchemaValue

    from vs_sim.api import Clock

# Both snapshots and streamed batches publish the same execution checkpoint.
# Naming the list keeps the generated client contract referentially identical,
# rather than making the generator invent one alias per inline occurrence.
ActiveExecutions = TypeAliasType("ActiveExecutions", list[ActiveAgentExecution])

# The smallest receive cap among supported presentation clients. Both server
# transports use it for stream output, so transport choice cannot change which
# protocol checkpoint is deliverable.
MAX_SERVER_MESSAGE_BYTES = 1024 * 1024
# One payload fragment stays comfortably below the smallest supported peer's
# 1 MiB frame cap even when every code point needs JSON's longest escape. The
# aggregate bound prevents a peer from making a frontend stage an unreasonable
# amount of memory. It matches the largest request frame the server accepts, so
# one assignment that entered through the public protocol remains observable.
MAX_ACTIVE_EXECUTIONS_CHUNK_CHARACTERS = 64 * 1024
MAX_ACTIVE_EXECUTIONS_CHECKPOINT_BYTES = 16 * 1024 * 1024
MAX_ACTIVE_EXECUTIONS_CHUNKS = 256

_ACTIVE_EXECUTIONS_ADAPTER = TypeAdapter(ActiveExecutions)
_JSON_CONTROL_CHARACTER_LIMIT = 0x20
_JSON_SINGLE_BYTE_LIMIT = 0x80
_JSON_TWO_BYTE_LIMIT = 0x800
_JSON_THREE_BYTE_LIMIT = 0x10000
_UTF16_HIGH_SURROGATE_START = 0xD800
_UTF16_LOW_SURROGATE_END = 0xDFFF
_JSON_SHORT_CONTROL_ESCAPES = frozenset({0x08, 0x09, 0x0A, 0x0C, 0x0D})


def _event_batch_json_schema(schema: JsonSchemaValue) -> None:
    """Publish the two legal checkpoint shapes as complete generated types."""
    ordinary = deepcopy(schema)
    deferred = deepcopy(schema)
    ordinary_properties = ordinary["properties"]
    deferred_properties = deferred["properties"]

    active_schema = schema["properties"]["active_executions"]
    checkpoint_id_schema = schema["properties"]["active_executions_checkpoint_id"]
    ordinary_properties["active_executions"] = deepcopy(active_schema["anyOf"][0])
    ordinary_properties["active_executions_checkpoint_id"] = {
        "default": None,
        "title": checkpoint_id_schema["title"],
        "type": "null",
    }
    deferred_properties["active_executions"] = {"type": "null"}
    deferred_properties["active_executions_checkpoint_id"] = deepcopy(
        checkpoint_id_schema["anyOf"][0]
    )
    ordinary["required"] = [*schema["required"], "type"]
    deferred["required"] = [
        *schema["required"],
        "type",
        "active_executions",
        "active_executions_checkpoint_id",
    ]

    # Keep the broad properties at the definition root for the repository's
    # tagged-union normalization, while the complete oneOf arms narrow every
    # validator and generated TypeScript consumer to the two legal shapes.
    schema["oneOf"] = [ordinary, deferred]


class PauseCommand(Request):
    """Request pausing after the active agent call."""

    type: Literal["command.pause"] = "command.pause"
    mode: Literal["after_current_agent_call"] = "after_current_agent_call"


class ResumeCommand(Request):
    """Request resuming a paused run."""

    type: Literal["command.resume"] = "command.resume"
    # A run paused on a provider capacity limit may switch to its configured
    # fallback provider as it resumes. Ignored when the run has no fallback.
    fallback: bool = False


class SteerCommand(Request):
    """Send steering text to the active run."""

    type: Literal["command.steer"] = "command.steer"
    text: str = Field(min_length=1)


class SnapshotQuery(Request):
    """Request the current run snapshot.

    ``expected_store_id`` fences the read to the event store whose batch established a
    client's sequence space. None preserves the unfenced request used by
    clients that do not combine a snapshot with a live subscription. An empty
    string is a real fence for the pre-attachment store, not an alias for None.
    """

    type: Literal["query.snapshot"] = "query.snapshot"
    expected_store_id: str | None = None


class ChatQuery(Request):
    """Send a message to an experiment-chat thread."""

    type: Literal["query.chat"] = "query.chat"
    text: str
    # None targets the default thread, preserving pre-thread clients.
    thread_id: ChatThreadId | None = None


class ChatThreadCreateQuery(Request):
    """Create a new experiment-chat thread with its own agent selection.

    Omitted fields resolve to the run's configured provider and model. The
    response carries the resolved settings and thread identity.
    """

    type: Literal["query.chat_thread_create"] = "query.chat_thread_create"
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


class IterationQuery(Request):
    """Request one bounded page of roots or direct children.

    ``expected_store_id`` fences every page to the event store whose stream
    established the navigation query. None preserves the unfenced request for
    clients that do not combine iteration navigation with a live subscription.
    An empty string is a real fence for the pre-attachment store.
    """

    type: Literal["query.iterations"] = "query.iterations"
    expected_store_id: str | None = None
    parent_id: str | None = None
    cursor: str | None = None
    limit: int = Field(default=100, ge=1, le=MAX_ITERATION_PAGE_SIZE)


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
    """Request run events in a sequence interval.

    ``expected_store_id`` fences the interval to the event store whose stream
    established the caller's history floor. None preserves the unfenced query
    for callers that do not combine history with a live subscription. An empty
    string is a real fence for the pre-attachment store.
    """

    type: Literal["query.events"] = "query.events"
    expected_store_id: str | None = None
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
    | IterationQuery
    | DesignQuery
    | DesignPatchQuery
    | EventsQuery
    | SubscribeRequest,
    Field(discriminator="type"),
]


class ChatThreadInfo(ProtocolModel):
    """Resolved identity and agent settings of one experiment-chat thread."""

    thread_id: ChatThreadId
    title: str = ""
    provider: str
    model: str


class RunSnapshot(ProtocolModel):
    """Current server projection of a run's public state."""

    protocol_version: Literal[1] = PROTOCOL_VERSION
    run_id: str
    # Opaque machine-local namespace for client state keyed by project and run.
    # None keeps snapshots from older/read-only composition roots compatible.
    project_scope: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    sequence: int
    status: RunStatus
    agent_kind: str | None = None
    round_label: str | None = None
    active_executions: ActiveExecutions = Field(default_factory=list)
    # Server-owned projection: a thread's title is backfilled from a later
    # CHAT event, so a client that folds only a tail cannot rebuild the
    # registry from the events it holds.
    chat_threads: list[ChatThreadInfo] = Field(default_factory=list)


class ChatResult(ProtocolModel):
    """Answer and thread identity returned by experiment chat."""

    question: str
    answer: str
    effect: Literal["none"] = "none"
    # Echoes the requested thread; None is the default thread.
    thread_id: ChatThreadId | None = None


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


class Response(ResponseEnvelope):
    """Response envelope for all protocol requests."""

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
    iterations: IterationSlice | IterationUnavailable | None = None
    iterations_ready: bool | None = None
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
        timestamp: datetime,
        operation: str = "Request",
    ) -> Response:
        """Build a failed response with consistent legacy and typed errors."""
        diagnostic = exception_to_diagnostic(
            error,
            scope=DiagnosticScope.REQUEST,
            operation=operation,
            code=getattr(error, "diagnostic_code", None),
        )
        return cls(
            request_id=request_id,
            timestamp=timestamp,
            ok=False,
            error=diagnostic.summary,
            diagnostic=diagnostic,
        )


_REQUEST_ADAPTER = TypeAdapter(ProtocolRequest)
MAX_REQUEST_FRAME_BYTES = 16 * 1024 * 1024
"""The largest request frame any transport hands to ``decode_request``."""
_JSON_VALUE: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


def decode_request(frame: str | bytes, *, clock: Clock) -> ProtocolRequest | Response:
    """Parse one request frame, or the failed response that rejects it.

    Every transport decodes through here, so a malformed frame (invalid JSON or
    UTF-8, a JSON value that is not an object, a missing or unknown ``type``,
    unknown keys, nesting deeper than the JSON parser's depth limit) draws the
    same typed response on each of them, stamped by ``clock``. The rejection echoes the frame's
    ``request_id`` and ``client_id`` when it carries them as an object, so a
    client can match it to what it sent.

    Total: no frame makes this raise. A frame is parsed only by ``vs_decode``: at most
    ``MAX_REQUEST_FRAME_BYTES``, with nesting bounded by pydantic's JSON parser, where
    Python's ``json`` recursed per level and raised ``RecursionError``, which escaped to
    the transport and ended the client's session. A frame that fails validation names
    its failing paths in the diagnostic's ``validation_paths``.
    """
    request = decode_json(
        frame, _REQUEST_ADAPTER, source="request frame", max_bytes=MAX_REQUEST_FRAME_BYTES
    )
    if not isinstance(request, Unreadable):
        return request
    request_id, client_id = _frame_ids(frame)
    response = failure_response(
        request_id, client_id, RequestFrameError(request.details("frame")), clock=clock
    )
    if request.kind is not UnreadableKind.INVALID or response.diagnostic is None:
        return response
    paths = tuple(problem.location for problem in request.problems)
    diagnostic = response.diagnostic.model_copy(update={"validation_paths": paths})
    return response.model_copy(update={"diagnostic": diagnostic})


class RequestFrameError(ValueError):
    """A request frame that is not a valid request; the message names each failing path."""


def failure_response(
    request_id: str, client_id: str, error: BaseException, *, clock: Clock
) -> Response:
    """The failed response for ``error``, stamped by ``clock`` and addressed to ``client_id``."""
    return Response.from_exception(
        request_id, error, timestamp=utc_datetime(clock), operation="Request"
    ).model_copy(update={"client_id": client_id})


def _frame_ids(frame: str | bytes) -> tuple[str, str]:
    value = decode_json(
        frame, _JSON_VALUE, source="request frame", max_bytes=MAX_REQUEST_FRAME_BYTES
    )
    if isinstance(value, Unreadable):
        return "unknown", ""
    if not isinstance(value, dict):
        return "unknown", ""
    client_id = value.get("client_id")
    return str(value.get("request_id", "unknown")), client_id if isinstance(client_id, str) else ""


class SubscribedMessage(ProtocolModel):
    """Initial acknowledgment for an event subscription."""

    type: Literal["subscribed"] = "subscribed"
    request_id: str
    client_id: str = ""
    run_id: str
    latest_sequence: int


class EventMessage(ProtocolModel):
    """Single-event message for the legacy streaming protocol."""

    type: Literal["event"] = "event"
    event: RunEvent


class ActiveExecutionsChunkMessage(ProtocolModel):
    """One bounded piece of an atomically replaced execution checkpoint.

    The payload pieces concatenate to the JSON encoding of ``ActiveExecutions``.
    A client stages one transfer and publishes the decoded list only after all
    pieces arrive in order. The metadata is copied from the batch whose
    checkpoint this replaces, so the final publication remains in the same
    sequence space even when the batch itself was split into event frames.
    """

    type: Literal["active_executions_chunk"] = "active_executions_chunk"
    checkpoint_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    chunk_index: int = Field(ge=0, lt=MAX_ACTIVE_EXECUTIONS_CHUNKS)
    chunk_count: int = Field(ge=1, le=MAX_ACTIVE_EXECUTIONS_CHUNKS)
    payload: str = Field(max_length=MAX_ACTIVE_EXECUTIONS_CHUNK_CHARACTERS)
    through_sequence: int = Field(ge=0)
    store_id: str
    history_after_sequence: int = Field(ge=0)


class EventBatchMessage(ProtocolModel):
    """Event batch and cursor metadata sent to subscribers."""

    # The runtime validator below owns construction through Pydantic. The same
    # invariant must also be visible to JSON Schema validators and generated
    # TypeScript consumers, so publish the two legal wire shapes explicitly.
    model_config = ConfigDict(json_schema_extra=_event_batch_json_schema)

    type: Literal["event_batch"] = "event_batch"
    events: list[RunEvent]
    through_sequence: int = Field(default=0, ge=0)
    # ``None`` appears only on event frames whose oversized checkpoint follows
    # as ``active_executions_chunk`` messages. A list, including an empty one,
    # remains an authoritative whole replacement.
    active_executions: ActiveExecutions | None = Field(default_factory=list)
    # Binds a deferred batch to the fragment transfer that completes it. Null
    # for every ordinary authoritative checkpoint.
    active_executions_checkpoint_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
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
    # True only when this already-open subscription has been bootstrapped again
    # after its first batch. A newly dialed subscription, including a reconnect
    # that discovers a different store, leaves this false: its first batch is
    # distinguished by the dial rather than by an in-loop replay transition.
    rebootstrap: bool = False

    @model_validator(mode="after")
    def _deferred_checkpoint_has_identity(self) -> EventBatchMessage:
        deferred = self.active_executions is None
        identified = self.active_executions_checkpoint_id is not None
        if deferred != identified:
            message = (
                "active_executions_checkpoint_id is present exactly when active_executions is null"
            )
            raise ValueError(message)
        return self

    def split_within(self, budget: int) -> Iterator[EventBatchMessage]:
        """Split this batch into whole batches that each serialize within *budget* bytes.

        Folding the chunks in order has the same effect as folding this batch.
        Each chunk is a complete ``event_batch`` rather than a fragment, so a
        transport that bounds its frames needs no reassembly step and
        ``WP-GRANULARITY`` still holds: one frame is one protocol message. The
        per-field rules that make that true are:

        - ``through_sequence`` on each chunk is the sequence of the last event it
          carries, so a consumer advances its cursor exactly as far as the events
          it has seen, and a connection lost mid-replay resumes from a true
          cursor. The final chunk keeps the batch's own ``through_sequence``,
          which may run past its last event when the journal's watermark
          advanced over events the checkpoint did not return; carrying it on an
          earlier chunk would claim delivery of events still to come.
        - ``rebootstrap`` is carried by the first chunk only. It means "this
          supersedes the fold" (``WP-REBOOTSTRAP``), so on a later chunk it
          would tell the consumer to discard the earlier chunks of the same
          replay.

        Chunking is on serialized size, not event count. If one event or the
        repeated authoritative checkpoint cannot fit, the method fails before
        yielding anything; callers that need bounded checkpoint fragmentation
        use :meth:`stream_messages_within` instead.
        """
        active_size = (
            None
            if self.active_executions is None
            else _active_executions_json_size(self.active_executions)
        )
        batches = _bounded_event_batches(
            self,
            budget=budget,
            active_executions_size=active_size,
        )
        if batches is None:
            _raise_event_batch_budget_error(budget)
        yield from batches

    def stream_messages_within(
        self, budget: int
    ) -> Iterator[EventBatchMessage | ActiveExecutionsChunkMessage]:
        """Yield bounded stream messages without exposing a partial checkpoint.

        Ordinary checkpoints keep the established event-batch representation.
        When the checkpoint cannot share a bounded batch with every whole
        event, event chunks carry ``active_executions: null`` and a typed
        fragment sequence follows. The transport-neutral client stream stages
        those fragments and emits one complete synthetic checkpoint batch; raw
        prefixes are never data-fold input.
        """
        active = self.active_executions
        if active is None:
            batches = _bounded_event_batches(self, budget=budget)
            if batches is None:
                _raise_event_batch_budget_error(budget)
            yield from batches
            return

        # Reject a frame budget that cannot carry even the smallest ordinary
        # checkpoint before walking, sizing, or encoding caller-controlled
        # checkpoint text.
        minimum_envelope = self.model_copy(update={"active_executions": [], "events": []})
        if len(minimum_envelope.model_dump_json().encode()) > budget:
            _raise_event_batch_budget_error(budget)

        checkpoint_size = _active_executions_json_size(active)
        event_sizes = [len(event.model_dump_json().encode()) for event in self.events]
        ordinary_batches = _bounded_event_batches(
            self,
            budget=budget,
            active_executions_size=checkpoint_size,
            event_sizes=event_sizes,
        )
        if ordinary_batches is not None:
            _encode_active_executions(active, expected_size=checkpoint_size)
            yield from ordinary_batches
            return

        # Validate every other frame before materializing the authoritative
        # checkpoint. An impossible budget therefore cannot make the encoder
        # allocate a large payload and cannot advance the peer's cursor.
        checkpoint_id_placeholder = "0" * 64
        without_checkpoint = self.model_copy(
            update={
                "active_executions": None,
                "active_executions_checkpoint_id": checkpoint_id_placeholder,
            }
        )
        deferred_batches = _bounded_event_batches(
            without_checkpoint,
            budget=budget,
            event_sizes=event_sizes,
        )
        if deferred_batches is None:
            _raise_event_batch_budget_error(budget)
        template = ActiveExecutionsChunkMessage(
            checkpoint_id=checkpoint_id_placeholder,
            chunk_index=MAX_ACTIVE_EXECUTIONS_CHUNKS - 1,
            chunk_count=MAX_ACTIVE_EXECUTIONS_CHUNKS,
            payload="",
            through_sequence=self.through_sequence,
            store_id=self.store_id,
            history_after_sequence=self.history_after_sequence,
        )
        if len(template.model_dump_json().encode()) > budget:
            msg = f"frame budget {budget} cannot hold an active-execution checkpoint fragment"
            raise ValueError(msg)

        payload_bytes = _encode_active_executions(active, expected_size=checkpoint_size)
        checkpoint_id = hashlib.sha256(payload_bytes).hexdigest()
        payload = payload_bytes.decode()
        del payload_bytes
        template = template.model_copy(update={"checkpoint_id": checkpoint_id})
        pieces = _checkpoint_pieces(payload, budget=budget, template=template)
        del payload

        # Validate the complete fragment projection before yielding
        # any event frame. A locally undeliverable checkpoint therefore becomes
        # one typed stream error without first advancing the peer's cursor.
        yield from (
            batch.model_copy(update={"active_executions_checkpoint_id": checkpoint_id})
            for batch in deferred_batches
        )
        count = len(pieces)
        for index, piece in enumerate(pieces):
            yield template.model_copy(
                update={"chunk_index": index, "chunk_count": count, "payload": piece}
            )


def _raise_event_batch_budget_error(budget: int) -> Never:
    msg = f"frame budget {budget} cannot hold an event_batch frame"
    raise ValueError(msg)


def _raise_checkpoint_size_error() -> Never:
    msg = (
        "active-execution checkpoint exceeds the "
        f"{MAX_ACTIVE_EXECUTIONS_CHECKPOINT_BYTES}-byte protocol limit"
    )
    raise ValueError(msg)


def _encode_active_executions(active: ActiveExecutions, *, expected_size: int) -> bytes:
    """Encode the authoritative checkpoint once after bounded preflight."""
    payload_bytes = _ACTIVE_EXECUTIONS_ADAPTER.dump_json(active)
    if len(payload_bytes) > MAX_ACTIVE_EXECUTIONS_CHECKPOINT_BYTES:
        # The structural preflight accounts exactly for the current typed
        # contract. Retain this boundary check so a future field cannot weaken
        # the aggregate limit before its sizing rule is added.
        _raise_checkpoint_size_error()
    if len(payload_bytes) != expected_size:
        msg = "active-execution checkpoint sizing disagrees with its authoritative JSON encoding"
        raise ValueError(msg)
    return payload_bytes


def _bounded_event_batches(
    batch: EventBatchMessage,
    *,
    budget: int,
    active_executions_size: int | None = None,
    event_sizes: list[int] | None = None,
) -> list[EventBatchMessage] | None:
    """Return a complete bounded batch projection, or ``None`` if impossible."""
    sizes = event_sizes
    if sizes is None:
        sizes = [len(event.model_dump_json().encode()) for event in batch.events]
    maximum_sequence = max(
        (event.sequence for event in batch.events),
        default=batch.through_sequence,
    )
    maximum_sequence = max(maximum_sequence, batch.through_sequence)
    flags = (batch.rebootstrap,) if not batch.events else (False, True)
    envelope_size = max(
        _event_batch_envelope_size(
            batch,
            rebootstrap=flag,
            through_sequence=maximum_sequence,
            active_executions_size=active_executions_size,
        )
        for flag in flags
    )
    if not batch.events:
        return [batch] if envelope_size <= budget else None

    room = budget - envelope_size
    if any(size > room for size in sizes):
        return None
    chunks: list[EventBatchMessage] = []
    start = 0
    used = 0
    for index, size in enumerate(sizes):
        if index > start and used + 1 + size > room:
            chunks.append(
                batch.model_copy(
                    update={
                        "events": batch.events[start:index],
                        "through_sequence": batch.events[index - 1].sequence,
                        "rebootstrap": batch.rebootstrap and start == 0,
                    }
                )
            )
            start, used = index, 0
        used += size + (1 if index > start else 0)
    chunks.append(
        batch.model_copy(
            update={
                "events": batch.events[start:],
                "rebootstrap": batch.rebootstrap and start == 0,
            }
        )
    )
    return chunks


def _event_batch_envelope_size(
    batch: EventBatchMessage,
    *,
    rebootstrap: bool,
    through_sequence: int,
    active_executions_size: int | None,
) -> int:
    envelope = batch.model_copy(
        update={
            "events": [],
            "rebootstrap": rebootstrap,
            "through_sequence": through_sequence,
        }
    )
    if active_executions_size is None:
        return len(envelope.model_dump_json().encode())
    empty_checkpoint = envelope.model_copy(update={"active_executions": []})
    return len(empty_checkpoint.model_dump_json().encode()) - len(b"[]") + active_executions_size


def _active_executions_json_size(active: ActiveExecutions) -> int:
    """Measure canonical checkpoint JSON without allocating its large strings."""
    size = len(b"[]")
    for index, execution in enumerate(active):
        if index:
            size += len(b",")
        blank_activity = execution.activity.model_copy(
            update={
                "summary": "",
                "tool": "" if execution.activity.tool is not None else None,
            }
        )
        blank_execution = execution.model_copy(
            update={
                "execution_id": "",
                "agent_kind": "",
                "round_label": "",
                "stage": "",
                "assignment": "",
                "activity": blank_activity,
                "provider": "" if execution.provider is not None else None,
                "model": "" if execution.model is not None else None,
            }
        )
        size += len(blank_execution.model_dump_json().encode())
        if size > MAX_ACTIVE_EXECUTIONS_CHECKPOINT_BYTES:
            _raise_checkpoint_size_error()
        strings = (
            execution.execution_id,
            execution.agent_kind,
            execution.round_label,
            execution.stage,
            execution.assignment,
            execution.activity.summary,
            execution.activity.tool,
            execution.provider,
            execution.model,
        )
        for value in strings:
            if value is None:
                continue
            remaining = MAX_ACTIVE_EXECUTIONS_CHECKPOINT_BYTES - size
            if len(value) > remaining:
                _raise_checkpoint_size_error()
            size += _json_string_content_size(value, limit=remaining)
            if size > MAX_ACTIVE_EXECUTIONS_CHECKPOINT_BYTES:
                _raise_checkpoint_size_error()
    return size


def _json_string_content_size(value: str, *, limit: int) -> int:
    """Return Pydantic's JSON-byte cost for string content, bounded by *limit*."""
    size = 0
    for character in value:
        point = ord(character)
        if point < _JSON_CONTROL_CHARACTER_LIMIT:
            width = 2 if point in _JSON_SHORT_CONTROL_ESCAPES else 6
        elif character in {'"', "\\"}:
            width = 2
        elif point < _JSON_SINGLE_BYTE_LIMIT:
            width = 1
        elif point < _JSON_TWO_BYTE_LIMIT:
            width = 2
        elif point < _JSON_THREE_BYTE_LIMIT:
            # Lone surrogates remain one lower-bound byte here. The single
            # authoritative Pydantic encoding below retains its standard
            # UnicodeEncodeError behavior for such invalid text.
            width = 1 if _UTF16_HIGH_SURROGATE_START <= point <= _UTF16_LOW_SURROGATE_END else 3
        else:
            width = 4
        size += width
        if size > limit:
            _raise_checkpoint_size_error()
    return size


def _checkpoint_pieces(
    payload: str,
    *,
    budget: int,
    template: ActiveExecutionsChunkMessage,
) -> list[str]:
    """Split checkpoint JSON on Unicode boundaries under the wire-frame cap."""
    pieces: list[str] = []
    offset = 0
    while offset < len(payload):
        low = 1
        high = min(MAX_ACTIVE_EXECUTIONS_CHUNK_CHARACTERS, len(payload) - offset)
        accepted = 0
        while low <= high:
            middle = (low + high) // 2
            candidate = template.model_copy(update={"payload": payload[offset : offset + middle]})
            if len(candidate.model_dump_json().encode()) <= budget:
                accepted = middle
                low = middle + 1
            else:
                high = middle - 1
        if accepted == 0:
            msg = f"frame budget {budget} cannot hold an active-execution checkpoint fragment"
            raise ValueError(msg)
        pieces.append(payload[offset : offset + accepted])
        if len(pieces) > MAX_ACTIVE_EXECUTIONS_CHUNKS:
            msg = "active-execution checkpoint exceeds the protocol fragment-count limit"
            raise ValueError(msg)
        offset += accepted
    return pieces


class ProtocolErrorMessage(ProtocolModel):
    """Structured error envelope for protocol failures."""

    type: Literal["protocol_error"] = "protocol_error"
    request_id: str | None = None
    client_id: str = ""
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
        diagnostic = exception_to_diagnostic(
            error,
            scope=scope,
            operation=operation,
            code=code or getattr(error, "diagnostic_code", None),
        )
        return cls(
            request_id=request_id,
            code=diagnostic.code,
            message=diagnostic.summary,
            diagnostic=diagnostic,
        )


ServerMessage = Annotated[
    SubscribedMessage
    | EventMessage
    | EventBatchMessage
    | ActiveExecutionsChunkMessage
    | ProtocolErrorMessage,
    Field(discriminator="type"),
]
