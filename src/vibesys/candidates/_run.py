"""Run-level policy: which candidate wins and how the run ends.

The winner is the best winner-eligible retained candidate by the configured metric space, else
the trusted baseline. Adoption is proposed once, and the run stops only after core reports the
adoption result.

A run ends in one of three typed ways, so that success always means the operator has a trusted,
measured result to use:

- ``adopted``: a retained, accuracy-verified candidate beat the input (success);
- ``no improvement``: no candidate did, but the input was measured and trusted, so the operator
  keeps it (success);
- ``no trusted result``: the input failed its measurement and no trusted candidate was found,
  so there is nothing to keep (failure).

The strategy decides *when* each phase begins (it knows what is in flight); this module decides
what is chosen, proposed and stopped with.
"""

from dataclasses import dataclass
from enum import StrEnum

from vibesys.candidates._config import CandidateConfig
from vibesys.candidates._settlement import measurement
from vibesys.hypothesis import RunEnding
from vibesys.metrics import MetricRow
from vs_core.api import (
    AdoptionResult,
    IdScheme,
    ObservationStatus,
    ProposeWinner,
    RetainedCandidate,
    RevisionRef,
    RunResultProposal,
    RunView,
    Selection,
    SettlementId,
    Stop,
    TrustedBaseline,
    Value,
    run_scope,
)


class RunPhase(StrEnum):
    """Where the whole search is."""

    SEARCHING = "searching"
    SELECTING = "selecting"
    ADOPTING = "adopting"
    STOPPING = "stopping"
    FINISHED = "finished"


class Winner(Value):
    """The retained candidate proposed for adoption, or the trusted baseline."""

    settlement: SettlementId | None
    revision: RevisionRef
    hypothesis_id: str | None


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
    rounds: tuple[EligibleRound, ...], config: CandidateConfig, baseline: RevisionRef
) -> Winner:
    """The best eligible candidate by the metric space; earlier rounds win ties."""
    points = sorted(rounds, key=lambda item: item.sequence)
    best = config.metric_space.best(
        points, lambda item: measurement(item.metrics[0] if item.metrics else None)
    )
    if best is None and points and not any(item.metrics for item in points):
        best = points[-1]  # no benchmark configured: the latest eligible candidate
    if best is None:
        return Winner(settlement=None, revision=baseline, hypothesis_id=None)
    return Winner(
        settlement=best.settlement, revision=best.candidate, hypothesis_id=best.hypothesis_id
    )


def selection_of(winner: Winner) -> Selection:
    """The core selection a winner proposes for adoption."""
    if winner.settlement is None:
        return TrustedBaseline(revision=winner.revision)
    return RetainedCandidate(settlement_id=winner.settlement, revision=winner.revision)


def proposal(ids: IdScheme, view: RunView, winner: Winner) -> ProposeWinner:
    """Propose the winner for adoption, once."""
    return ProposeWinner(
        decision_id=ids.decision_id("propose", "run"),
        scope=run_scope(view),
        selection=selection_of(winner),
    )


def stop(
    ids: IdScheme,
    view: RunView,
    winner: Winner | None,
    *,
    stopping: bool,
    no_winner_reason: str,
) -> Stop:
    """End the run after the adoption result: success with a winner, else failure.

    An operator stop ends it ``cancelled`` either way.
    """
    if winner is None:
        result = RunResultProposal(
            outcome="cancelled" if stopping else "failure", reason=no_winner_reason
        )
    else:
        ending = RunEnding.NO_IMPROVEMENT if winner.hypothesis_id is None else RunEnding.ADOPTED
        result = RunResultProposal(
            outcome="cancelled" if stopping else "success",
            reason=ending_reason(ending, winner.hypothesis_id or "kept the trusted input"),
            selection=selection_of(winner),
        )
    return Stop(
        decision_id=ids.decision_id("stop", "run"),
        scope=run_scope(view),
        mode="drain",
        result=result,
    )


def after_adoption(
    phase: RunPhase, winner: Winner | None, event: AdoptionResult
) -> tuple[RunPhase, Winner | None]:
    """Stop after a successful adoption; a failed one stops without a winner."""
    if phase is not RunPhase.ADOPTING:
        return phase, winner
    if event.observation.status is ObservationStatus.SUCCEEDED:
        return RunPhase.STOPPING, winner
    return RunPhase.STOPPING, None
