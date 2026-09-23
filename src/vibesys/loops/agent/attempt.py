"""Per-attempt values produced by the agent loop's implementer/judge retry.

One round runs up to ``max_retries_per_round`` implementer attempts, and only
the final attempt describes the round. Attempt-scoped facts therefore have a
shorter lifetime than the round-scoped accumulators around them (the best
accepted implementation, the completed official evaluation). Carrying them in
mutable locals that outlive an iteration made the two lifetimes look alike and
produced issue #503: the judge's verdict for attempt N survived into a record
whose final attempt N+1 was never reviewed.

``JudgeOutcome`` closes that gap by value: an attempt either was reviewed and
therefore has a verdict, or was skipped and therefore has a reason instead. A
verdict without a review is not constructible, so no reset discipline has to be
remembered when a new attempt-scoped fact is added.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, assert_never

from vibesys.schemas import Verdict

if TYPE_CHECKING:
    from vs_loop_state.api import JudgeVerdict


class JudgeSkipReason(StrEnum):
    """Why one implementer attempt received no independent judge verdict."""

    #: The attempt ended before the judge decision point. Also the initial
    #: value, so the outcome is bound even if no attempt runs.
    NOT_REACHED = "not_reached"
    #: The implementer turn produced no parseable structured response, so the
    #: framework synthesized a fail-closed one that carries no reviewable
    #: evidence.
    UNPARSEABLE_IMPLEMENTATION = "unparseable_implementation"
    #: Sparse-review policy deferred the audit to a later round.
    SPARSE_REVIEW_POLICY = "sparse_review_policy"


@dataclass(frozen=True, slots=True)
class JudgeReviewed:
    """An independent judge audited this attempt and returned ``verdict``."""

    verdict: Verdict


@dataclass(frozen=True, slots=True)
class JudgeSkipped:
    """No judge ran for this attempt, for ``reason``."""

    reason: JudgeSkipReason


#: One attempt's judge result. The two variants are exhaustive.
type JudgeOutcome = JudgeReviewed | JudgeSkipped


def attempt_was_reviewed(outcome: JudgeOutcome) -> bool:
    """Return whether an independent judge ruled on this attempt."""
    match outcome:
        case JudgeReviewed():
            return True
        case JudgeSkipped():
            return False
        case _:  # pragma: no cover - exhaustiveness is enforced statically
            assert_never(outcome)


def recorded_judge_verdict(outcome: JudgeOutcome) -> JudgeVerdict:
    """Return the persisted-record verdict for this attempt.

    A skipped review is recorded as ``deferred`` rather than as a missing
    value, so the round record states the review fact exactly once.
    """
    match outcome:
        case JudgeReviewed(verdict=verdict):
            match verdict:
                case Verdict.PASS:
                    return "pass"
                case Verdict.FAIL:
                    return "fail"
                case _:  # pragma: no cover - exhaustiveness is enforced statically
                    assert_never(verdict)
        case JudgeSkipped():
            return "deferred"
        case _:  # pragma: no cover - exhaustiveness is enforced statically
            assert_never(outcome)
