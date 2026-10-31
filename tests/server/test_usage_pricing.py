"""A row is priced in one fixed order, by how its cost was reached, over today's list and the person's book.

The list is a real one parsed from a document the test writes (the facade's own reader), so the
estimate order is checked over the arithmetic production uses. Rows come from the contract
(``tests/server/usage_support.py``), and so do the price books: any keys a person can type, any
chain of ``Map to`` the proto allows, including ones that loop.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from collections import defaultdict

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from tests.server.usage_support import FIRST_QUARTER, rollup_rows

from server.usage_pricing import (
    ListPricing,
    PriceBookError,
    RateDocument,
    check_book,
    read_rate_book,
    same_book,
    saved_book,
)
from vs_proto.api import UsageCostBasis, UsageSpeed, usage_state

NOW = dt.datetime(2026, 10, 31, 12, tzinfo=dt.UTC)
SOURCE = usage_state.UsageEstimateSource


class _Held:
    """A document store holding what it was given: the host's file, in memory."""

    def __init__(self, document: bytes | None) -> None:
        self.document = document

    def read(self) -> RateDocument | None:
        if self.document is None:
            return None
        return RateDocument(self.document, NOW, NOW)

    def write(self, stored: RateDocument) -> None:
        self.document = stored.document


def _list() -> bytes:
    """A price document: one model with a fast tier, one free, one partly priced."""
    return json.dumps(
        {
            "tier-model": {
                "input_cost_per_token": 5e-6,
                "output_cost_per_token": 25e-6,
                "cache_read_input_token_cost": 5e-7,
                "cache_creation_input_token_cost": 6.25e-6,
                "input_cost_per_token_priority": 1e-5,
                "output_cost_per_token_priority": 5e-5,
            },
            "free-model": {"input_cost_per_token": 0, "output_cost_per_token": 0},
        }
    ).encode()


def _pricing(book: usage_state.UsagePriceBook | None = None) -> ListPricing:
    rates = read_rate_book(_Held(_list()), now=NOW)
    return ListPricing(rates, book or usage_state.UsagePriceBook())


def _row(**fields: object) -> usage_state.UsageRollupRow:
    row = usage_state.UsageRollupRow(
        quarter=FIRST_QUARTER,
        provider="claude_code",
        model="tier-model",
        speed=UsageSpeed.STANDARD,
        basis=UsageCostBasis.PRICE_TABLE,
        input=1_000_000,
        output=100_000,
        turns=1,
    )
    for name, value in fields.items():
        setattr(row, name, value)
    return row


def _book(
    overrides: dict[str, float] | None = None, aliases: dict[str, str] | None = None
) -> usage_state.UsagePriceBook:
    return usage_state.UsagePriceBook(
        revision=1,
        overrides=[
            usage_state.UsageRateOverride(
                model=model,
                rate=usage_state.UsageRate(
                    input=price, output=price, cache_read=price, cache_write=price
                ),
            )
            for model, price in (overrides or {}).items()
        ],
        aliases=[usage_state.UsageAlias(from_=a, to=b) for a, b in (aliases or {}).items()],
    )


def test_a_listed_model_is_priced_by_the_list() -> None:
    priced = _pricing().price("claude_code", _row())
    assert priced.source is SOURCE.MODEL
    assert math.isclose(priced.estimate_usd, 5 + 2.5)
    assert math.isclose(
        priced.estimate_usd,
        priced.input_usd + priced.cache_read_usd + priced.cache_write_usd + priced.output_usd,
    )


def test_the_fast_tier_costs_its_rate_and_the_premium_says_how_much_more() -> None:
    standard = _pricing().price("claude_code", _row())
    fast = _pricing().price("claude_code", _row(speed=UsageSpeed.FAST))
    assert math.isclose(fast.estimate_usd, 2 * standard.estimate_usd)
    assert math.isclose(fast.speed_premium_usd, standard.estimate_usd)
    assert standard.speed_premium_usd == 0


def test_a_model_the_list_prices_at_one_speed_is_priced_there_without_a_premium() -> None:
    priced = _pricing().price("claude_code", _row(model="free-model", speed=UsageSpeed.ULTRAFAST))
    assert priced.source is SOURCE.MODEL
    assert priced.estimate_usd == 0
    assert priced.speed_premium_usd == 0


