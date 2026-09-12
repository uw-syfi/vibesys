"""Typed, append-only event contract exposed to frontend clients."""

from __future__ import annotations

import json
import threading
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path  # noqa: TC003  # tracked: #288
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, ValidationError, model_validator

from server.diagnostics import Diagnostic
from server.run_lifecycle import RunStatus

if TYPE_CHECKING:
    from collections.abc import Iterable
    from typing import BinaryIO


class EventType(StrEnum):  # noqa: D101  # tracked: #288
    SERVER_STARTED = "server_started"
    SERVER_READY = "server_ready"
    CONFIGURATION_FAILED = "configuration_failed"
    RUN_STARTED = "run_started"
    EXPERIMENTS_CHANGED = "experiments_changed"
    RUN_INTERRUPTED = "run_interrupted"
    RUN_STATUS_CHANGED = "run_status_changed"
    CHAT = "chat"
    CHAT_THREAD_CREATED = "chat_thread_created"
    STATUS_QUERY = "status_query"
    CONTROL = "control"
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
    RUN_FINISHED = "run_finished"
    RUN_FAILED = "run_failed"
    OUTPUT = "output"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    TODO_UPDATE = "todo_update"
    USAGE_UPDATE = "usage_update"


class EventStatus(StrEnum):  # noqa: D101  # tracked: #288
    ACTIVE = "active"
    ANSWERED = "answered"
    PENDING = "pending"
    CONSUMED = "consumed"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


OutputStream = Literal["stdout", "stderr"]
"""Which host stream a captured line of server output came from."""

AgentOutputChannel = Literal["assistant", "analysis", "tool", "diagnostic", "prompt"]
"""Presentation channel for streamed agent output."""


class EventPayload(BaseModel):
    """Immutable base for every structured event payload.

    Payloads are frozen so ``EventStore`` can hand the same stored object to
    every reader instead of copying the whole history on each replay. Producers
    build new payloads; ``model_copy(update=...)`` still works on frozen models.
    """

    model_config = ConfigDict(frozen=True)


class ChatData(EventPayload):  # noqa: D101  # tracked: #288
    kind: Literal["chat"] = "chat"
    answer: str
    # The authoritative thread title, set by the server on the turn that
    # titles a previously untitled thread so clients learn it from replay.
    thread_title: str | None = None
    # Identity of the turn this answer closes: the same id the turn's streamed
    # chunks carried, so clients fold the terminal answer over exactly that
    # turn and never over an abandoned one. None on records written before the
    # field existed, for which clients keep the last-open-turn heuristic.
    invocation_id: str | None = None


class ChatThreadCreatedData(EventPayload):
    """Identity and resolved agent settings for one experiment-chat thread.

    Replayed by clients to rebuild the thread list; the default thread is
    implicit and never records one of these.
    """

    kind: Literal["chat_thread_created"] = "chat_thread_created"
    thread_id: str
    title: str = ""
    driver: str
    provider: str
    model: str
    created_at: datetime


class InvocationStartedData(EventPayload):  # noqa: D101  # tracked: #288
    kind: Literal["invocation_started"] = "invocation_started"
    system_prompt: str
    user_prompt: str


class InvocationFinishedData(EventPayload):  # noqa: D101  # tracked: #288
    kind: Literal["invocation_finished"] = "invocation_finished"
    result: Any = None
    error: str | None = None


ExecutionActivityMode = Literal["thinking", "responding", "tool", "waiting"]


class AgentExecutionActivityData(EventPayload):
    """Complete current activity for an active agent execution."""

    kind: Literal["agent_execution_activity_changed"] = "agent_execution_activity_changed"
    mode: ExecutionActivityMode
    summary: str
    tool: str | None = None


class AgentExecutionStartedData(EventPayload):
    """Semantic context for one prompt-to-result agent execution."""

    kind: Literal["agent_execution_started"] = "agent_execution_started"
    stage: str
    attempt: int | None = None
    system_prompt: str = ""
    user_prompt: str = ""
    activity: AgentExecutionActivityData
    driver: str | None = None
    provider: str | None = None
    model: str | None = None


