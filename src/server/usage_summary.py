"""What the Usage screen shows for one host: rollups, cut into the caller's buckets and priced.

``summarize`` is a pure function of the rollups of a host's runs (``server.usage_rollup``), the
bucket edges and "now" the caller sent, and a ``Pricing`` that says what a row's tokens cost at
today's list prices. It opens no file and reads no clock, so any reply is a function of its
arguments, and every host given one request cuts the same buckets.

A row of a rollup is placed in the bucket of its quarter hour. Rows are added into series keyed by
origin, provider, model, speed, the source of the estimate and the source read; a series is
columnar (one array per quantity, one value per populated bucket), so a key is sent once however
many buckets it fills. The distinct counts (runs, sessions) are kept apart, since they do not add
across buckets or hosts.

The reply is bounded: ``USAGE_REPLY_MAX_BYTES`` of encoded protobuf, under the 1 MiB the host
process's output and the gateway's response both allow. The lists carry fixed limits (the proto
rules), and the series, which have none that matter, are folded until the reply fits: the cheapest
series first into ``other`` per provider, then the providers, then the speeds and sources. A fold
only adds, so every total is the same before and after; ``folded_series`` says how many it took.

Light: the standard library, ``vs_proto`` and the rollup module; no agentshim.

Public surface: ``summarize``, ``SummaryInputs``, ``RunInput``, ``Pricing``, ``Priced``,
``OutsideInput``, ``USAGE_REPLY_MAX_BYTES``, ``MAX_RUN_ROWS``, ``MAX_AGENT_ROWS``, ``MAX_PROVIDERS`` and
``OTHER``.
"""

from __future__ import annotations

import bisect
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from itertools import pairwise
from typing import TYPE_CHECKING, Final, Protocol

from server.usage_rollup import QUARTER_SECONDS
from vs_proto.api import Timestamp, UsageCostBasis, UsageSpeed, host_payloads, usage_state

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from server.usage_outside import ProviderRow

USAGE_REPLY_MAX_BYTES: Final = 768 * 1024
"""The most bytes a reply encodes to. The host process's output and the gateway's response each
stop at 1 MiB (``hostcommand.MaxOutput`` and ``hostoperations.MaxPayload``); this leaves the
envelope and a quarter of the line free."""

MAX_RUN_ROWS: Final = 128
"""The costliest runs a reply lists; the rest are in ``runs_other``."""
MAX_AGENT_ROWS: Final = 64
"""The costliest agents a reply lists; the rest are in ``agents_other``."""
MAX_PROVIDERS: Final = 16
"""The most providers a reply names; past it they are ``other``."""
OTHER: Final = "other"
"""The provider or model of everything a reply folded together."""

_MODEL_MAX_BYTES: Final = 128
_ROLE_MAX_BYTES: Final = 64
_PROVIDER: Final = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_RECENT_QUARTERS: Final = 4
_MAX_RATES: Final = 256
_MIN_EDGES: Final = 2
_APPROXIMATE: Final = frozenset({UsageCostBasis.LEGACY_RECORDED, UsageCostBasis.LEGACY_UNPRICED})
_SOURCE: Final = usage_state.UsageEstimateSource
_ORIGIN: Final = usage_state.UsageOrigin


@dataclass(frozen=True)
class Priced:
    """What one rollup row's tokens cost at today's list prices, and how that was reached."""

    model: str
    """The id the series is keyed by: the price list's name after the person's ``Map to``."""
    source: usage_state.UsageEstimateSource
    estimate_usd: float = 0.0
    input_usd: float = 0.0
    cache_read_usd: float = 0.0
    cache_write_usd: float = 0.0
    output_usd: float = 0.0
    speed_premium_usd: float = 0.0
    cache_savings_usd: float = 0.0


class Pricing(Protocol):
    """Today's list prices and the person's overrides, as the summary reads them."""

    def price(self, provider: str, row: usage_state.UsageRollupRow) -> Priced:
        """The estimate for ``row`` in the order: override, reported, recorded, model, unpriced."""
        ...

    def resolved(
        self, provider: str, model: str, speed: UsageSpeed
    ) -> tuple[usage_state.UsageRate | None, usage_state.UsageEstimateSource]:
        """The rate ``model`` (an id ``price`` returned) is priced at for ``speed``, and its source.

        No rate when the model has none listed; the source is then ``UNPRICED``.
        """
        ...

    @property
    def pricing(self) -> usage_state.UsagePricing:
        """How current the list is."""
        ...

    @property
    def book(self) -> usage_state.UsagePriceBook:
        """What the person set."""
        ...

    @property
    def prices_status(self) -> usage_state.UsagePricesStatus:
        """Whether ``book`` is what the person set, or the host could not read it."""
        ...


