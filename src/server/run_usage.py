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

A turn's usage rows (``usage_rows``) are what a reader prices: one per model the turn ran, each
keyed on how its cost was reached (``UsageCostBasis``) and counting the turn's requests on the
primary model only, so rows of any number of turns sum without counting a turn twice. A turn
journaled before the split existed has no rows of its own; ``legacy_basis`` is the one place that
reads such a turn (see its docstring for when it goes).

Light like the helper: the standard library, Pydantic and ``server.host_run_summary``.

Public surface: ``UsageFold``, ``ExecutionStart``, ``usage_records``, ``USAGE_EVENTS``,
``UsageCostBasis``, ``UsageRow``, ``usage_rows``, ``basis_of``, ``legacy_basis``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, JsonValue, TypeAdapter, ValidationError

from server.host_run_summary import (
    ModelUsage,
    RecordedCostBasis,
    TokenTally,
    UsageRecord,
    UsageSpeedName,
)

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
                self._starts[execution_id] = ExecutionStart(
                    execution_id=execution_id,
                    role=_text(event.get("agent_kind")),
                    harness=_text(data.get("provider")),
                    model=_text(data.get("model")),
                    client=_client(data.get("client")),
                    reasoning_effort=_text(data.get("reasoning_effort")),
                    harness_version=_text(data.get("harness_version")),
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
            cost_basis=_recorded_basis(data.get("cost_basis")),
            by_model=_rows(data.get("by_model")),
            provider_session_id=_text(data.get("provider_session_id")),
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


class UsageCostBasis(StrEnum):
    """How a row's cost was reached: ``vibesys.store.v1.UsageCostBasis`` by name.

    The first three and ``NOT_REPORTED`` are what the journal records for a turn
    (``UsageRecord.cost_basis``). The two ``LEGACY_*`` are never recorded: ``legacy_basis`` derives
    them for a line written before the field existed.
    """

    PRICE_TABLE = "price_table"
    PROVIDER_REPORTED = "provider_reported"
    UNKNOWN_MODEL = "unknown_model"
    NOT_REPORTED = "not_reported"
    LEGACY_RECORDED = "legacy_recorded"
    LEGACY_UNPRICED = "legacy_unpriced"


@dataclass(frozen=True, slots=True)
class UsageRow:
    """One model's share of one turn, keyed for pricing: the unit a rollup adds up.

    Cost is linear in the tokens of rows that share a model, speed and basis, so a reader can sum
    such rows across turns and price the sum. ``recorded_usd`` is agentshim's figure for the row,
    summed as given; ``turns`` is the requests the row counts, which only the primary row of a
    turn does.
    """

    model: str | None
    speed: UsageSpeedName
    basis: UsageCostBasis
    tokens: TokenTally
    cache_write_1h: int | None
    recorded_usd: float | None
    turns: int
    primary: bool


def legacy_basis(record: UsageRecord) -> UsageCostBasis:
    """The basis of a line written before ``cost_basis`` existed: recorded if it has a cost.

    A deliberate read branch on pre-schema journals, and the only one: every line written since
    records a basis (``NOT_REPORTED`` when the provider reported no cost), so a line without one is
    old, whatever its provider. The journal is not migrated: a rewrite would mutate journals on
    every host, and a stamp would turn a cache (the rollup) into the source of truth. Such a line
    has no per-model split either, so it reads as one row of its own tokens. Delete this function
    and the two ``LEGACY_*`` members 92 days after the change that added ``cost_basis`` merged,
    when no such line can fall in a window the Usage screen shows (#3531).
    """
    return (
        UsageCostBasis.LEGACY_RECORDED
        if record.cost_usd is not None
        else UsageCostBasis.LEGACY_UNPRICED
    )


def basis_of(record: UsageRecord) -> UsageCostBasis:
    """How the turn's cost was reached: as recorded, else by ``legacy_basis``."""
    if record.cost_basis is None:
        return legacy_basis(record)
    return UsageCostBasis(record.cost_basis)


def usage_rows(record: UsageRecord) -> tuple[UsageRow, ...]:
    """The rows of one turn: its models when the journal split them, else the turn as one row.

    The rows of a split turn are a superset of ``record.tokens`` (which counts the main
    conversation only) and sum to the whole turn. Every row carries the turn's basis, and the
    turn's requests are counted once, on the primary row. A row's basis is the turn's, not the
    row's own pricing state: a priced row of a turn with an unpriced sibling is ``UNKNOWN_MODEL``
    with its ``recorded_usd`` set. A turn with no split is one row of ``record.tokens`` and
    ``record.cost_usd`` that counts one request; its model is the one the session was configured
    with, ``None`` when the journal does not hold it. A split the reader could not parse reads as
    no split, so its turn is that one row at the whole turn's cost, as every turn was before the
    split existed.
    """
    basis = basis_of(record)
    if not record.by_model:
        return (
            UsageRow(
                model=record.model,
                speed="standard",
                basis=basis,
                tokens=record.tokens,
                cache_write_1h=None,
                recorded_usd=record.cost_usd,
                turns=1,
                primary=True,
            ),
        )
    return tuple(_row_of(row, basis) for row in record.by_model)


def _row_of(row: ModelUsage, basis: UsageCostBasis) -> UsageRow:
    return UsageRow(
        model=row.model,
        speed=row.speed or "standard",
        basis=basis,
        tokens=TokenTally(
            input=row.input,
            cache_read=row.cache_read,
            cache_write=row.cache_write,
            output=row.output,
            reasoning=row.reasoning,
        ),
        cache_write_1h=row.cache_write_1h,
        recorded_usd=row.recorded_usd,
        turns=row.turns,
        primary=row.primary,
    )


def _recorded_basis(value: JsonValue) -> RecordedCostBasis | None:
    if value == "price_table":
        return "price_table"
    if value == "provider_reported":
        return "provider_reported"
    if value == "unknown_model":
        return "unknown_model"
    if value == "not_reported":
        return "not_reported"
    return None


def _speed(value: JsonValue) -> UsageSpeedName | None:
    if value == "standard":
        return "standard"
    if value == "fast":
        return "fast"
    if value == "ultrafast":
        return "ultrafast"
    return None


def _rows(value: JsonValue) -> tuple[ModelUsage, ...]:
    """The rows a journal line's ``by_model`` holds; none when the line has no split.

    A malformed row makes the whole split unreported: the rows that remain would sum to less than
    the turn, and a partial split reads as complete.
    """
    if not isinstance(value, list):
        return ()
    rows: list[ModelUsage] = []
    for item in value:
        row = _row(item)
        if row is None:
            return ()
        rows.append(row)
    return tuple(rows)


def _row(item: JsonValue) -> ModelUsage | None:
    if not isinstance(item, dict):
        return None
    model = _text(item.get("model"))
    counts = [
        _count(item.get(name))
        for name in (
            "input_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "cache_creation_1h_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
            "turns",
        )
    ]
    primary = item.get("primary")
    if model is None or None in counts or not isinstance(primary, bool):
        return None
    input_, cache_read, cache_write, cache_write_1h, output, reasoning, turns = counts
    return ModelUsage(
        model=model,
        input=input_ or 0,
        cache_read=cache_read or 0,
        cache_write=cache_write or 0,
        cache_write_1h=cache_write_1h or 0,
        output=output or 0,
        reasoning=reasoning or 0,
        turns=turns or 0,
        recorded_usd=_cost(item.get("recorded_usd")),
        speed=_speed(item.get("speed")),
        primary=primary,
    )


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