class AgentExecutionFinishedData(EventPayload):
    """Terminal result for one agent execution."""

    kind: Literal["agent_execution_finished"] = "agent_execution_finished"
    result: Any = None
    error: str | None = None


class OutputData(EventPayload):  # noqa: D101  # tracked: #288
    kind: Literal["output"] = "output"
    stream: OutputStream
    source: str = "backend"
    content: str


class ServerReadyData(EventPayload):  # noqa: D101  # tracked: #288
    kind: Literal["server_ready"] = "server_ready"
    socket_protocol: Literal["jsonl"] = "jsonl"


class RunStartedData(EventPayload):  # noqa: D101  # tracked: #288
    kind: Literal["run_started"] = "run_started"
    outer_loop: str
    input: str
    max_rounds: int
    # The agent roles the loop can run per round (vibesys.loops.roles), so
    # frontends seed placeholders from the contract instead of a client-side
    # table. Empty on events recorded before the field existed.
    expected_roles: tuple[str, ...] = ()


class RunInterruptedData(EventPayload):  # noqa: D101  # tracked: #288
    kind: Literal["run_interrupted"] = "run_interrupted"
    reason: str
    signal: str | None = None


class RunStatusChangedData(EventPayload):
    """One move of the run through its lifecycle.

    Carries the whole transition so a client folds the status instead of
    inferring it: ``status`` is the new value and ``previous`` the one it
    replaced. Which invocation boundary a pause landed on is on the event
    envelope (``agent_kind``, ``round_label``, ``execution_id``) like every
    other execution-scoped fact, not repeated here.
    """

    kind: Literal["run_status_changed"] = "run_status_changed"
    status: RunStatus
    previous: RunStatus


class ExperimentsChangedData(EventPayload):  # noqa: D101  # tracked: #288
    kind: Literal["experiments_changed"] = "experiments_changed"
    reason: Literal["project_attached", "active_hypothesis_changed", "round_persisted"]


class ConfigurationFailedData(EventPayload):  # noqa: D101  # tracked: #288
    kind: Literal["configuration_failed"] = "configuration_failed"
    code: str
    stage: str
    message: str
    usage: str | None = None
    exit_code: int


class PhaseData(EventPayload):  # noqa: D101  # tracked: #288
    kind: Literal["phase"] = "phase"
    phase: str
    attempt: int | None = None


class AgentStatusData(EventPayload):
    """Structured progress readings for one agent invocation.

    Carried on presentation events so renderers can format their own status
    prefix (e.g. ``[Round 3/24 | Implementer | 12.3s | 20k/1.0M]``) without
    the server baking any layout or styling into the payload.
    """

    progress: str | None = None
    agent_label: str | None = None
    elapsed_seconds: float = 0.0
    input_tokens: int = 0
    context_window: int | None = None


class AgentOutputChunkData(EventPayload):  # noqa: D101  # tracked: #288
    kind: Literal["agent_output_chunk"] = "agent_output_chunk"
    channel: AgentOutputChannel
    content: str
    status: AgentStatusData | None = None


class ToolCallData(EventPayload):  # noqa: D101  # tracked: #288
    kind: Literal["tool_call"] = "tool_call"
    tool: str
    call_id: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    status: AgentStatusData | None = None


class CommandResultPayload(EventPayload):
    """Structured result of a command-style tool execution."""

    kind: Literal["command"] = "command"
    stdout: str
    stderr: str
    exit_code: int | None = None
    duration: float | None = None
    """Wall-clock execution time in seconds."""


class JsonResultPayload(EventPayload):
    """A tool result that is a JSON object or array, already parsed."""

    kind: Literal["json"] = "json"
    value: dict[str, Any] | list[Any]


ToolResultPayload = Annotated[
    CommandResultPayload | JsonResultPayload,
    Field(discriminator="kind"),
]
"""Typed structure a producer preserved alongside the raw result text."""


