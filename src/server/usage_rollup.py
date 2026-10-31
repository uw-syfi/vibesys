"""A run's usage rolled up to the quarter hour: the cache the Usage screen reads instead of a journal.

``build_rollup`` is a pure function of what a fold of the run's journal holds (``SummaryState``)
and when its kept ideas landed: the same journal always gives the same rollup, byte for byte, so
two hosts, or two concurrent reads of one host, cannot disagree. Each turn becomes the rows
``server.run_usage.usage_rows`` gives it, added into the row of its quarter hour, provider, model,
speed, role and cost basis. A row never mixes turns priced in different ways, so its cost is
linear in its tokens, and a reader prices a sum of rows as it would each turn.

``read_or_build`` is the one path from a journal to a rollup. It reads the stored rollup and
serves it when its version is this writer's and its journal identity is the journal's now, and
otherwise folds the journal and stores the result. It does not go through the summary cache's own
write, so a finished run whose summary is valid and which has no rollup gets one (that write is
skipped when the summary already covers the journal). The journal and the file sit behind
``UsageJournal`` and ``RollupStore``, so the native run log can implement the first at the
transport migration and a test can implement both in memory.

A cold fold costs the journal's bytes, charged to a ``Budget`` shared by every run of one read. A
run the budget cannot pay for is reported ``PENDING`` with the stale rollup if it has one of this
version, and the caller asks again: the first run of a budget is always paid, so each call settles
at least one run and a fixed set of journals settles in a finite number of calls.

Light: the standard library, ``vs_proto`` and the light ``server`` modules; no file is opened here.

Public surface: ``ROLLUP_VERSION``, ``ROLLUP_FILE``, ``JournalIdentity``, ``FoldedJournal``,
``UsageJournal``, ``RollupStore``, ``Budget``, ``Outcome``, ``RollupRead``, ``build_rollup``,
``is_fresh``, ``read_or_build``, ``quarter_of`` and ``QUARTER_SECONDS``.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Protocol

from server.run_usage import UsageCostBasis, usage_rows
from vs_proto.api import Timestamp, UsageSpeed, usage_state
from vs_proto.api import UsageCostBasis as WireCostBasis

if TYPE_CHECKING:
    from collections.abc import Iterable

    from server.host_run_summary import RateLimitReading, UsageRecord
    from server.run_summary import SummaryState

ROLLUP_VERSION: Final = 2
"""The version of what a rollup holds and how its rows are made. A stored rollup of another
version is rebuilt, so bump it with any change to ``build_rollup`` or to the fold of kept ideas.

2: ``new_bests`` hold every kept idea of the run, not only those the digest's capped dashboard keeps."""

ROLLUP_FILE: Final = "run-usage.pb"
"""The rollup's file, beside the run's journal."""

QUARTER_SECONDS: Final = 900
"""The grain of a row. Every IANA offset is a multiple of fifteen minutes, so a client's day or
hour boundary in any zone is a whole quarter."""

_BASES: Final = {
    UsageCostBasis.PRICE_TABLE: WireCostBasis.PRICE_TABLE,
    UsageCostBasis.PROVIDER_REPORTED: WireCostBasis.PROVIDER_REPORTED,
    UsageCostBasis.UNKNOWN_MODEL: WireCostBasis.UNKNOWN_MODEL,
    UsageCostBasis.NOT_REPORTED: WireCostBasis.NOT_REPORTED,
    UsageCostBasis.LEGACY_RECORDED: WireCostBasis.LEGACY_RECORDED,
    UsageCostBasis.LEGACY_UNPRICED: WireCostBasis.LEGACY_UNPRICED,
}
_SPEEDS: Final = {
    "standard": UsageSpeed.STANDARD,
    "fast": UsageSpeed.FAST,
    "ultrafast": UsageSpeed.ULTRAFAST,
}


@dataclass(frozen=True)
class JournalIdentity:
    """Which prefix of a journal a derived file describes: its complete lines and their digests."""

    boundary: int
    head_sha256: str
    tail_sha256: str


@dataclass(frozen=True)
class FoldedJournal:
    """A journal folded: what its summary holds, when its kept ideas landed, and the prefix it covers."""

    state: SummaryState
    bests: tuple[datetime, ...]
    identity: JournalIdentity
    persistable: bool
    """Whether the fold covers complete lines only. A fold that counted a line the writer has not
    ended is right for this read and wrong for a file that outlives it."""


class UsageJournal(Protocol):
    """One run's journal as the rollup reads it; the native run log implements it at its migration."""

    def identity(self) -> JournalIdentity | None:
        """The journal's complete lines now; ``None`` when the host holds no readable journal."""
        ...

    def cold_bytes(self) -> int:
        """The most bytes a fold of the journal reads now."""
        ...

    def fold(self) -> FoldedJournal:
        """Fold the journal as it is now."""
        ...


