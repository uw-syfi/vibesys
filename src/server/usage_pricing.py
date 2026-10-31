"""What a row's tokens would cost: the estimate order, over the host's rates and the person's prices.

``ListPricing`` prices a rollup row (``server.usage_rollup``) for the summary. The order is the
same for every provider and branches on how the row's cost was reached, never on which provider
ran it:

1. A price the person set for the model overrides everything and prices the tokens, at every
   speed.
2. A turn whose provider reported its cost (``PROVIDER_REPORTED``) keeps that cost. When the list
   also has a rate, the cost's split by kind of token is the rate's split scaled to the reported
   total.
3. A turn recorded before its basis existed (``LEGACY_RECORDED``) keeps its recorded cost,
   marked approximate.
4. A rate in the list for the model at the row's speed prices the tokens. A model the list prices
   only at the standard tier is priced there, with no speed premium, because the list names no
   other.
5. Otherwise the row is unpriced: no figure is made up.

A person's ``Map to`` applies first: it renames a model to the one whose price it should have, and
chains are followed to their end. Names are compared as ``book_key`` makes them (trimmed, lowercase,
no bracketed variant) on both ends of every ``Map to``, so ``x -> x[1m]`` is a model mapped to itself.
The book is a graph on those keys and must be acyclic: ``check_book`` refuses a book that is not, on
every save and again whenever one is loaded (``ListPricing`` will not be built over a book that
fails it). Following a chain is bounded by the number of entries besides, so no book, however it
came to be, can make a read loop.

The estimate is never stored and never reaches a recorded cost: the list and the arithmetic come
from ``vs_agent.api.usage_pricing``, the one door to them, whose types are not the ones a recorded
cost is computed from.

Light: ``vs_proto``, ``server.usage_summary`` and the sub-facade, which is imported on the first
usage read and not at ``hello``.

Public surface: ``ListPricing``, ``Rates``, ``PriceBookError``, ``PriceBookUnreadableError``, ``check_book``, ``same_book``, ``saved_book`` and,
re-exported from the sub-facade so one module holds the edge, ``RateBook``, ``RateDocument``,
``DocumentStore``, ``DocumentFetcher``, ``FetchError``, ``book_key``, ``read_rate_book`` and
``refresh_rate_book``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol

from server.usage_summary import Priced
from vs_agent.api import usage_pricing as lists
from vs_agent.api.usage_pricing import (
    DocumentFetcher,
    DocumentStore,
    FetchError,
    RateBook,
    RateDocument,
    book_key,
    read_rate_book,
    refresh_rate_book,
)
from vs_proto.api import Timestamp, UsageCostBasis, UsageSpeed, usage_state

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import date, datetime

_SPEEDS: Final = {
    UsageSpeed.STANDARD: "standard",
    UsageSpeed.FAST: "fast",
    UsageSpeed.ULTRAFAST: "ultrafast",
}
_SOURCE: Final = usage_state.UsageEstimateSource
_PRICE_STATUS: Final = {
    lists.RateStatus.FRESH: usage_state.UsagePricingStatus.FRESH,
    lists.RateStatus.CACHED: usage_state.UsagePricingStatus.CACHED,
    lists.RateStatus.BUNDLED: usage_state.UsagePricingStatus.BUNDLED,
}
_UNPRICED_NAMES: Final = "unknown"


class PriceBookError(ValueError):
    """A price book that cannot be saved; the message names the offending key."""


class PriceBookUnreadableError(PriceBookError):
    """The stored price book exists and cannot be used: it is damaged, or it could not be followed."""


def check_book(
    overrides: Sequence[usage_state.UsageRateOverride], aliases: Sequence[usage_state.UsageAlias]
) -> None:
    """Refuse a book with a repeated key, a model mapped to itself, or a chain that loops.

    Every name is compared as ``book_key`` makes it, which is also how pricing follows a chain,
    so what is accepted here is exactly what pricing can follow to an end. The check is over the
    whole book as it will be saved, so a batch that closes a loop between its own entries is
    refused, and the message names the offending entry as typed.
    """
    seen: set[str] = set()
    for override in overrides:
        key = book_key(override.model)
        if key in seen:
            message = f"overrides: {override.model!r} is set twice"
            raise PriceBookError(message)
        seen.add(key)
    mapped: dict[str, str] = {}
    spelled: dict[str, str] = {}
    for alias in aliases:
        source, target = book_key(alias.from_), book_key(alias.to)
        if source in mapped:
            message = f"aliases: {alias.from_!r} is mapped twice"
            raise PriceBookError(message)
        if source == target:
            message = f"aliases: {alias.from_!r} maps to itself"
            raise PriceBookError(message)
        mapped[source] = target
        spelled[source] = alias.from_
    for start in mapped:
        found = _loop_from(start, mapped)
        if found is not None:
            message = f"aliases: {spelled[start]!r} {found}"
            raise PriceBookError(message)


def _loop_from(start: str, mapped: Mapping[str, str]) -> str | None:
    """What is wrong with the chain that ``start`` begins, or ``None`` when it ends.

    A chain has at most one step per entry, so one that is still going after that many steps has
    repeated a name.
    """
    step = mapped[start]
    for _ in range(len(mapped)):
        if step == start:
            return "maps back to itself"
        following = mapped.get(step)
        if following is None:
            return None
        step = following
    return "leads into a loop"


def same_book(
    book: usage_state.UsagePriceBook,
    overrides: Sequence[usage_state.UsageRateOverride],
    aliases: Sequence[usage_state.UsageAlias],
) -> bool:
    """Whether ``overrides`` and ``aliases`` say what ``book`` already says, in any order."""
    return _content(book.overrides, book.aliases) == _content(overrides, aliases)


def saved_book(
    book: usage_state.UsagePriceBook,
    overrides: Sequence[usage_state.UsageRateOverride],
    aliases: Sequence[usage_state.UsageAlias],
) -> usage_state.UsagePriceBook:
    """The book after saving ``overrides`` and ``aliases`` over ``book``: their content, next revision."""
    check_book(overrides, aliases)
    return usage_state.UsagePriceBook(
        revision=book.revision + 1,
        overrides=sorted(
            (
                usage_state.UsageRateOverride(model=book_key(each.model), rate=each.rate)
                for each in overrides
            ),
            key=lambda each: each.model,
        ),
        aliases=sorted(
            (
                usage_state.UsageAlias(from_=book_key(each.from_), to=book_key(each.to))
                for each in aliases
            ),
            key=lambda each: each.from_,
        ),
    )


def _content(
    overrides: Sequence[usage_state.UsageRateOverride], aliases: Sequence[usage_state.UsageAlias]
) -> tuple[list[tuple[str, bytes]], list[tuple[str, str]]]:
    return (
        sorted((book_key(each.model), each.rate.to_binary()) for each in overrides if each.rate),
        sorted((book_key(each.from_), book_key(each.to)) for each in aliases),
    )


class Rates(Protocol):
    """The list prices a host holds, as pricing reads them: ``vs_agent``'s rate book, or a double."""

    @property
    def status(self) -> lists.RateStatus:
        """Whether the list was fetched lately, is the last good document, or is the pinned table."""
        ...

    @property
    def fetched_at(self) -> datetime | None:
        """When the document behind the rates was fetched."""
        ...

    @property
    def known_models(self) -> int:
        """How many names the list knows."""
        ...

    @property
    def refresh_due(self) -> bool:
        """Whether a refresh would fetch now."""
        ...

    @property
    def checked_on(self) -> date:
        """The day the list was read."""
        ...

    def lookup(self, provider: str, model: str) -> lists.ModelRates | None:
        """The rates of ``model`` run through ``provider``, or ``None`` when it has none."""
        ...

    def canonical(self, provider: str, model: str) -> str:
        """The id ``model`` is filed under."""
        ...