class ToolResultData(EventPayload):  # noqa: D101  # tracked: #288
    kind: Literal["tool_result"] = "tool_result"
    tool: str
    call_id: str | None = None
    content: str
    is_error: bool = False
    # ``content`` stays the raw, always-present text (fidelity, logs, replay).
    # Frontends render ``payload`` when present and fall back to ``content``.
    payload: ToolResultPayload | None = None


class TodoItemData(EventPayload):  # noqa: D101  # tracked: #288
    content: str
    # Expected values are "pending" / "in_progress" / "completed", but the
    # field stays open: todo payloads originate from agent tool calls, and an
    # unknown status must degrade in the renderer, not fail event emission.
    status: str


class TodoUpdateData(EventPayload):  # noqa: D101  # tracked: #288
    kind: Literal["todo_update"] = "todo_update"
    todos: list[TodoItemData] = Field(default_factory=list)


class UsageUpdateData(EventPayload):  # noqa: D101  # tracked: #288
    kind: Literal["usage_update"] = "usage_update"
    input_tokens: int
    context_window: int | None = None
    model: str | None = None


class SubprocessOutputData(EventPayload):  # noqa: D101  # tracked: #288
    kind: Literal["subprocess_output"] = "subprocess_output"
    process_id: str
    process_kind: str
    stream: OutputStream
    content: str


class JudgeResultData(EventPayload):  # noqa: D101  # tracked: #288
    kind: Literal["judge_result"] = "judge_result"
    verdict: Literal["pass", "fail"]
    feedback: str
    attempt: int


class BenchmarkResultData(EventPayload):  # noqa: D101  # tracked: #288
    kind: Literal["benchmark_result"] = "benchmark_result"
    metric: str
    value: FiniteFloat
    unit: str


class RoundFinishedData(EventPayload):  # noqa: D101  # tracked: #288
    kind: Literal["round_finished"] = "round_finished"
    attempts: int
    judge_verdict: Literal["pass", "fail", "skipped"]
    perf_metric: FiniteFloat | None = None
    perf_unit: str | None = None
    # True when no fresh profile ran this round; such a round records no perf
    # reading (perf_metric stays None). Defaults False so legacy persisted
    # events stay valid.
    profile_skipped: bool = False


EventData = Annotated[
    ChatData
    | ChatThreadCreatedData
    | InvocationStartedData
    | InvocationFinishedData
    | AgentExecutionStartedData
    | AgentExecutionActivityData
    | AgentExecutionFinishedData
    | OutputData
    | ServerReadyData
    | RunStartedData
    | RunInterruptedData
    | RunStatusChangedData
    | ExperimentsChangedData
    | ConfigurationFailedData
    | PhaseData
    | AgentOutputChunkData
    | SubprocessOutputData
    | JudgeResultData
    | BenchmarkResultData
    | RoundFinishedData
    | ToolCallData
    | ToolResultData
    | TodoUpdateData
    | UsageUpdateData,
    Field(discriminator="kind"),
]


class RunEvent(BaseModel):
    """One reproducible human, control, or invocation event.

    Frozen: a recorded event is a durable fact. Readers that need a variant
    build one with ``model_copy(update=...)`` rather than mutating a shared
    object, which lets ``EventStore`` replay history without copying it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal[1] = 1
    sequence: int = Field(default=0, ge=0)
    run_id: str = ""
    timestamp: datetime
    type: EventType
    text: str = ""
    diagnostic: Diagnostic | None = None
    status: EventStatus | None = None
    round_label: str | None = None
    agent_kind: str | None = None
    invocation_id: str | None = None
    execution_id: str | None = None
    # Which experiment-chat thread a chat event belongs to. None is the
    # default thread, preserving events written before threads existed.
    chat_thread_id: str | None = None
    data: EventData | None = None

    @model_validator(mode="before")
    @classmethod
    def _execution_identity_compatibility(cls, value: Any) -> Any:  # noqa: ANN401
        """Expose legacy invocation identity through the canonical field."""
        if not isinstance(value, dict):
            return value
        result = dict(value)
        execution_id = result.get("execution_id")
        invocation_id = result.get("invocation_id")
        if execution_id is None and invocation_id is not None:
            result["execution_id"] = invocation_id
        elif invocation_id is None and execution_id is not None:
            # Retain the old field during the protocol migration so older
            # presentation clients can still correlate streamed output.
            result["invocation_id"] = execution_id
        return result


_EAGER_TAIL_RECORDS = 1024
"""How many trailing records ``EventStore`` validates at construction.

