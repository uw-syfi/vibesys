"""What a serial search knows between rounds, and how it ends.

A serial search plays one round at a time. Each round is told where the search stands
(`RoundStart`) and ends with what it left behind (`RoundDone`); `SerialRun` folds the rounds in
order, decides when the search is over and which kept candidate leads. It holds no policy of
its own about how a round is played: single and multi play rounds differently and share this.
"""

from dataclasses import dataclass, field

from pydantic import Field

from vibesys.candidates import api as candidates
from vibesys.hypothesis import Finished, HypothesisConfig, RunEnding, rounds
from vibesys.hypothesis.state import HypothesisState
from vibesys.metrics import MetricRow
from vibesys.search_spans.api import adoption_label
from vibesys.serial_search._ledger import LedgerJson
from vibesys.serial_search._plan_view import LastProfile
from vs_core.api.search import (
    INPUT,
    Input,
    Kept,
    Label,
    NoWinner,
    RevisionRef,
    Settlement,
    SpanId,
    TaskTerms,
    Value,
)


class KeptBy(Value):
    """A revision a round kept, and the span of the round's workstream that kept it."""

    revision_id: str
    span: SpanId


class RoundStart(Value):
    """Where the search stands when a round begins."""

    number: int = Field(gt=0)
    base: RevisionRef
    """The newest revision a round may build on: a retained checkpoint, else the input."""
    ledger: LedgerJson
    last_profile: LastProfile | None = None
    baseline: candidates.Baseline
    kept: tuple[KeptBy, ...] = ()
    """The revisions earlier rounds kept, each with the workstream that kept it."""

    def kept_by(self, revision: RevisionRef) -> SpanId | None:
        """The span of the workstream that kept ``revision``; None for the input."""
        found = (item.span for item in self.kept if item.revision_id == revision.revision_id.root)
        return next(found, None)


class Closed(Value):
    """What the search keeps of a round core settled, for choosing the winner."""

    round_number: int = Field(gt=0)
    hypothesis_id: str
    kept: Kept | None = None
    """The candidate the settlement retained, if it retained one."""
    eligible: bool = False
    metrics: tuple[MetricRow, ...] = ()


class RoundDone(Value):
    """What a round leaves behind.

    ``counted`` says the round used one of the search's rounds (a round the operator stopped
    before it began does not). ``failure`` ends the search: the designer or the attribution
    could not go on.
    """

    number: int = Field(gt=0)
    ledger: LedgerJson
    counted: bool = True
    closed: Closed | None = None
    checkpoint: RevisionRef | None = None
    last_profile: LastProfile | None = None
    failure: str | None = None
    stopped: bool = False


def round_done(  # noqa: PLR0913  # LW-641205 [PLR0913]; the record joins independent facts of the finished round that no one record holds.
    start: RoundStart,
    closed: rounds.Ledger,
    *,
    hypothesis_id: str,
    settlement: Settlement | None,
    metrics: tuple[MetricRow, ...],
    last_profile: LastProfile | None = None,
) -> RoundDone:
    """What a round that settled (or whose workspace never opened) leaves behind."""
    return RoundDone(
        number=start.number,
        ledger=LedgerJson.of(closed),
        closed=None
        if settlement is None
        else Closed(
            round_number=start.number,
            hypothesis_id=hypothesis_id,
            kept=Kept.of(settlement),
            eligible=settlement.eligible,
            metrics=metrics,
        ),
        checkpoint=None if settlement is None else settlement.candidate,
        last_profile=last_profile,
    )


@dataclass
class SerialRun:
    """The record of a serial search: the ledger, the kept candidates and why it might end."""

    hypotheses: HypothesisConfig
    input: RevisionRef
    terms: TaskTerms
    baseline: candidates.Baseline
    ledger: LedgerJson = field(default_factory=LedgerJson)
    counted: int = 0
    checkpoint: RevisionRef | None = None
    last_profile: LastProfile | None = None
    closed: list[Closed] = field(default_factory=list)
    kept: list[KeptBy] = field(default_factory=list)
    failure: str | None = None
    stopped: bool = False

    def over(self, *, stopping: bool) -> bool:
        """Whether no round will begin: stopped, failed, out of rounds or out of hypotheses."""
        if stopping or self.stopped or self.failure is not None:
            return True
        if self.counted >= self.hypotheses.max_rounds:
            return True
        decoded = self.ledger.decode(candidates.space_of(self.terms))
        return isinstance(rounds.next_round(self.hypotheses, decoded, self.counted + 1), Finished)

    def start(self) -> RoundStart:
        """The next round, told where the search stands."""
        return RoundStart(
            number=self.counted + 1,
            base=self.checkpoint or self.input,
            ledger=self.ledger,
            last_profile=self.last_profile,
            baseline=self.baseline,
            kept=tuple(self.kept),
        )

    def finished(self, done: RoundDone, span: SpanId | None) -> None:
        """Fold a round that ended; ``span`` is the round's own, the workstream it labelled."""
        self.ledger = done.ledger
        self.counted += int(done.counted)
        if done.closed is not None:
            self.closed.append(done.closed)
        if done.checkpoint is not None:
            self.checkpoint = done.checkpoint
            if span is not None:
                self.kept.append(KeptBy(revision_id=done.checkpoint.revision_id.root, span=span))
        self.last_profile = done.last_profile or self.last_profile
        self.failure = done.failure or self.failure
        self.stopped = self.stopped or done.stopped

    def eligible(self) -> tuple[candidates.EligibleRound, ...]:
        """Every winner-eligible round that kept a candidate."""
        return tuple(
            candidates.EligibleRound(
                hypothesis_id=item.hypothesis_id,
                sequence=item.round_number,
                settlement=item.kept.settlement.settlement_id,
                candidate=item.kept.revision,
                metrics=item.metrics,
            )
            for item in self.closed
            if item.eligible and item.kept is not None
        )

    def leader(self) -> Kept | Input:
        """The kept candidate that leads now, else the input: what the run adopts if it ends."""
        return self.leading()[0]

    def leading(self) -> tuple[Kept | Input, Label]:
        """The leader, with the label of the span that adopts it (``ctx.choose``'s arguments)."""
        winner = candidates.choose_winner(
            self.eligible(),
            space=candidates.space_of(self.terms),
            baseline=self.input,
            when_unranked=candidates.Unranked.LATEST,
        )
        base = candidates.input_value(self.baseline)
        if winner.settlement is not None:
            for item in self.closed:
                if (
                    item.kept is not None
                    and item.kept.settlement.settlement_id == winner.settlement
                ):
                    return item.kept, adoption_label(item.kept, winner.value, base)
        return INPUT, adoption_label(INPUT, base, base)

    def ending(self) -> NoWinner | None:
        """None when the run ends with a winner or the trusted input standing; else why not."""
        if self.eligible():
            return None
        trusted = candidates.trusted(self.baseline)
        if trusted and self.failure is None:
            return None
        if trusted:
            return NoWinner(reason=self.failure or "no candidate could be adopted")
        why = candidates.failure(self.baseline)
        detail = why.kind.value if why is not None else "the input did not pass its measurement"
        if self.failure:
            detail = f"{detail}; {self.failure}"
        return NoWinner(reason=candidates.ending_reason(RunEnding.NO_TRUSTED_RESULT, detail))

    def snapshot(self) -> HypothesisState:
        """The hypothesis ledger as status views read it."""
        return self.ledger.decode().search
