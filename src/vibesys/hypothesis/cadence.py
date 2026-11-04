"""Review cadence and the measurement trigger, ported from ``loops/*/decisions.py``.

``multi/decisions.py`` and ``profile_multi/decisions.py`` carried byte-for-byte
identical copies of ``review_due``, ``candidate_evidence_is_fresh``, and
``official_evaluation_reason`` (now ``official_due``), plus two review-cadence overrides duplicated
in ``multi/session.py`` and ``profile_multi/session.py`` (``review_started``
forcing a review to completion, and a bounded-continuation retry deferring
one). This module keeps that one copy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.hypothesis.history import CandidateDisposition, HypothesisOutcome

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vibesys.hypothesis.config import HypothesisConfig
    from vibesys.hypothesis.state import RoundRecord

_CONTINUATION_OUTCOMES = frozenset(
    {
        HypothesisOutcome.CONTINUE,
        HypothesisOutcome.IMPLEMENTATION_FAILED,
        HypothesisOutcome.INCONCLUSIVE,
    }
)


def continuation_requested(*, outcome: HypothesisOutcome | None, next_step: str) -> bool:
    """Require a concrete unfinished same-hypothesis step."""
    return outcome in _CONTINUATION_OUTCOMES and bool(next_step.strip())


def keeps_hypothesis_active(
    *,
    outcome: HypothesisOutcome | None,
    next_step: str,
    continuation_rounds: int,
    max_continuation_rounds: int,
) -> bool:
    """Bound the implementer's continuation lease."""
    return continuation_requested(outcome=outcome, next_step=next_step) and (
        continuation_rounds < max_continuation_rounds
    )


def candidate_evidence_fresh(
    *,
    candidate_metrics: dict[str, float],
    candidate_evaluation_artifact: str | None,
    records: Sequence[RoundRecord],
) -> bool:
    """Detect a previously unseen objective row requiring review."""
    if not candidate_metrics:
        return False
    if not candidate_evaluation_artifact:
        return True
    metrics = dict(candidate_metrics)
    return not any(
        (record.candidate_evaluation_artifact or record.evaluation_artifact)
        == candidate_evaluation_artifact
        and record.candidate_metrics == metrics
        for record in records
    )


def review_due(  # noqa: PLR0913  # LW-040039 [PLR0913]; the parameters are independent injected collaborators or options, and bundling them would hide ownership.
    config: HypothesisConfig,
    *,
    round_number: int,
    outcome: HypothesisOutcome,
    candidate_evidence_is_fresh: bool = False,
    review_started: bool = False,
    requests_continuation: bool = False,
    pareto_frontier_claim: bool = False,
    revalidation_required: bool = False,
) -> bool:
    """Require review at cadence, final round, or on a new candidate claim.

    ``review_started``/``requests_continuation``/``pareto_frontier_claim``/
    ``revalidation_required`` are the four override sites from
    ``multi/session.py`` and ``profile_multi/session.py``'s ``review``: once
    an independent judge has started reviewing a round it must finish
    (``review_started and not requests_continuation``), and a bounded,
    already-reviewed continuation with no fresh Pareto claim and no pending
    gate revalidation may skip a repeat review.
    """
    due = (
        round_number == config.max_rounds
        or round_number % config.judge_every == 0
        or outcome in {HypothesisOutcome.SUPPORTED, HypothesisOutcome.NOMINATED}
        or candidate_evidence_is_fresh
    )
    if review_started and not requests_continuation:
        due = True
    if (
        review_started
        and round_number != config.max_rounds
        and requests_continuation
        and not pareto_frontier_claim
        and not revalidation_required
    ):
        due = False
    return due


MEASURED_AFTER_REVIEW = "review_passed"
"""Why the trusted gates measure an idea: it passed review."""


MEASURED_AFTER_EVALUATION = "evaluated_before_stop"
"""Why the trusted gates measure an idea its implementer left unsettled: the agent had evaluated it."""


def official_due(*, candidate_ready: bool) -> str | None:
    """Why the trusted gates measure this round's reviewed candidate, or None.

    Every idea that passes review is measured; nothing else schedules a measurement.
    """
    return MEASURED_AFTER_REVIEW if candidate_ready else None


__all__ = [
    "MEASURED_AFTER_EVALUATION",
    "MEASURED_AFTER_REVIEW",
    "CandidateDisposition",
    "candidate_evidence_fresh",
    "continuation_requested",
    "keeps_hypothesis_active",
    "official_due",
    "review_due",
]