The final record decides malformed-tail truncation, so it must be parsed
eagerly. Widening that to a window also keeps the common attach-then-read-the
-tail path free of any lazy parse, at a bounded cost on an empty run.
"""


@dataclass(frozen=True, slots=True)
class EventHeader:
    """Scalar identity of one stored record, recovered without full validation.

    ``sequence`` is the repaired cursor value ``read`` will report, not
    necessarily the integer on disk. ``execution_id`` already folds in the
    legacy ``invocation_id`` field the same way :class:`RunEvent` does.
    """

    sequence: int
    type: EventType
    execution_id: str | None
    chat_thread_id: str | None


_UNLOCATED = -1
"""Offset of a record that is only in memory, never read back from disk."""


@dataclass(slots=True)
class _StoredRecord:
    """One record's location on disk plus its parse, once something forces it.

    ``offset`` is ``_UNLOCATED`` for a record this process appended, and for
    every record on the eager fallback path: those already carry ``event``, so
    nothing ever asks the file for them again.
    """

    header: EventHeader
    offset: int
    length: int
    raw_sequence: int
    event: RunEvent | None = None


class EventStore:
    """Serialize event access so readers never observe partial JSONL writes.

    Read contract: reads return the stored ``RunEvent`` objects themselves, in
    a fresh list. ``RunEvent`` and its payloads are frozen, so readers project
    history with ``model_copy(update=...)`` instead of mutating what they read.
    Copying every event per read cost ~1.9s on a 72k-event history, paid again
    on each new subscription's full replay.

    Construction only scans the log with ``json.loads`` (measured ~2.6x cheaper
    than full validation) to learn each record's byte range and header fields,
    then validates the tail. Older records are validated when a read reaches
    them and cached from then on. Any doubt during the scan discards the index
    and falls back to validating the whole file, so a corrupt history still
    raises from ``__init__``: the worst case is a slow attach, never wrong
    state.
    """

    def __init__(self, path: Path, run_id: str):  # noqa: ANN204, D107  # tracked: #288
        self.path = path
        self.run_id = run_id
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._parsed_records = 0
        scanned = self._scan_unlocked()
        if scanned is None:
            events, self._malformed_tail_offset = self._read_unlocked()
            self._records = _records_from_events(_repair_legacy_sequences(events))
        else:
            self._records, self._malformed_tail_offset = scanned
        # A valid final record whose line was never terminated must gain its
        # newline before ``append`` writes anything after it.
        self._missing_tail_newline = self._malformed_tail_offset is None and _ends_without_newline(
            self.path
        )
        self._sequences = [record.header.sequence for record in self._records]
        self._next_sequence = self._sequences[-1] + 1 if self._sequences else 1

    def append(self, event: RunEvent) -> RunEvent:  # noqa: D102  # tracked: #288
        with self._changed:
            if self._malformed_tail_offset is not None:
                with self.path.open("r+b") as stream:
                    stream.truncate(self._malformed_tail_offset)
                self._malformed_tail_offset = None
            if self._missing_tail_newline:
                # Terminate the valid final record so the new record starts
                # its own line instead of concatenating onto it. The flag is a
                # construction-time observation, so recheck the file itself: if
                # it was removed or replaced since, a blind "\n" would corrupt
                # the fresh file's first record.
                if _ends_without_newline(self.path):
                    with self.path.open("a", encoding="utf-8") as stream:
                        stream.write("\n")
                self._missing_tail_newline = False
            event = event.model_copy(
                update={"sequence": self._next_sequence, "run_id": self.run_id}
            )
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(event.model_dump_json() + "\n")
            self._next_sequence += 1
            self._records.append(
                _StoredRecord(
                    header=_header_from_event(event, event.sequence),
                    offset=_UNLOCATED,
                    length=0,
                    raw_sequence=event.sequence,
                    event=event,
                )
            )
            self._sequences.append(event.sequence)
            self._changed.notify_all()
            return event

    @property
    def last_sequence(self) -> int:  # noqa: D102  # tracked: #288
        with self._lock:
            return self._next_sequence - 1

    @property
    def parsed_record_count(self) -> int:
        """How many stored records have been validated into models so far.

        Accounting for callers that must assert an attach stayed lazy without
        resorting to timing.
        """
        with self._lock:
            return self._parsed_records

    def event_headers(self) -> list[EventHeader]:
        """Return every stored record's header, in replay order, unparsed.

        This is the whole log's shape at scan cost. Consumers that only need
        event types and identities (a lifecycle index) read it instead of
        forcing the history into models.
        """
        with self._lock:
            return [record.header for record in self._records]

    def read(  # noqa: D102  # tracked: #288
        self, after_sequence: int = 0, before_sequence: int | None = None
    ) -> list[RunEvent]:
        with self._lock:
            return self._events_after_unlocked(after_sequence, before_sequence)

    def read_sequences(self, sequences: Iterable[int]) -> list[RunEvent]:
        """Return the records at the given cursor values, in the order asked.

        Unknown sequences are skipped. Only the named records are validated,
        which is what lets a consumer inspect a handful of rare payloads
        without paying for the history around them.
        """
        with self._lock:
            records: list[_StoredRecord] = []
            for sequence in sequences:
                index = bisect_left(self._sequences, sequence)
                if index < len(self._sequences) and self._sequences[index] == sequence:
                    records.append(self._records[index])
            self._force_parse_unlocked(records)
            return [record.event for record in records if record.event is not None]

    def wait(self, after_sequence: int, timeout: float | None = None) -> list[RunEvent]:
        """Block until replayable events exist after a client's cursor."""
        with self._changed:
            events = self._events_after_unlocked(after_sequence)
            if events:
                return events
            self._changed.wait(timeout)
            return self._events_after_unlocked(after_sequence)

    def wait_for_change(self, after_sequence: int, timeout: float | None = None) -> bool:
        """Block until a record exists past the cursor; report it, parse nothing.

        A subscriber that only needs to know the stream moved must not pay to
        validate the window it moved by. On a resumed run that window is the
        entire durable history.
        """
        with self._changed:
            if self._next_sequence - 1 > after_sequence:
                return True
            self._changed.wait(timeout)
            return self._next_sequence - 1 > after_sequence

    def notify_change(self) -> None:
        """Wake every waiter without appending, for a store being retired.

        A waiter blocked on a store the run has replaced would otherwise sleep
        out its timeout before noticing that the store it should read is a
        different object.
        """
        with self._changed:
            self._changed.notify_all()

    def _events_after_unlocked(
        self, after_sequence: int, before_sequence: int | None = None
    ) -> list[RunEvent]:
        start = bisect_right(self._sequences, after_sequence)
        stop = (
            len(self._records)
            if before_sequence is None
            else bisect_left(self._sequences, before_sequence)
        )
        if stop <= start:
            return []
        # A bounded read must only force the records it returns; that is what
        # keeps a backfill query off the whole log.
        window = self._records[start:stop]
        self._force_parse_unlocked(window)
        # A new list, so callers own the sequence; the frozen events inside it
        # stay shared with the store.
        return [record.event for record in window if record.event is not None]

    def _force_parse_unlocked(self, records: list[_StoredRecord]) -> None:
        """Validate any of these records not yet in memory, in log order.

        Records adjacent on disk are fetched in one read, so a dense range
        costs one seek while a sparse targeted read costs one seek per record.
        """
        pending = [record for record in records if record.event is None]
        if not pending:
            return
        with self.path.open("rb") as stream:
            run: list[_StoredRecord] = []
            for record in pending:
                if run and record.offset != run[-1].offset + run[-1].length:
                    self._parse_run_unlocked(stream, run)
                    run = []
                run.append(record)
            self._parse_run_unlocked(stream, run)

    def _parse_run_unlocked(self, stream: BinaryIO, run: list[_StoredRecord]) -> None:
        base = run[0].offset
        stream.seek(base)
        blob = stream.read(run[-1].offset + run[-1].length - base)
        for record in run:
            begin = record.offset - base
            self._parse_record(record, blob[begin : begin + record.length])

    def _parse_record(self, record: _StoredRecord, raw: bytes) -> None:
        self._parsed_records += 1
        event = RunEvent.model_validate_json(raw)
        if record.header.sequence != record.raw_sequence:
            # Only a legacy out-of-order or duplicate sequence needs the copy;
            # every other record is handed out exactly as it was written.
            event = event.model_copy(update={"sequence": record.header.sequence})
        record.event = event

    def _scan_unlocked(self) -> tuple[list[_StoredRecord], int | None] | None:
        """Index the log by byte range and header, or return None on any doubt.

        ``json.loads`` is a real parser, so the offsets and header fields it
        yields are exact. Returning None sends construction to the fully eager
        path, which is the only place a corrupt history is diagnosed.
        """
        if not self.path.exists():
            return [], None
        raw = self.path.read_bytes()
        lines = raw.splitlines(keepends=True)
        records: list[_StoredRecord] = []
        malformed_tail_offset: int | None = None
        offset = 0
        last_sequence = 0
        for index, line in enumerate(lines):
            record_offset = offset
            offset += len(line)
            header_fields = _scan_header_fields(line)
            if header_fields is None:
                # A final line holding complete JSON the header scan cannot
                # classify must be judged by full validation, so it falls to
                # the eager path; only a tail with no complete JSON prefix (a
                # torn append) is set aside for repair.
                if index != len(lines) - 1 or _starts_with_complete_json(line):
                    return None
                # Preserve access to earlier audit history if a process was
                # interrupted during its final append.
                malformed_tail_offset = record_offset
                break
            raw_sequence, event_type, execution_id, chat_thread_id = header_fields
            sequence = raw_sequence if raw_sequence > last_sequence else last_sequence + 1
            last_sequence = sequence
            records.append(
                _StoredRecord(
                    header=EventHeader(
                        sequence=sequence,
                        type=event_type,
                        execution_id=execution_id,
                        chat_thread_id=chat_thread_id,
                    ),
                    offset=record_offset,
                    length=len(line),
                    raw_sequence=raw_sequence,
                )
            )
        self._parse_eager_tail(raw, records, malformed_tail_offset)
        return records, malformed_tail_offset

    def _parse_eager_tail(
        self, raw: bytes, records: list[_StoredRecord], malformed_tail_offset: int | None
    ) -> None:
        """Validate the trailing window, raising on any record that fails.

        Every record here scanned as complete JSON, which a torn append can
        never leave behind, so a validation failure on the final record is
        corruption to surface, not an interrupted write to set aside.
        """
        for position in range(max(0, len(records) - _EAGER_TAIL_RECORDS), len(records)):
            record = records[position]
            try:
                self._parse_record(record, raw[record.offset : record.offset + record.length])
            except ValidationError as error:
                if position != len(records) - 1 or malformed_tail_offset is not None:
                    raise
                raise _complete_invalid_tail_error(self.path, record.offset) from error

    def _read_unlocked(self) -> tuple[list[RunEvent], int | None]:
        if not self.path.exists():
            return [], None
        lines = self.path.read_bytes().splitlines(keepends=True)
        events: list[RunEvent] = []
        offset = 0
        for index, line in enumerate(lines):
            record_offset = offset
            offset += len(line)
            try:
                self._parsed_records += 1
                event = RunEvent.model_validate_json(line)
                events.append(event)
            except ValidationError as error:
                if index != len(lines) - 1:
                    raise
                if _starts_with_complete_json(line):
                    raise _complete_invalid_tail_error(self.path, record_offset) from error
                # Preserve access to earlier audit history if a process was
                # interrupted during its final append.
                return events, record_offset
        return events, None


