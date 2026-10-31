"""The list-price sub-facade prices any tokens and names models as the list does.

Tokens are drawn from what a provider can report, including reports that break the library's
own rules (a cache count above the input): the facade prices them as if the rules held and never
fails, since a partial report must not make a figure fail. Rates are drawn from the documented
range, zero included.
"""

from __future__ import annotations

import datetime as dt
import math

from hypothesis import given
from hypothesis import strategies as st

from vs_agent.api.usage_pricing import (
    Counts,
    ModelRates,
    Rate,
    RateDocument,
    RateStatus,
    cache_savings,
    price_counts,
    read_rate_book,
    speed_premium,
)

NOW = dt.datetime(2026, 10, 31, 12, tzinfo=dt.UTC)
_COUNT = st.integers(min_value=0, max_value=10**9)
_COUNTS = st.builds(
    Counts,
    input=_COUNT,
    cache_read=_COUNT,
    cache_write=_COUNT,
    cache_write_1h=_COUNT,
    output=_COUNT,
    reasoning=_COUNT,
)
_PRICE = st.floats(min_value=0, max_value=1000, allow_nan=False)
_RATES = st.builds(
    Rate,
    input_usd=_PRICE,
    output_usd=_PRICE,
    cache_read_usd=_PRICE,
    cache_write_usd=_PRICE,
    cache_write_1h_usd=st.none() | _PRICE,
)


class _Empty:
    def read(self) -> RateDocument | None:
        return None

    def write(self, stored: RateDocument) -> None:
        del stored


@given(counts=_COUNTS, rate=_RATES)
def test_any_tokens_at_any_rate_cost_a_non_negative_sum_of_their_classes(
    counts: Counts, rate: Rate
) -> None:
    cost = price_counts(counts, rate)
    assert min(cost.input_usd, cost.cache_read_usd, cost.cache_write_usd, cost.output_usd) >= 0
    assert cost.total_usd == (
        cost.input_usd + cost.cache_read_usd + cost.cache_write_usd + cost.output_usd
    )
    assert cache_savings(counts, rate) >= 0 or rate.input_usd < rate.cache_read_usd


@given(rate=_RATES)
def test_nothing_costs_nothing_and_a_free_model_costs_nothing(rate: Rate) -> None:
    assert price_counts(Counts(0, 0, 0, 0, 0, 0), rate).total_usd == 0
    free = Rate(0, 0, 0, 0)
    assert price_counts(Counts(10**6, 10**5, 10**4, 10**3, 10**5, 10**4), free).total_usd == 0


@given(
    a=_COUNTS,
    b=_COUNTS,
    rate=_RATES,
)
def test_cost_is_additive_in_tokens_that_hold_the_rules(a: Counts, b: Counts, rate: Rate) -> None:
    """Two readings that each hold the library's rules price as their sum does."""

    def held(counts: Counts) -> Counts:
        return Counts(
            input=counts.input + counts.cache_read + counts.cache_write,
            cache_read=counts.cache_read,
            cache_write=counts.cache_write,
            cache_write_1h=min(counts.cache_write_1h, counts.cache_write),
            output=counts.output,
            reasoning=min(counts.reasoning, counts.output),
        )

    a, b = held(a), held(b)
    whole = Counts(
        a.input + b.input,
        a.cache_read + b.cache_read,
        a.cache_write + b.cache_write,
        a.cache_write_1h + b.cache_write_1h,
        a.output + b.output,
        a.reasoning + b.reasoning,
    )
    parts = price_counts(a, rate).total_usd + price_counts(b, rate).total_usd
    assert math.isclose(price_counts(whole, rate).total_usd, parts, rel_tol=1e-9, abs_tol=1e-6)


@given(counts=_COUNTS, standard=_RATES, fast=_RATES)
def test_the_standard_tier_has_no_premium_and_an_unlisted_tier_claims_none(
    counts: Counts, standard: Rate, fast: Rate
) -> None:
    rates = ModelRates("model", standard, fast=fast)
    assert speed_premium(counts, rates, "standard") == 0
    assert speed_premium(counts, rates, "ultrafast") == 0


def test_the_pinned_table_prices_what_the_catalogue_offers_and_names_models_as_the_list_does() -> (
    None
):
    book = read_rate_book(_Empty(), now=NOW)
    assert book.status is RateStatus.BUNDLED
    assert book.known_models > 0
    listed = book.lookup("claude_code", "claude-opus-5-5")
    assert listed is not None
    assert listed.standard.input_usd > 0
    assert book.canonical("claude_code", "Claude-Opus-5-5 ") == "claude-opus-5-5"
    assert book.canonical("claude_code", "claude-opus-5-5[1m]") == book.canonical(
        "claude_code", "claude-opus-5-5"
    )
    assert book.canonical("claude_code", "<Synthetic>") == "<synthetic>"
    assert book.lookup("claude_code", "<synthetic>") is None
    assert book.lookup("claude_code", "no-such-model") is None
