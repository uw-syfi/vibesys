"""What tokens would cost at list prices, and the list itself: the Usage screen's view of agentshim.

agentshim owns the price list (its document format, its provider name mapping, the pinned table in
the wheel, the refresh policy) and the cost arithmetic. This sub-facade is the only place VibeSys
code reaches them, and none of agentshim's types cross it: a rate, a count and a cost are the
frozen dataclasses below, and the list's file and fetch are two small roles the host implements.

Nothing here touches a recorded cost. A recorded cost is computed when a turn runs, from the
table pinned in the wheel, and feeds replay and budgets; the figures here are an estimate at
today's list prices, never stored, read only by the Usage screen. The two share no type
(agentshim's ``cost_usd`` takes its pinned ``ModelPricing``, not the ``Rate`` here), and this
module is the one thing that imports the list side, so the split is held by the module graph.

Public surface: ``Rate``, ``ModelRates``, ``Counts``, ``Cost``, ``price_counts``,
``speed_premium``, ``cache_savings``, ``RateStatus``, ``RateDocument``, ``DocumentStore``,
``DocumentFetcher``, ``FetchError``, ``RateBook``, ``book_key``, ``read_rate_book`` and
``refresh_rate_book``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Protocol

from agentshim.core.rates import RateEntry, Rates, candidate_names, normalize_name, price_tokens
from agentshim.core.rates import cache_savings_usd as _cache_savings
from agentshim.core.rates import speed_premium_usd as _speed_premium
from agentshim.core.usage import TokenUsage, UsageSpeed
from agentshim.rates import (
    RateEndpoint,
    RateFetchError,
    RateRead,
    RateSnapshot,
    rates_for,
    read_rates,
    refresh_rates,
)

if TYPE_CHECKING:
    from datetime import date, datetime

SPEEDS: Final = ("standard", "fast", "ultrafast")
"""The speed names a request can run at: ``vibesys.store.v1.UsageSpeed`` without its unreported."""


@dataclass(frozen=True)
class Rate:
    """US dollars per million tokens for one model at one speed. Zero is a price."""

    input_usd: float
    output_usd: float
    cache_read_usd: float
    cache_write_usd: float
    cache_write_1h_usd: float | None = None


@dataclass(frozen=True)
class ModelRates:
    """A model's rates at each speed the list prices it at."""

    name: str
    standard: Rate
    fast: Rate | None = None
    ultrafast: Rate | None = None

    def at(self, speed: str) -> Rate | None:
        """The rate at ``speed``, or ``None`` when the list prices none for it.

        Never another tier's: whether a request at an unlisted speed is billed as standard is
        the caller's decision.
        """
        if speed == "standard":
            return self.standard
        return self.fast if speed == "fast" else self.ultrafast if speed == "ultrafast" else None


@dataclass(frozen=True)
class Counts:
    """The tokens of some turns, as a rollup row holds them.

    ``input`` counts cache reads and writes; reasoning is part of ``output``. A reading that
    breaks those rules (a cache count above the input) is priced as if it held them, since a
    provider's partial report must not make a figure fail.
    """

    input: int
    cache_read: int
    cache_write: int
    cache_write_1h: int
    output: int
    reasoning: int


@dataclass(frozen=True)
class Cost:
    """What tokens cost, split by class of token."""

    input_usd: float
    cache_read_usd: float
    cache_write_usd: float
    output_usd: float

    @property
    def total_usd(self) -> float:
        """All four classes together."""
        return self.input_usd + self.cache_read_usd + self.cache_write_usd + self.output_usd


def price_counts(counts: Counts, rate: Rate) -> Cost:
    """What ``counts`` cost at ``rate``, every token priced once."""
    cost = price_tokens(_usage(counts), _rates(rate))
    return Cost(cost.input_usd, cost.cache_read_usd, cost.cache_write_usd, cost.output_usd)


def speed_premium(counts: Counts, rates: ModelRates, speed: str) -> float:
    """How much more ``counts`` cost at ``speed`` than at the standard tier; zero when unlisted."""
    return _speed_premium(_usage(counts), _entry(rates), UsageSpeed(speed))


def cache_savings(counts: Counts, rate: Rate) -> float:
    """What cache reads saved against paying the input rate for the same tokens."""
    return _cache_savings(_usage(counts), _rates(rate))


class RateStatus(StrEnum):
    """Where the rates of a book came from."""

    FRESH = "fresh"
    CACHED = "cached"
    BUNDLED = "bundled"


@dataclass(frozen=True)
class RateDocument:
    """The last price document fetched, as fetched, and when; what a host keeps between reads."""

    document: bytes | None
    fetched_at: datetime | None
    last_attempt_at: datetime | None


class DocumentStore(Protocol):
    """Where the host keeps its one ``RateDocument``."""

    def read(self) -> RateDocument | None:
        """The stored document, or ``None`` when nothing was stored."""
        ...

    def write(self, stored: RateDocument) -> None:
        """Replace the stored document."""
        ...


class FetchError(Exception):
    """A document could not be fetched: the request failed or timed out."""


class DocumentFetcher(Protocol):
    """Fetches the price document over HTTPS; the host supplies the client."""

    def fetch(self, url: str, accept: str) -> bytes:
        """The body at ``url``, asked for as ``accept``; ``FetchError`` when it cannot be had."""
        ...