@dataclass(frozen=True)
class _Resolved:
    model: str
    """The id the model's price is looked up and overridden under, after ``Map to``."""
    listed: lists.ModelRates | None
    override: lists.Rate | None


class ListPricing:
    """Today's list prices and the person's book, as the summary prices rows with them."""

    def __init__(
        self,
        rates: Rates,
        book: usage_state.UsagePriceBook,
        *,
        status: usage_state.UsagePricesStatus = usage_state.UsagePricesStatus.OK,
    ) -> None:
        """Price over ``rates`` and the person's ``book``; ``status`` says how the book was read.

        Raises ``PriceBookError`` when ``book`` repeats a key or its ``Map to`` chains loop, so a
        book that could not be followed to an end is never priced over.
        """
        check_book(book.overrides, book.aliases)
        self._rates = rates
        self._book = book
        self._status = status
        self._aliases: Mapping[str, str] = {
            book_key(each.from_): book_key(each.to) for each in book.aliases
        }
        self._overrides: Mapping[str, lists.Rate] = {
            book_key(each.model): _rate_of(each.rate) for each in book.overrides if each.rate
        }
        self._resolved: dict[tuple[str, str], _Resolved] = {}

    def price(self, provider: str, row: usage_state.UsageRollupRow) -> Priced:
        """The estimate for ``row`` in the order: override, reported, recorded, model, unpriced."""
        resolved = self._resolve(provider, row.model)
        counts = lists.Counts(
            row.input,
            row.cache_read,
            row.cache_write,
            row.cache_write_1h,
            row.output,
            row.reasoning,
        )
        speed = _SPEEDS.get(row.speed, "standard")
        if resolved.override is not None:
            return _priced(resolved.model, _SOURCE.OVERRIDE, counts, resolved.override, 0.0)
        listed = resolved.listed
        rate = None if listed is None else (listed.at(speed) or listed.standard)
        if row.has_field("recorded_usd") and row.basis == UsageCostBasis.PROVIDER_REPORTED:
            return _kept(resolved.model, _SOURCE.REPORTED, row.recorded_usd, counts, rate)
        if row.has_field("recorded_usd") and row.basis == UsageCostBasis.LEGACY_RECORDED:
            return _kept(resolved.model, _SOURCE.RECORDED, row.recorded_usd, counts, None)
        if listed is None or rate is None:
            return Priced(resolved.model, _SOURCE.UNPRICED)
        return _priced(
            resolved.model, _SOURCE.MODEL, counts, rate, lists.speed_premium(counts, listed, speed)
        )

    def resolved(
        self, provider: str, model: str, speed: UsageSpeed
    ) -> tuple[usage_state.UsageRate | None, usage_state.UsageEstimateSource]:
        """The rate ``model`` (an id ``price`` returned) is priced at for ``speed``, and its source."""
        resolved = self._resolve(provider, model)
        if resolved.override is not None:
            return _wire_rate(resolved.override), _SOURCE.OVERRIDE
        listed = resolved.listed
        if listed is None:
            return None, _SOURCE.UNPRICED
        rate = listed.at(_SPEEDS.get(speed, "standard")) or listed.standard
        return _wire_rate(rate), _SOURCE.MODEL

    @property
    def pricing(self) -> usage_state.UsagePricing:
        """How current the list is."""
        rates = self._rates
        pricing = usage_state.UsagePricing(
            status=_PRICE_STATUS[rates.status],
            known_models=rates.known_models,
            refresh_due=rates.refresh_due,
            checked_on=rates.checked_on.isoformat(),
        )
        if rates.fetched_at is not None:
            pricing.fetched_at = Timestamp.from_datetime(rates.fetched_at)
        return pricing

    @property
    def book(self) -> usage_state.UsagePriceBook:
        """What the person set."""
        return self._book

    @property
    def prices_status(self) -> usage_state.UsagePricesStatus:
        """Whether ``book`` is what the person set, or the host could not read it."""
        return self._status

    def _resolve(self, provider: str, model: str) -> _Resolved:
        """The id ``model`` is priced under after ``Map to``, with its list rates and override.

        The chain is followed in ``book_key`` space for at most as many steps as the book has
        entries, which ends it whatever the book holds.
        """
        key = (provider, model)
        found = self._resolved.get(key)
        if found is None:
            name = book_key(model)
            for _ in range(len(self._aliases)):
                target = self._aliases.get(name)
                if target is None:
                    break
                name = target
            named = model if name == book_key(model) else name
            final = self._rates.canonical(provider, named) or _UNPRICED_NAMES
            found = _Resolved(
                final, self._rates.lookup(provider, final), self._overrides.get(book_key(final))
            )
            self._resolved[key] = found
        return found