class RollupStore(Protocol):
    """Where one run's rollup is kept."""

    def read(self) -> usage_state.RunUsageRollup | None:
        """The stored rollup; ``None`` when there is none or it cannot be read."""
        ...

    def write(self, rollup: usage_state.RunUsageRollup) -> None:
        """Replace the stored rollup. A store that cannot write loses only time."""
        ...


class Budget:
    """The bytes one read may still fold. The first charge is always paid, so a read progresses."""

    def __init__(self, remaining: int) -> None:
        """A budget of ``remaining`` bytes, at least one."""
        if remaining < 1:
            message = f"a budget needs at least one byte, got {remaining}"
            raise ValueError(message)
        self.remaining = remaining
        self._charged = False

    def charge(self, cost: int) -> bool:
        """Pay ``cost`` if the budget has it, or if nothing was paid yet; whether it was paid."""
        if self._charged and cost > self.remaining:
            return False
        self._charged = True
        self.remaining = max(0, self.remaining - cost)
        return True


class Outcome(StrEnum):
    """How a rollup came to be returned."""

    FRESH = "fresh"
    """The stored rollup describes the journal as it is."""
    BUILT = "built"
    """The journal was folded and the rollup made."""
    PENDING = "pending"
    """The budget could not pay for a fold; the rollup, if any, is a stale one."""
    MISSING = "missing"
    """The host holds no readable journal."""


@dataclass(frozen=True)
class RollupRead:
    """What ``read_or_build`` found for one run."""

    outcome: Outcome
    rollup: usage_state.RunUsageRollup | None


def quarter_of(at: datetime) -> int:
    """The quarter hour ``at`` falls in: epoch seconds over ``QUARTER_SECONDS``, rounded down."""
    return math.floor(at.timestamp() / QUARTER_SECONDS)


def is_fresh(rollup: usage_state.RunUsageRollup, identity: JournalIdentity) -> bool:
    """Whether ``rollup`` was built by this writer from the journal ``identity`` names."""
    journal = rollup.journal
    return (
        rollup.version == ROLLUP_VERSION
        and journal is not None
        and (journal.boundary, journal.head_sha256, journal.tail_sha256)
        == (identity.boundary, identity.head_sha256, identity.tail_sha256)
    )


def read_or_build(journal: UsageJournal, store: RollupStore, budget: Budget) -> RollupRead:
    """The run's rollup as the journal is now, folded only if the stored one does not describe it."""
    identity = journal.identity()
    if identity is None:
        return RollupRead(Outcome.MISSING, None)
    stored = store.read()
    if stored is not None and is_fresh(stored, identity):
        return RollupRead(Outcome.FRESH, stored)
    if not budget.charge(journal.cold_bytes()):
        usable = stored if stored is not None and stored.version == ROLLUP_VERSION else None
        return RollupRead(Outcome.PENDING, usable)
    folded = journal.fold()
    rollup = build_rollup(folded.state, folded.bests, folded.identity)
    if folded.persistable:
        store.write(rollup)
    return RollupRead(Outcome.BUILT, rollup)


def build_rollup(
    state: SummaryState, bests: Iterable[datetime], identity: JournalIdentity
) -> usage_state.RunUsageRollup:
    """The rollup of the journal prefix ``identity`` names, whose fold is ``state``.

    A turn with no end time cannot be placed in a quarter: it counts in ``untimed_turns`` and has
    no row. Rows, session ids and limits come out in a fixed order, so equal folds give equal bytes.
    """
    totals: dict[_Key, _Sum] = defaultdict(_Sum)
    untimed = 0
    session_ids: set[str] = set()
    complete = True
    first: datetime | None = None
    last: datetime | None = None
    for record in state.records:
        if record.provider_session_id is None:
            complete = False
        else:
            session_ids.add(record.provider_session_id)
        if record.ended_at is None:
            untimed += 1
            continue
        began = min(record.started_at or record.ended_at, record.ended_at)
        first = began if first is None else min(first, began)
        last = record.ended_at if last is None else max(last, record.ended_at)
        for key, tally in _rows_of(record, record.ended_at):
            totals[key].add(tally)
    rollup = usage_state.RunUsageRollup(
        version=ROLLUP_VERSION,
        journal=usage_state.JournalIdentity(
            boundary=identity.boundary,
            head_sha256=identity.head_sha256,
            tail_sha256=identity.tail_sha256,
        ),
        rows=[_row(key, totals[key]) for key in sorted(totals)],
        provider_session_ids=sorted(session_ids),
        session_ids_complete=complete,
        last_limits=[_limit(reading) for reading in state.rate_limits],
        new_bests=[_timestamp(at) for at in sorted(bests)],
        untimed_turns=untimed,
    )
    if first is not None and last is not None:
        rollup.first_turn_at = _timestamp(first)
        rollup.last_turn_at = _timestamp(last)
    return rollup