def _scan_header_fields(line: bytes) -> tuple[int, EventType, str | None, str | None] | None:
    """Recover one record's header fields cheaply, or None if anything is off.

    Every rejection here (non-object record, absent or non-integer
    ``sequence``, unknown ``type``, non-string identity) is a case where
    :class:`RunEvent` validation could disagree with the scan, so the caller
    must reparse the history the strict way rather than guess.
    """
    try:
        record = json.loads(line)
    except ValueError:
        return None
    if not isinstance(record, dict):
        return None
    sequence = record.get("sequence")
    # ``type is not int`` also rejects bool, which pydantic would coerce.
    if type(sequence) is not int or sequence < 0:
        return None
    try:
        event_type = EventType(record.get("type"))
    except ValueError:
        return None
    execution_id = record.get("execution_id")
    if execution_id is None:
        # RunEvent exposes legacy invocation identity through execution_id.
        execution_id = record.get("invocation_id")
    chat_thread_id = record.get("chat_thread_id")
    if not _is_optional_str(execution_id) or not _is_optional_str(chat_thread_id):
        return None
    return sequence, event_type, execution_id, chat_thread_id


def _is_optional_str(value: Any) -> bool:  # noqa: ANN401  # scanning untyped JSON
    return value is None or isinstance(value, str)