@dataclass(frozen=True)
class RunInput:
    """One run's rollup, and where the run lives."""

    project: int
    """The index in the request's projects."""
    run_id: str
    rollup: usage_state.RunUsageRollup


@dataclass(frozen=True)
class OutsideInput:
    """One provider directory's sessions that no run made, and how completely it was read."""

    source: host_payloads.UsageSource
    """Its kind, root and status; the summary fills in the sessions it counts."""
    rows: Sequence[ProviderRow]


@dataclass(frozen=True)
class SummaryInputs:
    """Everything one reply is a function of."""

    request_id: str
    edges: Sequence[datetime]
    now: datetime
    runs: Sequence[RunInput]
    journal: host_payloads.UsageSource
    """The journal source; its ``scanned``, ``status`` and the like are the caller's to state."""
    pricing: Pricing
    pending_runs: int = 0
    pending_files: int = 0
    max_bytes: int = USAGE_REPLY_MAX_BYTES
    outside: Sequence[OutsideInput] = field(default=())
    """The transcript sources, in the order their index in a series key names (the journal is 0)."""


def summarize(inputs: SummaryInputs) -> host_payloads.UsageSummaryResponse:
    """The reply for ``inputs``; raises ``ValueError`` when the edges are not strictly increasing."""
    edge_seconds = [_seconds(edge) for edge in inputs.edges]
    if len(edge_seconds) < _MIN_EDGES or any(a >= b for a, b in pairwise(edge_seconds)):
        message = "edges: expected at least two, strictly increasing"
        raise ValueError(message)
    now = inputs.now
    built = _Builder(inputs, edge_seconds, now)
    for run in inputs.runs:
        built.add_run(run)
    for index, outside in enumerate(inputs.outside, start=1):
        built.add_outside(index, outside)
    response, columns = built.response()
    response.folded_series = _fit(response, columns, inputs.max_bytes)
    return response


class _Cell:
    """The quantities of one series in one bucket."""

    __slots__ = (
        "approximate_turns",
        "cache_read_usd",
        "cache_savings_usd",
        "cache_write",
        "cache_write_usd",
        "cached",
        "estimate_usd",
        "input_usd",
        "output",
        "output_usd",
        "reasoning",
        "recorded",
        "speed_premium_usd",
        "turns",
        "uncached",
    )

    def __init__(self) -> None:
        self.cached = self.cache_write = self.uncached = self.output = self.reasoning = 0
        self.turns = self.approximate_turns = 0
        self.recorded: list[float] = []
        self.estimate_usd: list[float] = []
        self.input_usd: list[float] = []
        self.cache_read_usd: list[float] = []
        self.cache_write_usd: list[float] = []
        self.output_usd: list[float] = []
        self.speed_premium_usd: list[float] = []
        self.cache_savings_usd: list[float] = []


_Key = tuple[int, str, str, int, int, int]
"""Origin, provider, model, speed, estimate source and source index of a series."""