def test_a_model_the_list_does_not_know_is_unpriced_not_guessed() -> None:
    priced = _pricing().price("claude_code", _row(model="mystery"))
    assert (priced.source, priced.estimate_usd) == (SOURCE.UNPRICED, 0)


@pytest.mark.parametrize(
    ("basis", "source"),
    [
        (UsageCostBasis.PROVIDER_REPORTED, SOURCE.REPORTED),
        (UsageCostBasis.LEGACY_RECORDED, SOURCE.RECORDED),
    ],
)
def test_a_cost_already_known_is_kept_and_the_list_does_not_replace_it(
    basis: UsageCostBasis, source: usage_state.UsageEstimateSource
) -> None:
    priced = _pricing().price("claude_code", _row(basis=basis, recorded_usd=1.25))
    assert (priced.source, priced.estimate_usd) == (source, 1.25)


def test_a_reported_cost_is_split_by_the_rate_scaled_to_the_total() -> None:
    priced = _pricing().price(
        "claude_code", _row(basis=UsageCostBasis.PROVIDER_REPORTED, recorded_usd=3.0)
    )
    parts = priced.input_usd + priced.cache_read_usd + priced.cache_write_usd + priced.output_usd
    assert math.isclose(parts, 3.0)
    assert math.isclose(priced.input_usd / priced.output_usd, 5 / 2.5)


def test_an_override_prices_every_basis_and_every_speed_and_zero_is_a_price() -> None:
    pricing = _pricing(_book({"tier-model": 0.0}))
    for basis in (
        UsageCostBasis.PROVIDER_REPORTED,
        UsageCostBasis.LEGACY_RECORDED,
        UsageCostBasis.PRICE_TABLE,
    ):
        for speed in (UsageSpeed.STANDARD, UsageSpeed.FAST):
            priced = pricing.price("claude_code", _row(basis=basis, speed=speed, recorded_usd=9.0))
            assert (priced.source, priced.estimate_usd) == (SOURCE.OVERRIDE, 0)


def test_a_row_with_no_recorded_cost_that_claims_a_reported_basis_is_priced_by_the_list() -> None:
    """The basis names how the cost was reached; with no cost to keep, the list prices it."""
    priced = _pricing().price("claude_code", _row(basis=UsageCostBasis.PROVIDER_REPORTED))
    assert priced.source is SOURCE.MODEL


def test_map_to_prices_a_model_as_another_and_keys_the_series_by_the_target() -> None:
    pricing = _pricing(_book(aliases={"my-own-model": "tier-model"}))
    priced = pricing.price("claude_code", _row(model="My-Own-Model"))
    assert (priced.model, priced.source) == ("tier-model", SOURCE.MODEL)
    assert math.isclose(priced.estimate_usd, 7.5)


def test_an_override_binds_to_the_id_after_map_to() -> None:
    pricing = _pricing(_book(overrides={"tier-model": 1.0}, aliases={"mine": "tier-model"}))
    assert pricing.price("claude_code", _row(model="mine")).source is SOURCE.OVERRIDE


_NAMES = st.sampled_from(["a", "b", "c", "d", "tier-model", "free-model", " A ", "B"])


@given(
    mapping=st.dictionaries(_NAMES, _NAMES, max_size=6),
)
@example(mapping={"a": "b", "b": "a"})
@example(mapping={"a": "a"})
def test_a_book_is_accepted_exactly_when_no_chain_of_map_to_loops(mapping: dict[str, str]) -> None:
    """The oracle is a plain walk of each chain over the normalized names, written here."""
    normalized: dict[str, str] = {}
    for source, target in mapping.items():
        normalized.setdefault(source.strip().lower(), target.strip().lower())
    loops = False
    for start, first in normalized.items():
        seen = {start}
        step = first
        while step in normalized and step not in seen:
            seen.add(step)
            step = normalized[step]
        loops = loops or step in seen
    aliases = [usage_state.UsageAlias(from_=a, to=b) for a, b in mapping.items()]
    # Distinct after normalization is a separate rule: keep only the first of each.
    firsts = {}
    for alias in aliases:
        firsts.setdefault(alias.from_.strip().lower(), alias)
    aliases = list(firsts.values())
    if loops:
        with pytest.raises(PriceBookError, match="aliases"):
            check_book([], aliases)
    else:
        check_book([], aliases)


