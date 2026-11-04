"""Run-level policy: which candidate wins and how the run ends.

The winner is the best winner-eligible retained candidate by the configured metric space, else
the trusted baseline. The search names it with `ctx.choose`; core adopts it.

A run ends in one of three typed ways, so that success always means the operator has a trusted,
measured result to use:

- ``adopted``: a retained, accuracy-verified candidate beat the input (success);
- ``no improvement``: no candidate did, but the input was measured and trusted, so the operator
  keeps it (success);
- ``no trusted result``: the input failed its measurement and no trusted candidate was found,
  so there is nothing to keep (failure).

The search decides *when* the run ends (it knows what is in flight); this module decides what is
chosen and what reason a failed run carries.
"""

from dataclasses import dataclass

from vibesys.candidates._settlement import measurement
from vibesys.candidates._winner import Unranked, best_by
from vibesys.hypothesis import RunEnding
from vibesys.metrics import MetricRow, MetricSpace
from vs_core.api import RevisionRef, SettlementId, Value


class Winner(Value):
    """The retained candidate proposed for adoption, or the trusted baseline."""

    settlement: SettlementId | None
    revision: RevisionRef
    hypothesis_id: str | None
    value: float | None = None
    """The candidate's headline value, when it was measured."""


@dataclass(frozen=True)
class EligibleRound:
    """One finished, winner-eligible round of a strategy's search."""

    hypothesis_id: str
    sequence: int
    settlement: SettlementId
    candidate: RevisionRef
    metrics: tuple[MetricRow, ...]


def ending_reason(ending: RunEnding, detail: str) -> str:
    """The typed reason a run result carries."""
    return f"{ending.value}: {detail}"


def choose_winner(
    rounds: tuple[EligibleRound, ...],
    *,
    space: MetricSpace,
    baseline: RevisionRef,
    when_unranked: Unranked,
) -> Winner:
    """The best eligible round by ``space``, else the trusted baseline.

    Earlier rounds win ties. ``when_unranked`` says what happens when no round has a metric.
    """
    best = best_by(
        rounds,
        space=space,
        reading=lambda item: measurement(item.metrics[0] if item.metrics else None),
        order=lambda item: item.sequence,
        when_unranked=when_unranked,
    )
    if best is None:
        return Winner(settlement=None, revision=baseline, hypothesis_id=None)
    return Winner(
        settlement=best.settlement,
        revision=best.candidate,
        hypothesis_id=best.hypothesis_id,
        value=best.metrics[0].value if best.metrics else None,
    )