class _Builder:
    def __init__(self, inputs: SummaryInputs, edge_seconds: list[int], now: datetime) -> None:
        self._inputs = inputs
        self._edges = edge_seconds
        self._buckets = len(edge_seconds) - 1
        self._providers = _provider_names(inputs)
        self._series: dict[_Key, dict[int, _Cell]] = defaultdict(dict)
        self._runs_in_bucket: dict[tuple[int, str], set[tuple[int, str]]] = defaultdict(set)
        self._sessions_in_bucket: dict[tuple[int, str], set[str]] = defaultdict(set)
        self._sessions_in_source: dict[int, set[str]] = defaultdict(set)
        self._run_costs: dict[tuple[int, str], dict[str, _Costs]] = {}
        self._agent_costs: dict[tuple[str, str], dict[str, _Costs]] = {}
        self._bests: dict[int, int] = defaultdict(int)
        self._recent: dict[str, _Costs] = {}
        self._rates: dict[tuple[str, str, int], host_payloads.UsageResolvedRate] = {}
        self._now_quarter = math.floor(_seconds(now) / QUARTER_SECONDS)

    def add_run(self, run: RunInput) -> None:
        identity = (run.project, run.run_id)
        for best in run.rollup.new_bests:
            bucket = self._bucket_at(best.seconds)
            if bucket is not None:
                self._bests[bucket] += 1
        for row in run.rollup.rows:
            provider = self._providers[row.provider]
            priced = self._inputs.pricing.price(row.provider, row)
            model = priced.model if len(priced.model.encode()) <= _MODEL_MAX_BYTES else OTHER
            if self._now_quarter - _RECENT_QUARTERS < row.quarter <= self._now_quarter:
                self._recent.setdefault(provider, _Costs()).add(priced, row)
            bucket = self._bucket(row.quarter)
            if bucket is None:
                continue
            key = (_ORIGIN.RUN, provider, model, int(row.speed), int(priced.source), 0)
            _add_row(self._series[key].setdefault(bucket, _Cell()), row, priced)
            self._runs_in_bucket[(bucket, provider)].add(identity)
            self._run_costs.setdefault(identity, {}).setdefault(provider, _Costs()).add(priced, row)
            role = row.role.encode()[:_ROLE_MAX_BYTES].decode(errors="ignore")
            self._agent_costs.setdefault((role, model), {}).setdefault(provider, _Costs()).add(
                priced, row
            )
            self._rate(row, provider, model, priced)

    def add_outside(self, index: int, outside: OutsideInput) -> None:
        """Add the sessions of one transcript source; its series are read from source ``index``."""
        for found in outside.rows:
            row = _as_row(found)
            provider = self._providers[found.provider]
            priced = self._inputs.pricing.price(found.provider, row)
            model = priced.model if len(priced.model.encode()) <= _MODEL_MAX_BYTES else OTHER
            if self._now_quarter - _RECENT_QUARTERS < row.quarter <= self._now_quarter:
                self._recent.setdefault(provider, _Costs()).add(priced, row)
            bucket = self._bucket(row.quarter)
            if bucket is None:
                continue
            key = (_ORIGIN.OUTSIDE_RUNS, provider, model, int(row.speed), int(priced.source), index)
            _add_row(self._series[key].setdefault(bucket, _Cell()), row, priced)
            self._sessions_in_bucket[(bucket, provider)].add(found.row.session_id)
            self._sessions_in_source[index].add(found.row.session_id)
            self._rate(row, provider, model, priced)

    def _rate(
        self, row: usage_state.UsageRollupRow, provider: str, model: str, priced: Priced
    ) -> None:
        """Remember the rate a row's model is priced at, once per provider, model and speed."""
        key = (provider, model, int(row.speed))
        if key in self._rates or len(self._rates) >= _MAX_RATES:
            return
        rate, source = self._inputs.pricing.resolved(row.provider, priced.model, row.speed)
        message = host_payloads.UsageResolvedRate(
            provider=provider, model=model, speed=row.speed, source=source
        )
        if rate is not None:
            message.rate = rate
        self._rates[key] = message

    def _bucket(self, quarter: int) -> int | None:
        return self._bucket_at(quarter * QUARTER_SECONDS)

    def _bucket_at(self, seconds: int) -> int | None:
        if seconds < self._edges[0] or seconds >= self._edges[-1]:
            return None
        return bisect.bisect_right(self._edges, seconds) - 1

    def response(self) -> tuple[host_payloads.UsageSummaryResponse, list[_Columns]]:
        inputs = self._inputs
        columns = [_columns_of(key, cells) for key, cells in sorted(self._series.items())]
        runs, runs_other = _cut_runs(self._run_costs)
        agents, agents_other = _cut_agents(self._agent_costs)
        distinct = host_payloads.UsageSource.from_binary(inputs.journal.to_binary())
        distinct.distinct_sessions = len(
            {run for found in self._runs_in_bucket.values() for run in found}
        )
        sources = [distinct]
        for index, outside in enumerate(inputs.outside, start=1):
            counted = host_payloads.UsageSource.from_binary(outside.source.to_binary())
            counted.distinct_sessions = len(self._sessions_in_source.get(index, ()))
            sources.append(counted)
        response = host_payloads.UsageSummaryResponse(
            request_id=inputs.request_id,
            series=[_message(each) for each in columns],
            counts=[
                host_payloads.UsageBucketCount(
                    bucket=bucket,
                    provider=provider,
                    runs=len(self._runs_in_bucket.get((bucket, provider), ())),
                    outside_sessions=len(self._sessions_in_bucket.get((bucket, provider), ())),
                )
                for bucket, provider in sorted({*self._runs_in_bucket, *self._sessions_in_bucket})
            ],
            bests=[
                host_payloads.UsageBucketBests(bucket=bucket, count=count)
                for bucket, count in sorted(self._bests.items())
            ],
            runs=runs,
            runs_other=runs_other,
            agents=agents,
            agents_other=agents_other,
            recent=_provider_costs(self._recent),
            sources=sources,
            pricing=inputs.pricing.pricing,
            prices=inputs.pricing.book,
            rates=[self._rates[key] for key in sorted(self._rates)],
            pending_runs=inputs.pending_runs,
            pending_files=inputs.pending_files,
            read_at=Timestamp.from_datetime(inputs.now.astimezone(UTC)),
            prices_status=inputs.pricing.prices_status,
        )
        return response, columns