@given(mapping=st.dictionaries(_NAMES, _NAMES, max_size=6), row=rollup_rows())
def test_pricing_a_stored_book_that_loops_still_ends(
    mapping: dict[str, str], row: usage_state.UsageRollupRow
) -> None:
    """A book written by something else may loop; following it stops at the first name that repeats."""
    book = usage_state.UsagePriceBook(
        aliases=[usage_state.UsageAlias(from_=a, to=b) for a, b in mapping.items()]
    )
    priced = _pricing(book).price(row.provider, row)
    assert priced.estimate_usd >= 0


def test_a_model_or_a_source_named_twice_is_refused_naming_the_key() -> None:
    rate = usage_state.UsageRate()
    with pytest.raises(PriceBookError, match="'X'"):
        check_book(
            [
                usage_state.UsageRateOverride(model="x", rate=rate),
                usage_state.UsageRateOverride(model="X", rate=rate),
            ],
            [],
        )
    with pytest.raises(PriceBookError, match="'A'"):
        check_book(
            [],
            [usage_state.UsageAlias(from_="a", to="b"), usage_state.UsageAlias(from_="A", to="c")],
        )


@given(
    rows=st.lists(rollup_rows(quarters=0), min_size=2, max_size=6),
    speed=st.sampled_from([UsageSpeed.STANDARD, UsageSpeed.FAST, UsageSpeed.ULTRAFAST]),
    overridden=st.booleans(),
)
def test_pricing_a_sum_of_rows_is_the_sum_of_pricing_each(
    rows: list[usage_state.UsageRollupRow], speed: UsageSpeed, *, overridden: bool
) -> None:
    """Rows that share a model, speed and basis add: a rollup row stands for all its turns.

    The cost of a priced row is linear in its tokens, the property a rollup relies on. Cache
    counts above the input are the contract's to allow; the rows here keep input above them.
    """
    pricing = _pricing(_book({"tier-model": 3.0}) if overridden else None)
    group = []
    for row in rows:
        row.model, row.provider, row.speed, row.basis = (
            "tier-model",
            "claude_code",
            speed,
            UsageCostBasis.PRICE_TABLE,
        )
        row.input = row.input + row.cache_read + row.cache_write
        row.cache_write_1h = min(row.cache_write_1h, row.cache_write)
        row.reasoning = min(row.reasoning, row.output)
        group.append(row)
    summed = usage_state.UsageRollupRow(
        quarter=group[0].quarter,
        provider="claude_code",
        model="tier-model",
        speed=speed,
        basis=UsageCostBasis.PRICE_TABLE,
        input=sum(r.input for r in group),
        cache_read=sum(r.cache_read for r in group),
        cache_write=sum(r.cache_write for r in group),
        cache_write_1h=sum(r.cache_write_1h for r in group),
        output=sum(r.output for r in group),
        reasoning=sum(r.reasoning for r in group),
    )
    each = defaultdict(float)
    for row in group:
        priced = pricing.price("claude_code", row)
        each["estimate"] += priced.estimate_usd
        each["premium"] += priced.speed_premium_usd
    whole = pricing.price("claude_code", summed)
    assert math.isclose(whole.estimate_usd, each["estimate"], rel_tol=1e-9, abs_tol=1e-9)
    assert math.isclose(whole.speed_premium_usd, each["premium"], rel_tol=1e-9, abs_tol=1e-9)


# --- The book a person saves ---


def test_saving_the_book_a_host_holds_changes_nothing() -> None:
    held = saved_book(
        usage_state.UsagePriceBook(),
        [usage_state.UsageRateOverride(model=" X ", rate=usage_state.UsageRate(input=1))],
        [],
    )
    assert held.revision == 1
    assert [each.model for each in held.overrides] == ["x"]
    assert same_book(
        held, [usage_state.UsageRateOverride(model="x", rate=usage_state.UsageRate(input=1))], []
    )
    assert not same_book(held, [], [])
