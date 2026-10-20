"""What a serial search knows between rounds, and how it ends.

A serial search plays one round at a time. Each round is told where the search stands
(`RoundStart`) and ends with what it left behind (`RoundDone`); `SerialRun` folds the rounds in
order, decides when the search is over and which candidate is best. It holds no policy of its
own about how a round is played: single and multi play rounds differently and share this.

An idea starts from a base. The default base is the best idea so far (the input while none has
beaten it); an orchestrator may name another passed idea, or the input. An idea that continues
its own hypothesis builds on its own work in progress.
"""

from dataclasses import dataclass, field
from typing import Self

from pydantic import Field

from vibesys.candidates import api as candidates
from vibesys.hypothesis import Finished, HypothesisConfig, RunEnding, rounds
from vibesys.hypothesis.state import HypothesisState
from vibesys.metrics import MetricRow
from vibesys.search_spans.api import adoption_label
from vibesys.serial_search._ledger import LedgerJson
from vibesys.serial_search._plan_view import LastProfile, MeasurementNoise
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


class Retained(Value):
    """A revision a round retained, the span of its workstream, and how its idea fared.

    ``passed`` says the idea's own result held: the trusted gates measured the revision and
    nothing turned it down, so a later idea may start from it. ``value`` and ``unit`` are the
    headline the gates read.
    """

    round_number: int = Field(gt=0)
    revision: RevisionRef
    span: SpanId
    hypothesis_id: str | None = None
    passed: bool = False
    value: float | None = None
    unit: str | None = None


class RoundStart(Value):
    """Where the search stands when a round begins."""

    number: int = Field(gt=0)
    base: RevisionRef
    """The best revision so far (the input while none beat it): where a new idea starts unless
    its plan names another base."""
    best_round: int | None = None
    """The round whose idea made ``base``; None when it is the input."""
    work_in_progress: RevisionRef | None = None
    """What the previous round retained: where an idea continuing its own hypothesis starts."""
    ledger: LedgerJson
    last_profile: LastProfile | None = None
    baseline: candidates.Baseline
    retained: tuple[Retained, ...] = ()
    """The revisions earlier rounds retained, each with the workstream that retained it."""
    measurement_noise: MeasurementNoise | None = None
    """The noise the latest comparison of a candidate with its base stated; None while none has."""

    def retained_as(self, revision: RevisionRef) -> Retained | None:
        """The round that made ``revision``; None for the input.

        That is the first round to retain it: a later round holding the same revision unchanged
        (an idea that failed before it edited anything) made nothing.
        """
        return next((item for item in self.retained if item.revision == revision), None)

    def passed_ideas(self) -> tuple[Retained, ...]:
        """The ideas that passed, oldest first: the bases a plan may name besides the input."""
        return tuple(item for item in self.retained if item.passed)


class Closed(Value):
    """What the search keeps of a round core settled, for choosing the winner."""

    round_number: int = Field(gt=0)
    hypothesis_id: str
    kept: Kept | None = None
    """The candidate the settlement retained, if it retained one."""
    passed: bool = False
    """The idea's own result held (see `Retained`)."""
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
    measurement_noise: MeasurementNoise | None = None
    """The noise the round's trusted comparison stated; None when it had none."""
    failure: str | None = None
    stopped: bool = False


def round_done(  # noqa: PLR0913  # LW-641205 [PLR0913]; the record joins independent facts of the finished round that no one record holds.
    start: RoundStart,
    closed: rounds.Ledger,
    *,
    hypothesis_id: str,
    settlement: Settlement | None,
    passed: bool,
    metrics: tuple[MetricRow, ...],
    last_profile: LastProfile | None = None,
    measurement_noise: MeasurementNoise | None = None,
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
            passed=passed,
            eligible=settlement.eligible,
            metrics=metrics,
        ),
        checkpoint=None if settlement is None else settlement.candidate,
        last_profile=last_profile,
        measurement_noise=measurement_noise,
    )


class SerialState(Value):
    """What a serial search knows between rounds, as its root checkpoints it.

    After a code change the root resumes from it (`SerialRun.resumed`): the ledger, the rounds
    folded so far, the best, and the input's measurement, so no round is played again and nothing
    measured is measured again.
    """

    baseline: candidates.Baseline
    """The input's measurement: not asked for again once it landed."""
    ledger: LedgerJson
    counted: int = 0
    work_in_progress: RevisionRef | None = None
    last_profile: LastProfile | None = None
    measurement_noise: MeasurementNoise | None = None
    closed: tuple[Closed, ...] = ()
    retained: tuple[Retained, ...] = ()
    failure: str | None = None
    stopped: bool = False
    """Saved by earlier versions, which ended the search by it; nothing reads it now.

    A stop is judged by ``ctx.stop_requested``, which a resume that lifts the stop clears. A
    flag kept in the checkpoint would end the search again after that resume.
    """