class _Costs:
    """The estimate, recorded cost and tokens of rows added together."""

    __slots__ = ("estimate", "recorded", "tokens")

    def __init__(self) -> None:
        self.estimate: list[float] = []
        self.recorded: list[float] = []
        self.tokens = 0

    def add(self, priced: Priced, row: usage_state.UsageRollupRow) -> None:
        self.estimate.append(priced.estimate_usd)
        if row.has_field("recorded_usd"):
            self.recorded.append(row.recorded_usd)
        self.tokens += row.input + row.output


def _add_row(cell: _Cell, row: usage_state.UsageRollupRow, priced: Priced) -> None:
    cell.cached += row.cache_read
    cell.cache_write += row.cache_write
    cell.uncached += max(0, row.input - row.cache_read - row.cache_write)
    cell.output += row.output
    cell.reasoning += row.reasoning
    cell.turns += row.turns
    if row.basis in _APPROXIMATE:
        cell.approximate_turns += row.turns
    if row.has_field("recorded_usd"):
        cell.recorded.append(row.recorded_usd)
    cell.estimate_usd.append(priced.estimate_usd)
    cell.input_usd.append(priced.input_usd)
    cell.cache_read_usd.append(priced.cache_read_usd)
    cell.cache_write_usd.append(priced.cache_write_usd)
    cell.output_usd.append(priced.output_usd)
    cell.speed_premium_usd.append(priced.speed_premium_usd)
    cell.cache_savings_usd.append(priced.cache_savings_usd)


def _provider_names(inputs: SummaryInputs) -> dict[str, str]:
    """Each raw provider id as the reply names it: itself if it is an id, else ``other``.

    Past ``MAX_PROVIDERS`` ids the later ones (in name order) are ``other`` too, so the reply's
    lists have a size that does not depend on how garbled a journal is.
    """
    raw = {row.provider for run in inputs.runs for row in run.rollup.rows}
    raw |= {found.provider for each in inputs.outside for found in each.rows}
    named = set(sorted(name for name in raw if _PROVIDER.fullmatch(name))[: MAX_PROVIDERS - 1])
    return {name: name if name in named else OTHER for name in raw}


def _as_row(found: ProviderRow) -> usage_state.UsageRollupRow:
    """A transcript row as a rollup row that reported no cost, so one pricing prices both."""
    row = found.row
    return usage_state.UsageRollupRow(
        quarter=row.quarter,
        provider=found.provider,
        model=row.model,
        speed=row.speed,
        basis=UsageCostBasis.NOT_REPORTED,
        input=row.input,
        cache_read=row.cache_read,
        cache_write=row.cache_write,
        cache_write_1h=row.cache_write_1h,
        output=row.output,
        reasoning=row.reasoning,
        turns=row.turns,
    )


def _provider_costs(costs: dict[str, _Costs]) -> list[host_payloads.UsageProviderCost]:
    return [
        host_payloads.UsageProviderCost(
            provider=provider,
            estimate_usd=math.fsum(each.estimate),
            recorded_usd=math.fsum(each.recorded),
            tokens=each.tokens,
        )
        for provider, each in sorted(costs.items())
    ]


def _cut_runs(
    costs: dict[tuple[int, str], dict[str, _Costs]],
) -> tuple[list[host_payloads.UsageRunRow], list[host_payloads.UsageProviderCost]]:
    ranked = sorted(costs.items(), key=lambda each: (-_total(each[1]), each[0]))
    shown, rest = ranked[:MAX_RUN_ROWS], ranked[MAX_RUN_ROWS:]
    rows = [
        host_payloads.UsageRunRow(
            project=project, run_id=run_id, costs=_provider_costs(by_provider)
        )
        for (project, run_id), by_provider in shown
    ]
    return rows, _provider_costs(_merged(by for _, by in rest))