_PROVIDER_BYTES: Final = 256
_MODEL_BYTES: Final = 512
_ROLE_BYTES: Final = 256
_WORDS_BYTES: Final = 256
"""What ``RunUsageRollup`` allows a name of each kind (``usage_state.proto``)."""


@dataclass(frozen=True, order=True)
class _Key:
    quarter: int
    provider: str
    model: str
    speed: int
    role: str
    basis: int


@dataclass
class _Sum:
    """The counts of the turns added into one row."""

    input: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cache_write_1h: int = 0
    output: int = 0
    reasoning: int = 0
    turns: int = 0
    recorded: list[float] | None = None

    def add(self, other: _Sum) -> None:
        self.input += other.input
        self.cache_read += other.cache_read
        self.cache_write += other.cache_write
        self.cache_write_1h += other.cache_write_1h
        self.output += other.output
        self.reasoning += other.reasoning
        self.turns += other.turns
        if other.recorded is not None:
            self.recorded = [*(self.recorded or []), *other.recorded]


def _rows_of(record: UsageRecord, ended_at: datetime) -> Iterable[tuple[_Key, _Sum]]:
    quarter = quarter_of(ended_at)
    for row in usage_rows(record):
        key = _Key(
            quarter=quarter,
            provider=_fit(record.harness, _PROVIDER_BYTES),
            model=_fit(row.model, _MODEL_BYTES),
            speed=_SPEEDS[row.speed],
            role=_fit(record.role, _ROLE_BYTES),
            basis=_BASES[row.basis],
        )
        tokens = row.tokens
        yield (
            key,
            _Sum(
                input=tokens.input or 0,
                cache_read=tokens.cache_read or 0,
                cache_write=tokens.cache_write or 0,
                cache_write_1h=row.cache_write_1h or 0,
                output=tokens.output or 0,
                reasoning=tokens.reasoning or 0,
                turns=row.turns,
                recorded=None if row.recorded_usd is None else [row.recorded_usd],
            ),
        )


def _row(key: _Key, total: _Sum) -> usage_state.UsageRollupRow:
    row = usage_state.UsageRollupRow(
        quarter=key.quarter,
        provider=key.provider,
        model=key.model,
        speed=UsageSpeed(key.speed),
        role=key.role,
        basis=WireCostBasis(key.basis),
        input=total.input,
        cache_read=total.cache_read,
        cache_write=total.cache_write,
        cache_write_1h=total.cache_write_1h,
        output=total.output,
        reasoning=total.reasoning,
        turns=total.turns,
    )
    if total.recorded is not None:
        row.recorded_usd = math.fsum(total.recorded)
    return row


def _fit(text: str | None, limit: int) -> str:
    """``text`` (empty when the journal does not say), cut to at most ``limit`` bytes of whole characters.

    The rollup's own rules bound a name, so a journal that holds a longer one still gives a
    rollup the file store will take back, instead of one it would fold again on every read.
    """
    text = text or ""
    encoded = text.encode()
    return text if len(encoded) <= limit else encoded[:limit].decode(errors="ignore")


def _limit(reading: RateLimitReading) -> usage_state.LimitReading:
    wire = usage_state.LimitReading(
        provider=_fit(reading.provider, _PROVIDER_BYTES),
        exhausted=reading.exhausted,
        observed_at=_timestamp(reading.observed_at),
    )
    if reading.window is not None:
        wire.window = _fit(reading.window, _WORDS_BYTES)
    if reading.limit is not None:
        wire.limit = _fit(reading.limit, _WORDS_BYTES)
    if reading.used_fraction is not None and math.isfinite(reading.used_fraction):
        wire.used_fraction = max(0.0, reading.used_fraction)
    if reading.resets_at is not None:
        wire.resets_at = _timestamp(reading.resets_at)
    if reading.window_minutes is not None:
        wire.window_minutes = reading.window_minutes
    return wire


def _timestamp(at: datetime) -> Timestamp:
    return Timestamp.from_datetime(at.astimezone(UTC))


__all__ = [
    "QUARTER_SECONDS",
    "ROLLUP_FILE",
    "ROLLUP_VERSION",
    "Budget",
    "FoldedJournal",
    "JournalIdentity",
    "Outcome",
    "RollupRead",
    "RollupStore",
    "UsageJournal",
    "build_rollup",
    "is_fresh",
    "quarter_of",
    "read_or_build",
]
