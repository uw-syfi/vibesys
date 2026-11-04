"""The estimate at today's list prices never reaches a recorded cost.

A recorded cost is computed when a turn runs, from the table pinned in the agentshim wheel, and it
feeds replay and budgets. The Usage screen's estimate is computed at read time from a price list
a host fetches. This poisons every input of the second (a fetched document with absurd rates at
every speed, stored where the host would keep it) and checks the first does not move, for every
model the pinned table prices.
"""

from __future__ import annotations

import datetime as dt
import json

import agentshim
from agentshim.pricing import cost_usd, price_for

from vs_agent.api.usage_pricing import (
    Counts,
    FetchError,
    RateDocument,
    price_counts,
    read_rate_book,
    refresh_rate_book,
)

NOW = dt.datetime(2026, 10, 31, 12, tzinfo=dt.UTC)
MODELS = (
    ("anthropic", "claude_code", "claude-opus-5-5"),
    ("anthropic", "claude_code", "claude-haiku-5-5"),
    ("openai", "codex", "gpt-6.1-sol"),
)
USAGE = agentshim.TokenUsage(
    input_tokens=1_000_000,
    output_tokens=100_000,
    cache_read_input_tokens=200_000,
    cache_write_input_tokens=50_000,
)


class _Held:
    def __init__(self) -> None:
        self.stored: RateDocument | None = None

    def read(self) -> RateDocument | None:
        return self.stored

    def write(self, stored: RateDocument) -> None:
        self.stored = stored


class _Absurd:
    """A network that serves a price list pricing every model at a dollar a token."""

    def __init__(self) -> None:
        self.requests = 0

    def fetch(self, url: str, accept: str) -> bytes:
        del url, accept
        self.requests += 1
        return json.dumps(
            {
                model: {
                    "input_cost_per_token": 1.0,
                    "output_cost_per_token": 1.0,
                    "cache_read_input_token_cost": 1.0,
                    "cache_creation_input_token_cost": 1.0,
                    "input_cost_per_token_priority": 2.0,
                    "output_cost_per_token_priority": 2.0,
                }
                for _, _, model in MODELS
            }
        ).encode()


class _Down:
    def fetch(self, url: str, accept: str) -> bytes:
        del url, accept
        message = "down"
        raise FetchError(message)


def _recorded() -> dict[str, float]:
    found: dict[str, float] = {}
    for vendor, _provider, model in MODELS:
        pricing = price_for(vendor, model)
        assert pricing is not None
        found[model] = cost_usd(USAGE, pricing)
    return found


def test_a_poisoned_price_list_moves_the_estimate_and_not_the_recorded_cost() -> None:
    before = _recorded()
    store = _Held()
    network = _Absurd()
    book = refresh_rate_book(store, network, now=NOW, force=True)
    assert network.requests == 1
    assert store.stored is not None
    for _, provider, model in MODELS:
        listed = book.lookup(provider, model)
        assert listed is not None
        assert listed.standard.input_usd == 1_000_000.0
        estimate = price_counts(Counts(1_000_000, 200_000, 50_000, 0, 100_000, 0), listed.standard)
        assert estimate.total_usd > 1e5
    assert _recorded() == before
    assert price_for("anthropic", "claude-opus-5-5") is not None


def test_a_down_network_and_a_damaged_document_leave_the_pinned_table_priced() -> None:
    before = _recorded()
    store = _Held()
    book = refresh_rate_book(store, _Down(), now=NOW, force=True)
    assert book.lookup("claude_code", "claude-opus-5-5") is not None
    store.stored = RateDocument(b"\xff\xfe not json", NOW, NOW)
    book = read_rate_book(store, now=NOW)
    assert book.lookup("claude_code", "claude-opus-5-5") is not None
    assert _recorded() == before
