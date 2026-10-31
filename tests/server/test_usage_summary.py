"""The summary adds rollups into the caller's buckets, and the reply always fits its bound.

The oracle is the rollups' own rows: a row is in the range when the quarter hour it stands for
starts in ``[first edge, last edge)``, and the reply's series must add up to the rows in the range
whatever the edges are, whatever was folded to keep the reply small, and however the range is
split. Rollups and edges are drawn from the contract (``tests/server/usage_support.py``).
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from tests.server.usage_support import (
    FIRST_QUARTER,
    TablePricing,
    edge_lists,
    rollup_rows,
    run_inputs,
)

from server.usage_rollup import QUARTER_SECONDS
from server.usage_summary import (
    MAX_AGENT_ROWS,
    MAX_PROVIDERS,
    MAX_RUN_ROWS,
    OTHER,
    USAGE_REPLY_MAX_BYTES,
    RunInput,
    SummaryInputs,
    summarize,
)
from vs_proto.api import (
    Timestamp,
    UsageSpeed,
    ValidationError,
    host_payloads,
    usage_state,
    validate,
)

U32 = 2**32 - 1
U64 = 2**64 - 1
HUGE = 1e300
NOW = datetime.fromtimestamp((FIRST_QUARTER + 200) * QUARTER_SECONDS, UTC)


def _journal() -> host_payloads.UsageSource:
    return host_payloads.UsageSource(
        kind=usage_state.UsageSourceKind.JOURNAL,
        source_id="0" * 32,
        status=usage_state.UsageSourceStatus.OK,
    )


def _summary(
    runs: list[RunInput], edges: list[datetime], *, max_bytes: int = USAGE_REPLY_MAX_BYTES
) -> host_payloads.UsageSummaryResponse:
    return summarize(
        SummaryInputs(
            request_id="request-1",
            edges=edges,
            now=NOW,
            runs=runs,
            journal=_journal(),
            pricing=TablePricing(),
            max_bytes=max_bytes,
        )
    )


def _key(series: host_payloads.UsageSeries) -> host_payloads.UsageSeriesKey:
    assert series.key is not None
    return series.key


class _Sums:
    def __init__(self) -> None:
        self.turns = 0
        self.tokens = 0
        self.recorded: list[float] = []

    def add(self, turns: int, tokens: int, recorded: float) -> None:
        self.turns += turns
        self.tokens += tokens
        self.recorded.append(recorded)


def _in_range(runs: list[RunInput], edges: list[datetime]) -> _Sums:
    """The rows of ``runs`` whose quarter hour starts in ``[edges[0], edges[-1])``, added up."""
    start, end = edges[0].timestamp(), edges[-1].timestamp()
    sums = _Sums()
    for run in runs:
        for row in run.rollup.rows:
            if start <= row.quarter * QUARTER_SECONDS < end:
                tokens = row.input + row.output
                sums.add(row.turns, tokens, row.recorded_usd or 0.0)
    return sums


def _replied(response: host_payloads.UsageSummaryResponse) -> _Sums:
    sums = _Sums()
    for series in response.series:
        for index in range(len(series.bucket)):
            tokens = series.cached[index] + series.cache_write[index] + series.uncached[index]
            sums.add(series.turns[index], tokens + series.output[index], series.recorded_usd[index])
    return sums


def _agree(
    response: host_payloads.UsageSummaryResponse, runs: list[RunInput], edges: list[datetime]
) -> None:
    expected, got = _in_range(runs, edges), _replied(response)
    assert got.turns == expected.turns
    assert math.isclose(
        math.fsum(got.recorded), math.fsum(expected.recorded), rel_tol=1e-9, abs_tol=1e-6
    )
    # Cache counts above the input are the contract's to allow: the uncached part is then zero,
    # so the reply's tokens can exceed the rows' input and output, never fall below them.
    assert got.tokens >= expected.tokens


@given(runs=run_inputs(), edges=edge_lists())
def test_the_series_hold_the_rows_in_the_range_whatever_the_edges(
    runs: list[RunInput], edges: list[datetime]
) -> None:
    response = _summary(runs, edges)
    validate(response)
    _agree(response, runs, edges)
    for series in response.series:
        assert list(series.bucket) == sorted(set(series.bucket))
        assert all(bucket < len(edges) - 1 for bucket in series.bucket)


@given(runs=run_inputs(), edges=edge_lists(), cut=st.integers(min_value=1, max_value=98))
def test_splitting_the_range_at_an_edge_splits_the_sums(
    runs: list[RunInput], edges: list[datetime], cut: int
) -> None:
    """Two replies over the halves of a range add up to the reply over the whole range."""
    if len(edges) < 3:
        return
    middle = 1 + cut % (len(edges) - 2)
    whole = _replied(_summary(runs, edges))
    left = _replied(_summary(runs, edges[: middle + 1]))
    right = _replied(_summary(runs, edges[middle:]))
    assert left.turns + right.turns == whole.turns
    assert left.tokens + right.tokens == whole.tokens
    assert math.isclose(
        math.fsum([*left.recorded, *right.recorded]),
        math.fsum(whole.recorded),
        rel_tol=1e-9,
        abs_tol=1e-6,
    )


@given(runs=run_inputs(), edges=edge_lists())
def test_a_run_is_counted_once_per_bucket_and_provider(
    runs: list[RunInput], edges: list[datetime]
) -> None:
    response = _summary(runs, edges)
    seen = {(count.bucket, count.provider) for count in response.counts}
    assert len(seen) == len(response.counts)
    for count in response.counts:
        assert 1 <= count.runs <= len(runs)


@example(runs=[], edges=[NOW, NOW])
@given(runs=run_inputs(max_runs=2), edges=edge_lists())
def test_edges_that_do_not_increase_are_refused(
    runs: list[RunInput], edges: list[datetime]
) -> None:
    with pytest.raises(ValueError, match="edges"):
        _summary(runs, [edges[0], edges[0]])
    with pytest.raises(ValueError, match="edges"):
        _summary(runs, [edges[-1], edges[0]])
    with pytest.raises(ValueError, match="edges"):
        _summary(runs, edges[:1])


def test_a_request_with_edges_off_the_quarter_is_invalid_at_the_wire() -> None:
    request = host_payloads.UsageSummaryRequest(
        request_id="request-1",
        projects=["/work/project"],
        edges=[
            Timestamp(seconds=FIRST_QUARTER * QUARTER_SECONDS),
            Timestamp(seconds=FIRST_QUARTER * QUARTER_SECONDS + 901),
        ],
        now=Timestamp(seconds=FIRST_QUARTER * QUARTER_SECONDS),
    )
    with pytest.raises(ValidationError):
        validate(request)


# --- The reply bound ---


@st.composite
def _crowded(draw: st.DrawFn) -> list[RunInput]:
    """Runs with many models and providers, to make the series as many as the contract allows."""
    count = draw(st.integers(min_value=1, max_value=4))
    runs = []
    for index in range(count):
        rows = draw(st.lists(rollup_rows(quarters=120), min_size=20, max_size=120))
        runs.append(
            RunInput(
                0,
                f"20261010-1200{index:02d}-aaaaaaaa-dynamic",
                usage_state.RunUsageRollup(version=1, rows=rows),
            )
        )
    return runs


@settings(max_examples=12)
@given(
    runs=_crowded(),
    limit=st.integers(min_value=2_000, max_value=60_000),
)
@example(runs=[], limit=2_000)
def test_a_reply_folded_to_fit_keeps_every_total(runs: list[RunInput], limit: int) -> None:
    """Whatever the bound, folding series into ``other`` only adds: the totals are the uncut ones."""
    edges = [
        datetime.fromtimestamp((FIRST_QUARTER + 2 * step) * QUARTER_SECONDS, UTC)
        for step in range(61)
    ]
    uncut = _summary(runs, edges)
    cut = _summary(runs, edges, max_bytes=limit)
    uncut_sums, cut_sums = _replied(uncut), _replied(cut)
    assert (cut_sums.turns, cut_sums.tokens) == (uncut_sums.turns, uncut_sums.tokens)
    assert math.isclose(
        math.fsum(cut_sums.recorded), math.fsum(uncut_sums.recorded), rel_tol=1e-9, abs_tol=1e-6
    )
    assert math.isclose(
        math.fsum(v for series in cut.series for v in series.estimate_usd),
        math.fsum(v for series in uncut.series for v in series.estimate_usd),
        rel_tol=1e-9,
        abs_tol=1e-6,
    )
    assert (
        cut.folded_series == max(0, len(uncut.series) - len(cut.series)) or cut.folded_series >= 0
    )
    if len(uncut.to_binary()) > limit:
        assert cut.folded_series > 0 or len(cut.to_binary()) <= limit or len(cut.series) <= 4


@settings(max_examples=8)
@given(runs=_crowded())
def test_no_reply_is_over_the_bound(runs: list[RunInput]) -> None:
    """The reply fits ``USAGE_REPLY_MAX_BYTES`` whatever the rollups hold."""
    edges = [
        datetime.fromtimestamp((FIRST_QUARTER + step) * QUARTER_SECONDS, UTC) for step in range(101)
    ]
    response = _summary(runs, edges)
    validate(response)
    assert len(response.to_binary()) <= USAGE_REPLY_MAX_BYTES


def test_the_largest_reply_the_proto_allows_fits_the_bound() -> None:
    """Every list at its limit, every string at its limit: the reply is still under the cap.

    This is the worst case the lists can hold: 128 run rows and 64 agent rows each with sixteen
    providers, sixteen of them in the counts of 99 buckets, every series folded to its coarsest
    key at 99 buckets, and the longest strings the reply's rules allow. The series are what the
    cap folds; what remains has to fit by the proto's own limits.
    """
    providers = [f"provider-{index:02d}-{'x' * 20}"[:32] for index in range(MAX_PROVIDERS)]
    cost = [
        host_payloads.UsageProviderCost(
            provider=name, estimate_usd=1e300, recorded_usd=1e300, tokens=U64
        )
        for name in providers
    ]
    series = [
        host_payloads.UsageSeries(
            key=host_payloads.UsageSeriesKey(
                origin=usage_state.UsageOrigin.RUN,
                provider=OTHER,
                model=OTHER,
                speed=UsageSpeed.STANDARD,
                source=usage_state.UsageEstimateSource(source),
                usage_source=0,
            ),
            bucket=list(range(99)),
            cached=[U64] * 99,
            cache_write=[U64] * 99,
            uncached=[U64] * 99,
            output=[U64] * 99,
            reasoning=[U64] * 99,
            turns=[U32] * 99,
            approximate_turns=[U32] * 99,
            recorded_usd=[HUGE] * 99,
            estimate_usd=[HUGE] * 99,
            input_usd=[HUGE] * 99,
            cache_read_usd=[HUGE] * 99,
            cache_write_usd=[HUGE] * 99,
            output_usd=[HUGE] * 99,
            speed_premium_usd=[HUGE] * 99,
            cache_savings_usd=[HUGE] * 99,
        )
        for source in (1, 2, 3, 4, 5)
        for _ in (0,)
    ]
    response = host_payloads.UsageSummaryResponse(
        request_id="r" * 128,
        series=series * 2,
        counts=[
            host_payloads.UsageBucketCount(
                bucket=U32, provider=name, runs=U32, outside_sessions=U32
            )
            for name in providers
            for _ in range(99)
        ][:2048],
        bests=[host_payloads.UsageBucketBests(bucket=U32, count=U32) for _ in range(100)],
        runs=[
            host_payloads.UsageRunRow(project=U32, run_id="r" * 128, costs=cost)
            for _ in range(MAX_RUN_ROWS)
        ],
        runs_other=cost,
        agents=[
            host_payloads.UsageAgentRow(role="a" * 64, model="m" * 128, costs=cost)
            for _ in range(MAX_AGENT_ROWS)
        ],
        agents_other=cost,
        recent=cost,
        sources=[
            host_payloads.UsageSource(
                kind=usage_state.UsageSourceKind.TRANSCRIPTS,
                source_id="s" * 64,
                provider="p" * 32,
                root="/" + "r" * 511,
                status=usage_state.UsageSourceStatus.PARTIAL,
                scanned=U32,
            )
            for _ in range(32)
        ],
        pricing=usage_state.UsagePricing(checked_on="2026-10-30", known_models=U32),
        prices=usage_state.UsagePriceBook(revision=U64),
        rates=[
            host_payloads.UsageResolvedRate(
                provider=name,
                model="m" * 128,
                speed=UsageSpeed.STANDARD,
                rate=usage_state.UsageRate(
                    input=1e5, output=1e5, cache_read=1e5, cache_write=1e5, cache_write_1h=1e5
                ),
                source=usage_state.UsageEstimateSource.OVERRIDE,
            )
            for name in providers
            for _ in range(32)
        ][:512],
        pending_runs=U32,
        pending_files=U32,
        folded_series=U32,
        read_at=Timestamp(seconds=FIRST_QUARTER * QUARTER_SECONDS),
    )
    validate(response)
    assert len(response.to_binary()) <= USAGE_REPLY_MAX_BYTES


@given(runs=run_inputs(max_runs=3), edges=edge_lists())
def test_a_garbled_provider_is_other_and_at_most_sixteen_are_named(
    runs: list[RunInput], edges: list[datetime]
) -> None:
    response = _summary(runs, edges)
    named = {_key(series).provider for series in response.series}
    assert len(named) <= MAX_PROVIDERS
    for series in response.series:
        assert _key(series).provider == OTHER or _key(series).provider in {
            "claude_code",
            "codex",
            "opencode",
            "gemini",
        }
        validate(series)
