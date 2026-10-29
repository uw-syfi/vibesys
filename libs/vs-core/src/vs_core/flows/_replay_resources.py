"""Pure resource reservations and actual usage, projected from immutable starts."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Literal

from vs_proto.api import Change, Record, Span, replay

from ._replay_accounting import AccountingAmount, AccountingLedger
from ._replay_sources import ReplayError

type ResourceDimension = Literal["money_usd", "input_tokens", "output_tokens"]
DIMENSIONS: tuple[ResourceDimension, ...] = ("money_usd", "input_tokens", "output_tokens")

_RESOURCES: dict[replay.AccountingDimension, ResourceDimension] = {
    replay.AccountingDimension.MONEY_USD: "money_usd",
    replay.AccountingDimension.INPUT_TOKENS: "input_tokens",
    replay.AccountingDimension.CACHE_READ_TOKENS: "input_tokens",
    replay.AccountingDimension.CACHE_CREATION_TOKENS: "input_tokens",
    replay.AccountingDimension.OUTPUT_TOKENS: "output_tokens",
}
"""The dimension registry: input includes both cache kinds; reasoning is not output."""


@dataclass(frozen=True)
class ReplayResourcesBlocked:
    """A paid frontier request cannot fit its declared upper bound in this epoch."""

    dimension: ResourceDimension


@dataclass(frozen=True)
class ReplayResourceTotals:
    """Reported usage only; absent dimensions remain visible in the native observation."""

    money_usd: float = 0
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class ReplayResourceAccounting:
    """Actual known totals plus conservative current-epoch admission debits.

    Pending invocations retain their full reservation. A paid result with
    unreported usage also retains the relevant reservation, never a guessed
    zero. Historical attempts and imported history, including late readings of
    imported obligations, do not consume a fresh epoch's resource budget.
    ``lifetime`` is the one coverage-aware fold of native and imported
    evidence; ``reported`` and ``epoch_reported`` are its known parts.
    """

    reported: ReplayResourceTotals
    epoch_reported: ReplayResourceTotals
    committed: ReplayResourceTotals
    lifetime: dict[replay.AccountingDimension, AccountingAmount]

    def decide(
        self, operation: replay.OperationReplay, limits: replay.ReplayResources | None
    ) -> ReplayResourcesBlocked | None:
        if limits is None:
            return None
        policy = operation.policy
        reservation = None if policy is None else policy.reservation
        for dimension in DIMENSIONS:
            if not limits.has_field(dimension):
                continue
            if reservation is None or not reservation.has_field(dimension):
                problem = f"bounded replay request requires policy.reservation.{dimension}"
                raise ReplayError(problem)
            if getattr(self.committed, dimension) + getattr(reservation, dimension) > getattr(
                limits, dimension
            ):
                return ReplayResourcesBlocked(dimension)
        return None

    @classmethod
    def from_records(cls, records: tuple[Record, ...], *, epoch: int) -> ReplayResourceAccounting:
        ledger = AccountingLedger.from_records(records)
        lifetime = ledger.totals()
        return cls(
            _known(lifetime),
            _known(ledger.totals(origin_epoch=epoch)),
            _debits(records, epoch),
            lifetime,
        )


def _known(totals: dict[replay.AccountingDimension, AccountingAmount]) -> ReplayResourceTotals:
    values: dict[ResourceDimension, Fraction] = dict.fromkeys(DIMENSIONS, Fraction(0))
    for dimension, amount in totals.items():
        if (resource := _RESOURCES.get(dimension)) is not None:
            values[resource] += amount.known
    return ReplayResourceTotals(
        money_usd=float(values["money_usd"]),
        input_tokens=int(values["input_tokens"]),
        output_tokens=int(values["output_tokens"]),
    )


def _debits(records: tuple[Record, ...], epoch: int) -> ReplayResourceTotals:
    """Conservative admission debits of spans started in this epoch."""
    spans: dict[str, tuple[int, Span]] = {}
    for record in records:
        if record.body is None or record.body.field != "span":
            continue
        change = record.body.value
        span = change.span
        if span is None or span.replay is None:
            continue
        if change.change == Change.STARTED:
            spans[span.id] = (record.epoch, span)
        elif span.id in spans:
            spans[span.id] = (spans[span.id][0], span)
    parents = {
        span.replay_physical.logical
        for _, span in spans.values()
        if span.replay_physical is not None
    }
    committed = ReplayResourceTotals()
    for started_epoch, span in spans.values():
        if span.id not in parents and started_epoch == epoch:
            committed = _add(committed, _debit(span))
    return committed


def _add(left: ReplayResourceTotals, right: ReplayResourceTotals) -> ReplayResourceTotals:
    return ReplayResourceTotals(
        money_usd=left.money_usd + right.money_usd,
        input_tokens=left.input_tokens + right.input_tokens,
        output_tokens=left.output_tokens + right.output_tokens,
    )


def _debit(span: Span) -> ReplayResourceTotals:
    operation, observed = span.replay, span.replay_observation
    reservation = (
        None if operation is None or operation.policy is None else operation.policy.reservation
    )
    usage = None if observed is None else observed.usage
    cost = observed.cost_usd if observed is not None and observed.has_field("cost_usd") else 0.0
    inputs = 0 if usage is None else usage.input + usage.cache_read + usage.cache_creation
    outputs = 0 if usage is None else usage.output
    known = ReplayResourceTotals(cost, inputs, outputs)
    if reservation is None:
        return known
    money_known = observed is not None and (observed.has_field("cost_usd") or not observed.paid)
    inputs_known = usage is not None and all(
        usage.has_field(name) for name in ("input", "cache_read", "cache_creation")
    )
    output_known = usage is not None and usage.has_field("output")
    return ReplayResourceTotals(
        money_usd=cost if money_known else max(cost, reservation.money_usd),
        input_tokens=inputs if inputs_known else max(inputs, reservation.input_tokens),
        output_tokens=outputs if output_known else max(outputs, reservation.output_tokens),
    )