@dataclass
class SerialRun:
    """The record of a serial search: the ledger, the ideas' results and why it might end."""

    hypotheses: HypothesisConfig
    input: RevisionRef
    terms: TaskTerms
    baseline: candidates.Baseline
    ledger: LedgerJson = field(default_factory=LedgerJson)
    counted: int = 0
    work_in_progress: RevisionRef | None = None
    last_profile: LastProfile | None = None
    measurement_noise: MeasurementNoise | None = None
    closed: list[Closed] = field(default_factory=list)
    retained: list[Retained] = field(default_factory=list)
    failure: str | None = None

    def state(self) -> SerialState:
        """What the root checkpoints (`SerialState`)."""
        return SerialState(
            baseline=self.baseline,
            ledger=self.ledger,
            counted=self.counted,
            work_in_progress=self.work_in_progress,
            last_profile=self.last_profile,
            measurement_noise=self.measurement_noise,
            closed=tuple(self.closed),
            retained=tuple(self.retained),
            failure=self.failure,
        )

    @classmethod
    def resumed(
        cls,
        hypotheses: HypothesisConfig,
        input_revision: RevisionRef,
        terms: TaskTerms,
        state: SerialState,
    ) -> Self:
        """The run as ``state`` saved it, on the input revision and terms the run was given."""
        return cls(
            hypotheses=hypotheses,
            input=input_revision,
            terms=terms,
            baseline=state.baseline,
            ledger=state.ledger,
            counted=state.counted,
            work_in_progress=state.work_in_progress,
            last_profile=state.last_profile,
            measurement_noise=state.measurement_noise,
            closed=list(state.closed),
            retained=list(state.retained),
            failure=state.failure,
        )

    def over(self, *, stopping: bool) -> bool:
        """Whether no round will begin: stopped, failed, out of rounds or out of hypotheses.

        ``stopping`` is whether a stop stands now (a search passes ``ctx.stop_requested``). It is
        not kept: a resume that lifts the stop lets the search go on with the rounds it has left.
        """
        if stopping or self.failure is not None:
            return True
        if self.counted >= self.hypotheses.max_rounds:
            return True
        decoded = self.ledger.decode(candidates.space_of(self.terms))
        return isinstance(rounds.next_round(self.hypotheses, decoded, self.counted + 1), Finished)

    def start(self) -> RoundStart:
        """The next round, told where the search stands."""
        best = self._best()
        return RoundStart(
            number=self.counted + 1,
            base=self.input if best is None else best[1].revision,
            best_round=None if best is None else best[0],
            work_in_progress=self.work_in_progress,
            ledger=self.ledger,
            last_profile=self.last_profile,
            baseline=self.baseline,
            retained=tuple(self.retained),
            measurement_noise=self.measurement_noise,
        )

    def finished(self, done: RoundDone, span: SpanId | None) -> None:
        """Fold a round that ended; ``span`` is the round's own, the workstream it labelled.

        A round the operator stopped counts for nothing (``RoundDone.counted``), and it ends the
        search only while the stop stands (`over`).
        """
        self.ledger = done.ledger
        self.counted += int(done.counted)
        if done.closed is not None:
            self.closed.append(done.closed)
        self.work_in_progress = done.checkpoint
        if done.checkpoint is not None and done.checkpoint != self.input and span is not None:
            closed = done.closed
            headline = None if closed is None or not closed.metrics else closed.metrics[0]
            self.retained.append(
                Retained(
                    round_number=done.number,
                    revision=done.checkpoint,
                    span=span,
                    hypothesis_id=None if closed is None else closed.hypothesis_id,
                    passed=closed is not None and closed.passed,
                    value=None if headline is None else headline.value,
                    unit=None if headline is None else headline.unit,
                )
            )
        self.last_profile = done.last_profile or self.last_profile
        self.measurement_noise = done.measurement_noise or self.measurement_noise
        self.failure = done.failure or self.failure

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
        """The best candidate now, else the input: what the run adopts if it ends."""
        return self.leading()[0]

    def leading(self) -> tuple[Kept | Input, Label]:
        """The best, with the label of the span that adopts it (``ctx.choose``'s arguments)."""
        winner = self._winner()
        base = candidates.input_value(self.baseline)
        best = self._best(winner)
        if best is not None:
            return best[1], adoption_label(best[1], winner.value, base)
        return INPUT, adoption_label(INPUT, base, base)

    def _winner(self) -> candidates.Winner:
        return candidates.choose_winner(
            self.eligible(),
            space=candidates.space_of(self.terms),
            baseline=self.input,
            when_unranked=candidates.Unranked.LATEST,
        )

    def _best(self, winner: candidates.Winner | None = None) -> tuple[int, Kept] | None:
        """The round and candidate that are the best (the run's winner); None for the input."""
        winner = winner or self._winner()
        if winner.settlement is None:
            return None
        for item in self.closed:
            kept = item.kept
            if kept is not None and kept.settlement.settlement_id == winner.settlement:
                return item.round_number, kept
        return None

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