def _cut_agents(
    costs: dict[tuple[str, str], dict[str, _Costs]],
) -> tuple[list[host_payloads.UsageAgentRow], list[host_payloads.UsageProviderCost]]:
    ranked = sorted(costs.items(), key=lambda each: (-_total(each[1]), each[0]))
    shown, rest = ranked[:MAX_AGENT_ROWS], ranked[MAX_AGENT_ROWS:]
    rows = [
        host_payloads.UsageAgentRow(role=role, model=model, costs=_provider_costs(by_provider))
        for (role, model), by_provider in shown
    ]
    return rows, _provider_costs(_merged(by for _, by in rest))


def _total(by_provider: dict[str, _Costs]) -> float:
    return math.fsum(value for each in by_provider.values() for value in each.estimate)


def _merged(groups: Iterable[dict[str, _Costs]]) -> dict[str, _Costs]:
    merged: dict[str, _Costs] = {}
    for group in groups:
        for provider, each in group.items():
            into = merged.setdefault(provider, _Costs())
            into.estimate.extend(each.estimate)
            into.recorded.extend(each.recorded)
            into.tokens += each.tokens
    return merged


def _seconds(at: datetime) -> int:
    return math.floor(at.timestamp())


# --- Fitting the reply under its bound ---


@dataclass
class _Columns:
    """One series as plain columns: what the folds add, before it is a message."""

    key: _Key
    bucket: list[int]
    cached: list[int]
    cache_write: list[int]
    uncached: list[int]
    output: list[int]
    reasoning: list[int]
    turns: list[int]
    approximate_turns: list[int]
    recorded_usd: list[list[float]]
    estimate_usd: list[list[float]]
    input_usd: list[list[float]]
    cache_read_usd: list[list[float]]
    cache_write_usd: list[list[float]]
    output_usd: list[list[float]]
    speed_premium_usd: list[list[float]]
    cache_savings_usd: list[list[float]]
    """The terms each bucket's cost is the exact sum of, so a fold adds without rounding twice."""


def _columns_of(key: _Key, cells: dict[int, _Cell]) -> _Columns:
    ordered = sorted(cells)
    return _Columns(
        key=key,
        bucket=ordered,
        cached=[cells[b].cached for b in ordered],
        cache_write=[cells[b].cache_write for b in ordered],
        uncached=[cells[b].uncached for b in ordered],
        output=[cells[b].output for b in ordered],
        reasoning=[cells[b].reasoning for b in ordered],
        turns=[cells[b].turns for b in ordered],
        approximate_turns=[cells[b].approximate_turns for b in ordered],
        recorded_usd=[cells[b].recorded for b in ordered],
        estimate_usd=[cells[b].estimate_usd for b in ordered],
        input_usd=[cells[b].input_usd for b in ordered],
        cache_read_usd=[cells[b].cache_read_usd for b in ordered],
        cache_write_usd=[cells[b].cache_write_usd for b in ordered],
        output_usd=[cells[b].output_usd for b in ordered],
        speed_premium_usd=[cells[b].speed_premium_usd for b in ordered],
        cache_savings_usd=[cells[b].cache_savings_usd for b in ordered],
    )


def _message(columns: _Columns) -> host_payloads.UsageSeries:
    origin, provider, model, speed, source, usage_source = columns.key
    return host_payloads.UsageSeries(
        key=host_payloads.UsageSeriesKey(
            origin=_ORIGIN(origin),
            provider=provider,
            model=model,
            speed=UsageSpeed(speed),
            source=_SOURCE(source),
            usage_source=usage_source,
        ),
        bucket=columns.bucket,
        cached=columns.cached,
        cache_write=columns.cache_write,
        uncached=columns.uncached,
        output=columns.output,
        reasoning=columns.reasoning,
        turns=columns.turns,
        approximate_turns=columns.approximate_turns,
        recorded_usd=[math.fsum(terms) for terms in columns.recorded_usd],
        estimate_usd=[math.fsum(terms) for terms in columns.estimate_usd],
        input_usd=[math.fsum(terms) for terms in columns.input_usd],
        cache_read_usd=[math.fsum(terms) for terms in columns.cache_read_usd],
        cache_write_usd=[math.fsum(terms) for terms in columns.cache_write_usd],
        output_usd=[math.fsum(terms) for terms in columns.output_usd],
        speed_premium_usd=[math.fsum(terms) for terms in columns.speed_premium_usd],
        cache_savings_usd=[math.fsum(terms) for terms in columns.cache_savings_usd],
    )