def _starts_with_complete_json(line: bytes) -> bool:
    """Whether the bytes begin with one complete JSON value.

    A torn append leaves a strict prefix of the record plus its newline, and
    no strict prefix of a serialized object contains a complete JSON value, so
    this discriminates an interrupted write from fully written bytes: a lone
    record, or complete records concatenated onto one line by a writer that
    never terminated it. Truncating the latter would erase durable facts, so
    it must be surfaced as corruption, not repaired as a torn tail.
    """
    try:
        text = line.decode()
    except UnicodeDecodeError:
        return False
    try:
        json.JSONDecoder().raw_decode(text)
    except ValueError:
        return False
    return True


def _ends_without_newline(path: Path) -> bool:
    """Whether the file's final byte leaves its last record unterminated."""
    if not path.exists():
        return False
    size = path.stat().st_size
    if size == 0:
        return False
    with path.open("rb") as stream:
        stream.seek(size - 1)
        return stream.read(1) != b"\n"


def _complete_invalid_tail_error(path: Path, offset: int) -> ValueError:
    """Corruption error for a fully written final record that fails validation.

    Unlike a torn append, the record is complete, so silently truncating it
    would erase a durable fact; the operator must inspect the file instead.
    """
    return ValueError(f"complete final record at byte offset {offset} in {path} failed validation")


