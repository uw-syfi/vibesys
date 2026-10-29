"""How a serial search's root begins: from its snapshot, from an older save, or from nothing.

A run that resumes on changed code restarts its root from the latest snapshot the root
checkpointed (`SerialState`), so the search goes on at the next round with its ledger, best and
input measurement. A run saved before roots checkpointed their state has only its config as a
snapshot; its history is still in the run's record (the ledger the root published, the
settlements core recorded, the spans of the rounds), and `rebuilt` reads it back. What the
record does not hold is not guessed (see `rebuilt`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibesys.candidates import api as candidates
from vibesys.hypothesis import HypothesisSearch, rounds
from vibesys.hypothesis.state import HypothesisState
from vibesys.metrics import MetricRow
from vibesys.serial_search._ledger import LedgerJson
from vibesys.serial_search._run import Closed, Retained, SerialRun, SerialState
from vs_core.api.search import Kept, SpanType

if TYPE_CHECKING:
    from vibesys.hypothesis import HypothesisConfig, RoundRecord
    from vibesys.metrics import MetricSpace
    from vs_core.api.search import Flow, RevisionRef, RunTrace, Settlement, SpanId, TaskTerms


async def open_run(
    ctx: Flow, hypotheses: HypothesisConfig, snapshot: SerialState | None
) -> SerialRun:
    """The run a serial search's root plays: resumed, rebuilt from an older save, or started.

    With a ``snapshot`` the input is not measured again. Without one the input is measured (core
    answers a measurement it already holds without running it again), and a run that published a
    ledger before, which only a run saved by a root that checkpointed no state did, has what
    `rebuilt` recovers of its rounds put back.
    """
    terms = ctx.task.terms
    if snapshot is not None:
        return SerialRun.resumed(hypotheses, ctx.facts.baseline, terms, snapshot)
    run = SerialRun(hypotheses, ctx.facts.baseline, terms, await candidates.measure_input(ctx))
    published = ctx.published(HypothesisState)
    if published is None or not published.rounds:
        return run
    saved = Saved(published, ctx.settlements, ctx.spans)
    restored = rebuilt(hypotheses, terms, run.input, run.baseline, saved)
    return SerialRun.resumed(hypotheses, run.input, terms, restored)


@dataclass(frozen=True)
class Saved:
    """What a run recorded of its rounds, from three owners: its root's ledger, core, the trace."""

    published: HypothesisState
    settlements: tuple[Settlement, ...]
    trace: RunTrace


def rebuilt(
    hypotheses: HypothesisConfig,
    terms: TaskTerms,
    input_revision: RevisionRef,
    baseline: candidates.Baseline,
    saved: Saved,
) -> SerialState:
    """The state of a serial search read back from what its run recorded.

    Each round the ledger recorded is paired with the settlement core recorded for its attempt
    (an attempt is named for its hypothesis and round number) and the span of its workstream.
    The ledger is the published one, with the carry-over seeded from its records as a resume
    does. From these come the rounds counted, the rounds closed with the candidate each kept
    (ranked on the metrics the round recorded), the revisions retained, and the work in
    progress.

    What the record does not hold is not guessed. The previous round's profile summary and the
    noise its comparison stated (read by the next design turn) are absent until the next round
    states them. A round passed when its settlement kept a candidate that was eligible: the
    record does not say whether a kept but ineligible candidate passed its own checks, so it
    reads as not passed, and a later idea may then not start from it. A round whose settlement
    or span is not found is not closed or retained.
    """
    published, settlements, trace = saved.published, saved.settlements, saved.trace
    records = published.rounds
    space = candidates.space_of(terms)
    ledger = rounds.Ledger(
        search=HypothesisSearch(hypotheses).resume(published, space),
        carry=HypothesisSearch(hypotheses).initial_carry(records),
    )
    closed: list[Closed] = []
    retained: list[Retained] = []
    work_in_progress: RevisionRef | None = None
    for record in records:
        settlement = _settlement_of(record, settlements)
        if settlement is None:
            continue
        closing = _closed(record, settlement, space)
        closed.append(closing)
        work_in_progress = settlement.candidate
        span = _span_of(record.round_number, trace)
        if settlement.candidate is not None and settlement.candidate != input_revision and span:
            headline = closing.metrics[0] if closing.metrics else None
            retained.append(
                Retained(
                    round_number=record.round_number,
                    revision=settlement.candidate,
                    span=span,
                    hypothesis_id=record.hypothesis_id,
                    passed=closing.passed,
                    value=None if headline is None else headline.value,
                    unit=None if headline is None else headline.unit,
                )
            )
    return SerialState(
        baseline=baseline,
        ledger=LedgerJson.of(ledger),
        counted=len(records),
        work_in_progress=work_in_progress,
        closed=tuple(closed),
        retained=tuple(retained),
    )


def _settlement_of(record: RoundRecord, settlements: tuple[Settlement, ...]) -> Settlement | None:
    """The settlement of the attempt a round played: ``attempt:<hypothesis>:<round>``."""
    named = f"attempt:{record.hypothesis_id}:{record.round_number}"
    return next((item for item in settlements if item.attempt.attempt_id.root == named), None)


def _closed(record: RoundRecord, settlement: Settlement, space: MetricSpace) -> Closed:
    directions = {item.name: item.direction for item in space.objectives}
    rows = tuple(
        MetricRow(
            name=name,
            value=value,
            direction=directions[name],
            unit=record.perf_unit if index == 0 else None,
        )
        for index, (name, value) in enumerate(record.metrics.items())
        if name in directions
    )
    return Closed(
        round_number=record.round_number,
        hypothesis_id=record.hypothesis_id or "",
        kept=Kept.of(settlement),
        passed=settlement.retention != "discard" and settlement.eligible,
        eligible=settlement.eligible,
        metrics=rows,
    )


_ROUND_FLOW = re.compile(r"^flow:(\d+)/#(\d+)$")


def _span_of(number: int, trace: RunTrace) -> SpanId | None:
    """The span of round ``number``'s workstream: its latest run of the task of that key."""
    found: tuple[int, SpanId] | None = None
    for span in trace.spans:
        match = _ROUND_FLOW.match(span.id)
        if span.type is SpanType.FLOW and match and int(match.group(1)) == number:
            occurrence = int(match.group(2))
            if found is None or occurrence > found[0]:
                found = (occurrence, span.id)
    return None if found is None else found[1]