def _fit(response: host_payloads.UsageSummaryResponse, columns: list[_Columns], limit: int) -> int:
    """Fold the series of ``response`` until it encodes within ``limit`` bytes; how many were folded.

    Three folds, each tried while the reply is too large and still changes something: the cheaper
    half of the series into ``other`` per provider, then every series into ``other`` providers,
    then every speed and source together. The last leaves at most one series per origin and
    estimate source, which fits for any input the proto's limits allow.
    """
    folded = 0
    for coarsen in (_by_model, _by_provider, _by_speed_and_source):
        while len(response.to_binary()) > limit:
            before = sorted(each.key for each in columns)
            columns[:] = coarsen(columns)
            after = sorted(each.key for each in columns)
            response.series = [_message(each) for each in columns]
            folded += max(0, len(before) - len(after))
            if after == before:
                break
    return folded


def _weight(columns: _Columns) -> float:
    return (
        math.fsum(term for terms in columns.estimate_usd for term in terms)
        + sum(columns.output) * 1e-12
    )


def _by_model(series: list[_Columns]) -> list[_Columns]:
    """Fold the cheaper half of the series that are not ``other`` into their ``other``."""
    named = sorted((each for each in series if each.key[2] != OTHER), key=_weight)
    if not named:
        return series
    moving = {id(each) for each in named[: max(1, len(named) // 2)]}
    return _combine(
        [_rekeyed(each, model=OTHER) if id(each) in moving else each for each in series]
    )


def _by_provider(series: list[_Columns]) -> list[_Columns]:
    if all(each.key[1] == OTHER and each.key[2] == OTHER for each in series):
        return series
    return _combine([_rekeyed(each, model=OTHER, provider=OTHER) for each in series])


def _by_speed_and_source(series: list[_Columns]) -> list[_Columns]:
    return _combine(
        [
            _rekeyed(
                each, model=OTHER, provider=OTHER, speed=int(UsageSpeed.STANDARD), usage_source=0
            )
            for each in series
        ]
    )


def _rekeyed(
    columns: _Columns,
    *,
    model: str,
    provider: str | None = None,
    speed: int | None = None,
    usage_source: int | None = None,
) -> _Columns:
    """``columns`` under a coarser key."""
    origin, was_provider, _, was_speed, source, was_usage_source = columns.key
    return replace(
        columns,
        key=(
            origin,
            was_provider if provider is None else provider,
            model,
            was_speed if speed is None else speed,
            source,
            was_usage_source if usage_source is None else usage_source,
        ),
    )


def _combine(series: list[_Columns]) -> list[_Columns]:
    """The series with equal keys added together, in key order."""
    groups: dict[_Key, list[_Columns]] = {}
    for each in series:
        groups.setdefault(each.key, []).append(each)
    return [_added(key, group) for key, group in sorted(groups.items())]


def _added(key: _Key, group: list[_Columns]) -> _Columns:
    buckets = sorted({bucket for each in group for bucket in each.bucket})
    place = {bucket: index for index, bucket in enumerate(buckets)}

    def whole(name: str) -> list[int]:
        total = [0] * len(buckets)
        for each in group:
            for bucket, value in zip(each.bucket, getattr(each, name), strict=True):
                total[place[bucket]] += value
        return total

    def terms(name: str) -> list[list[float]]:
        found: list[list[float]] = [[] for _ in buckets]
        for each in group:
            for bucket, values in zip(each.bucket, getattr(each, name), strict=True):
                found[place[bucket]].extend(values)
        return found

    return _Columns(
        key=key,
        bucket=buckets,
        cached=whole("cached"),
        cache_write=whole("cache_write"),
        uncached=whole("uncached"),
        output=whole("output"),
        reasoning=whole("reasoning"),
        turns=whole("turns"),
        approximate_turns=whole("approximate_turns"),
        recorded_usd=terms("recorded_usd"),
        estimate_usd=terms("estimate_usd"),
        input_usd=terms("input_usd"),
        cache_read_usd=terms("cache_read_usd"),
        cache_write_usd=terms("cache_write_usd"),
        output_usd=terms("output_usd"),
        speed_premium_usd=terms("speed_premium_usd"),
        cache_savings_usd=terms("cache_savings_usd"),
    )
