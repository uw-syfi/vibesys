"""A run's usage records: the projection of its journal into one ``UsageRecord`` per agent turn.

A ``turn_usage_recorded`` event reports what a turn spent; the ``agent_execution_started`` event
with the same ``execution_id`` says who ran it and with what (role, provider, model, reasoning
effort). ``UsageFold`` joins them as the journal is read, so the records of a run are a pure
function of its events: it opens no file and reads no clock. ``usage_records`` is that function
for a whole event list, and ``server.run_summary`` embeds the fold so ``runs.summary`` serves the
records beside the totals they sum to.

A turn whose execution start was never recorded still yields a record; what the start would have
said is ``None``, not inferred. A start is kept after its turn, so a second usage event of one
execution (a retried turn) joins the same start.

Light like the helper: the standard library, Pydantic and ``server.host_run_summary``.

Public surface: ``UsageFold``, ``ExecutionStart``, ``usage_records``, ``USAGE_EVENTS``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, JsonValue, TypeAdapter, ValidationError

from server.host_run_summary import TokenTally, UsageRecord

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

_TIMESTAMP: Final[TypeAdapter[AwareDatetime]] = TypeAdapter(AwareDatetime)

USAGE_EVENTS: Final = frozenset({"agent_execution_started", "turn_usage_recorded"})
"""The event types ``UsageFold`` reads."""


class ExecutionStart(BaseModel):
    """What an ``agent_execution_started`` event says about who ran a turn."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    execution_id: str
    role: str | None
    harness: str | None
    model: str | None
    client: Literal["scripted", "provider"] | None = None
    reasoning_effort: str | None
    harness_version: str | None
    started_at: AwareDatetime | None


@dataclass
class UsageFold:
    """Joins usage events to execution starts, event by event, in journal order."""

    _starts: dict[str, ExecutionStart] = field(default_factory=dict)
    _records: list[UsageRecord] = field(default_factory=list)

    @classmethod
    def from_state(
        cls, starts: Iterable[ExecutionStart], records: Iterable[UsageRecord]
    ) -> UsageFold:
        """A fold that has folded what ``starts`` and ``records`` hold."""
        return cls({start.execution_id: start for start in starts}, list(records))

    @property
    def starts(self) -> tuple[ExecutionStart, ...]:
        """The execution starts folded so far."""
        return tuple(self._starts.values())

    @property
    def records(self) -> tuple[UsageRecord, ...]:
        """The records of the turns folded so far, in journal order."""
        return tuple(self._records)

    def consume(self, event: Mapping[str, JsonValue]) -> UsageRecord | None:
        """Fold one journal event; the record a ``turn_usage_recorded`` event adds, else ``None``."""
        data = event.get("data")
        if not isinstance(data, dict):
            return None
        kind = event.get("type")
        if kind == "agent_execution_started" and data.get("kind") == "agent_execution_started":
            execution_id = event.get("execution_id")
            if isinstance(execution_id, str):
                client = _client(data.get("client"))
                scripted = client == "scripted"
                self._starts[execution_id] = ExecutionStart(
                    execution_id=execution_id,
                    role=_text(event.get("agent_kind")),
                    harness=None if scripted else _text(data.get("provider")),
                    model=None if scripted else _text(data.get("model")),
                    client=client,
                    reasoning_effort=None if scripted else _text(data.get("reasoning_effort")),
                    harness_version=None if scripted else _text(data.get("harness_version")),
                    started_at=_time(event.get("timestamp")),
                )
        elif kind == "turn_usage_recorded" and data.get("kind") == "turn_usage_recorded":
            record = self._record(event, data)
            self._records.append(record)
            return record
        return None

    def _record(self, event: Mapping[str, JsonValue], data: dict[str, JsonValue]) -> UsageRecord:
        execution_id = _text(event.get("execution_id"))
        start = self._starts.get(execution_id) if execution_id is not None else None
        return UsageRecord(
            run_id=_text(event.get("run_id")),
            execution_id=execution_id,
            span_id=_text(data.get("span_id")),
            round_label=_text(event.get("round_label")),
            role=None if start is None else start.role,
            harness=None if start is None else start.harness,
            model=None if start is None else start.model,
            client=None if start is None else start.client,
            reasoning_effort=None if start is None else start.reasoning_effort,
            harness_version=None if start is None else start.harness_version,
            started_at=None if start is None else start.started_at,
            ended_at=_time(event.get("timestamp")),
            tokens=TokenTally(
                input=_count(data.get("input_tokens")),
                cache_read=_count(data.get("cache_read_input_tokens")),
                cache_write=_count(data.get("cache_creation_input_tokens")),
                output=_count(data.get("output_tokens")),
                reasoning=_count(data.get("reasoning_output_tokens")),
            ),
            cost_usd=_cost(data.get("total_cost_usd")),
            duration_ms=_count(data.get("duration_ms")),
        )


def usage_records(events: Iterable[Mapping[str, JsonValue]]) -> list[UsageRecord]:
    """The usage records of a journal's ``events``, one per finished turn, in journal order.

    An event at or before the last folded sequence is skipped whole, as every fold of the journal
    skips it, so a journal replayed with a repeated prefix has each turn once.
    """
    fold = UsageFold()
    last = 0
    for event in events:
        sequence = event.get("sequence")
        if type(sequence) is not int or sequence <= last:
            continue
        last = sequence
        fold.consume(event)
    return list(fold.records)


def _text(value: JsonValue) -> str | None:
    return value if isinstance(value, str) and value else None


def _client(value: JsonValue) -> Literal["scripted", "provider"] | None:
    if value == "scripted":
        return "scripted"
    if value == "provider":
        return "provider"
    return None


def _count(value: JsonValue) -> int | None:
    """A reported token count; anything else (absent, null, malformed) is not reported."""
    return value if type(value) is int and value >= 0 else None


def _cost(value: JsonValue) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def _time(value: JsonValue) -> AwareDatetime | None:
    try:
        return _TIMESTAMP.validate_python(value)
    except ValidationError:
        return None