def _header_from_event(event: RunEvent, sequence: int) -> EventHeader:
    return EventHeader(
        sequence=sequence,
        type=event.type,
        execution_id=event.execution_id,
        chat_thread_id=event.chat_thread_id,
    )


def _records_from_events(events: list[RunEvent]) -> list[_StoredRecord]:
    """Wrap already-validated events as stored records with no disk location."""
    return [
        _StoredRecord(
            header=_header_from_event(event, event.sequence),
            offset=_UNLOCATED,
            length=0,
            raw_sequence=event.sequence,
            event=event,
        )
        for event in events
    ]


def _repair_legacy_sequences(events: list[RunEvent]) -> list[RunEvent]:
    """Expose a stable, strictly increasing cursor without rewriting the audit log."""
    repaired: list[RunEvent] = []
    last_sequence = 0
    for event in events:
        repaired_event = (
            event.model_copy(update={"sequence": last_sequence + 1})
            if event.sequence <= last_sequence
            else event
        )
        repaired.append(repaired_event)
        last_sequence = repaired_event.sequence
    return repaired


def make_event(event_type: EventType, text: str = "", **fields: Any) -> RunEvent:  # noqa: ANN401, D103  # tracked: #288
    return RunEvent(timestamp=datetime.now(UTC), type=event_type, text=text, **fields)


def json_value(value: Any) -> Any:  # noqa: ANN401, D103  # tracked: #288
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    try:
        json.dumps(value)
        return value  # noqa: TRY300  # tracked: #288
    except TypeError:
        return repr(value)