class RateBook:
    """The rates a host prices with, and how current they are."""

    def __init__(self, read: RateRead) -> None:
        """Wrap what agentshim read; built by ``read_rate_book`` and ``refresh_rate_book``."""
        self._read = read

    @property
    def status(self) -> RateStatus:
        """Whether the list was fetched lately, is the last good document, or is the pinned table."""
        return RateStatus(self._read.status.value)

    @property
    def fetched_at(self) -> datetime | None:
        """When the document behind the rates was fetched; ``None`` for the pinned table."""
        return self._read.fetched_at

    @property
    def known_models(self) -> int:
        """How many names the book knows."""
        return self._read.known_models

    @property
    def refresh_due(self) -> bool:
        """Whether a refresh would fetch now."""
        return self._read.refresh_due

    @property
    def checked_on(self) -> date:
        """The day the list was read."""
        return self._read.table.checked_on

    def lookup(self, provider: str, model: str) -> ModelRates | None:
        """The rates of ``model`` run through ``provider``, or ``None`` when it has none."""
        entry = rates_for(self._read.table, provider, model)
        return None if entry is None else _model_rates(entry)

    def canonical(self, provider: str, model: str) -> str:
        """The id ``model`` is filed under: the list's name for it, else its trimmed lowercase name.

        A bracketed variant and a date snapshot name the model of the list they extend, so they
        share a price and an override; a name the list does not know stays as it is, lowercase.
        """
        entry = rates_for(self._read.table, provider, model)
        if entry is not None:
            return entry.name
        return book_key(model)


def book_key(name: str) -> str:
    """The id a person's override or ``Map to`` names ``name`` by, whatever provider ran it.

    Trimmed, lowercase, without a bracketed variant (``[1m]``): two spellings that differ only
    so are one model. A book compares every name through this one function, on both ends of a
    ``Map to``, so a chain can only be followed, never missed or looped by a spelling.
    """
    names = candidate_names(name)
    return names[0] if names else normalize_name(name)


def read_rate_book(store: DocumentStore, *, now: datetime) -> RateBook:
    """The best rates available without the network: the stored document over the pinned table."""
    return RateBook(read_rates(_AgentStore(store), now=now))


def refresh_rate_book(
    store: DocumentStore, fetcher: DocumentFetcher, *, now: datetime, force: bool = False
) -> RateBook:
    """Fetch a new document if one is due, and report the rates to use.

    A fetch happens only when the document is older than a day or ``force``, and never within a
    minute of the last attempt, failed or not; a document that does not parse never replaces a
    good one.
    """
    return RateBook(refresh_rates(_AgentStore(store), _AgentSource(fetcher), now=now, force=force))


class _AgentStore:
    """A ``DocumentStore`` as agentshim's store."""

    def __init__(self, store: DocumentStore) -> None:
        self._store = store

    def read(self) -> RateSnapshot | None:
        stored = self._store.read()
        if stored is None:
            return None
        return RateSnapshot(stored.document, stored.fetched_at, stored.last_attempt_at)

    def write(self, snapshot: RateSnapshot) -> None:
        self._store.write(
            RateDocument(snapshot.document, snapshot.fetched_at, snapshot.last_attempt_at)
        )


class _AgentSource:
    """A ``DocumentFetcher`` as agentshim's source."""

    def __init__(self, fetcher: DocumentFetcher) -> None:
        self._fetcher = fetcher

    def fetch(self, endpoint: RateEndpoint) -> bytes:
        try:
            return self._fetcher.fetch(endpoint.url, endpoint.accept)
        except FetchError as error:
            raise RateFetchError(str(error)) from error


def _usage(counts: Counts) -> TokenUsage:
    """``counts`` as the library's tokens, held to its rules by clamping."""
    cache = counts.cache_read + counts.cache_write
    write_1h = min(counts.cache_write_1h, counts.cache_write)
    return TokenUsage(
        input_tokens=max(counts.input, cache),
        output_tokens=counts.output,
        cache_read_input_tokens=counts.cache_read,
        cache_write_input_tokens=counts.cache_write,
        cache_write_1h_input_tokens=write_1h,
        reasoning_output_tokens=min(counts.reasoning, counts.output),
    )


def _rates(rate: Rate) -> Rates:
    return Rates(
        input_usd=rate.input_usd,
        output_usd=rate.output_usd,
        cache_read_usd=rate.cache_read_usd,
        cache_write_usd=rate.cache_write_usd,
        cache_write_1h_usd=rate.cache_write_1h_usd,
    )


def _entry(rates: ModelRates) -> RateEntry:
    return RateEntry(
        name=normalize_name(rates.name),
        standard=_rates(rates.standard),
        fast=None if rates.fast is None else _rates(rates.fast),
        ultrafast=None if rates.ultrafast is None else _rates(rates.ultrafast),
    )


def _rate(rates: Rates) -> Rate:
    return Rate(
        rates.input_usd,
        rates.output_usd,
        rates.cache_read_usd,
        rates.cache_write_usd,
        rates.cache_write_1h_usd,
    )


def _model_rates(entry: RateEntry) -> ModelRates:
    return ModelRates(
        name=entry.name,
        standard=_rate(entry.standard),
        fast=None if entry.fast is None else _rate(entry.fast),
        ultrafast=None if entry.ultrafast is None else _rate(entry.ultrafast),
    )


__all__ = [
    "SPEEDS",
    "Cost",
    "Counts",
    "DocumentFetcher",
    "DocumentStore",
    "FetchError",
    "ModelRates",
    "Rate",
    "RateBook",
    "RateDocument",
    "RateStatus",
    "book_key",
    "cache_savings",
    "price_counts",
    "read_rate_book",
    "refresh_rate_book",
    "speed_premium",
]