def _priced(
    model: str,
    source: usage_state.UsageEstimateSource,
    counts: lists.Counts,
    rate: lists.Rate,
    premium: float,
) -> Priced:
    cost = lists.price_counts(counts, rate)
    return Priced(
        model=model,
        source=source,
        estimate_usd=cost.total_usd,
        input_usd=cost.input_usd,
        cache_read_usd=cost.cache_read_usd,
        cache_write_usd=cost.cache_write_usd,
        output_usd=cost.output_usd,
        speed_premium_usd=premium,
        cache_savings_usd=lists.cache_savings(counts, rate),
    )


def _kept(
    model: str,
    source: usage_state.UsageEstimateSource,
    cost: float,
    counts: lists.Counts,
    rate: lists.Rate | None,
) -> Priced:
    """A cost already known: the estimate, split by kind of token as ``rate`` would, if there is one."""
    if rate is None:
        return Priced(model=model, source=source, estimate_usd=cost)
    split = lists.price_counts(counts, rate)
    scale = 0.0 if split.total_usd <= 0 else cost / split.total_usd
    return Priced(
        model=model,
        source=source,
        estimate_usd=cost,
        input_usd=split.input_usd * scale,
        cache_read_usd=split.cache_read_usd * scale,
        cache_write_usd=split.cache_write_usd * scale,
        output_usd=split.output_usd * scale,
        cache_savings_usd=lists.cache_savings(counts, rate),
    )


def _rate_of(rate: usage_state.UsageRate | None) -> lists.Rate:
    if rate is None:
        message = "a price override without a rate"
        raise PriceBookError(message)
    return lists.Rate(
        rate.input,
        rate.output,
        rate.cache_read,
        rate.cache_write,
        rate.cache_write_1h if rate.has_field("cache_write_1h") else None,
    )


def _wire_rate(rate: lists.Rate) -> usage_state.UsageRate:
    wire = usage_state.UsageRate(
        input=rate.input_usd,
        output=rate.output_usd,
        cache_read=rate.cache_read_usd,
        cache_write=rate.cache_write_usd,
    )
    if rate.cache_write_1h_usd is not None:
        wire.cache_write_1h = rate.cache_write_1h_usd
    return wire


__all__ = [
    "DocumentFetcher",
    "DocumentStore",
    "FetchError",
    "ListPricing",
    "PriceBookError",
    "PriceBookUnreadableError",
    "RateBook",
    "RateDocument",
    "Rates",
    "book_key",
    "check_book",
    "read_rate_book",
    "refresh_rate_book",
    "same_book",
    "saved_book",
]
