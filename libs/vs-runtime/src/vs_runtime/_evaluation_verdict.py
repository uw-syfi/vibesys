"""Whose fault a finished evaluation is, as a closed union that policy must match.

A failed trusted evaluation is the candidate's (``WORKLOAD``), the machinery's
(``INFRASTRUCTURE``), or cannot be told (``AMBIGUOUS``); see ``classify``. Policy that
read only ``passed`` charged all three to the candidate: a node loss spent a paid
implementer attempt and sent the machinery's error to the implementer as something to
repair. ``verdict_of`` turns an evaluation into one of three variants, and a policy
matches them exhaustively (``assert_never`` makes a missed case a type error).

``SettlingMeasurement`` is the one rule for measuring again, so a policy only ever acts on
a settled verdict.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, assert_never

from vs_core.api import DEFAULT_MAX_MEASUREMENT_SUBMISSIONS, may_resubmit
from vs_runtime._failure_classification import job_failure
from vs_runtime.contracts import BenchmarkFailureKind, RuntimeContractError

__all__ = [
    "CandidateFailed",
    "EvaluationPassed",
    "EvaluationVerdict",
    "InfrastructureFailed",
    "SettlingMeasurement",
    "StageEvaluation",
    "verdict_of",
]


class StageEvaluation(Protocol):
    """What a verdict reads from an evaluation: its feedback and who is at fault."""

    @property
    def feedback(self) -> str | None:
        """The failure text, absent when the evaluation passed."""
        ...

    @property
    def failure_kind(self) -> BenchmarkFailureKind | None:
        """Whose fault the failure is, present exactly when the evaluation failed."""
        ...


@dataclass(frozen=True, slots=True)
class EvaluationPassed[Evaluated: StageEvaluation]:
    """Policy may accept the evaluation."""

    evaluation: Evaluated


@dataclass(frozen=True, slots=True)
class CandidateFailed[Evaluated: StageEvaluation]:
    """The candidate failed on its own account: feedback for the implementer."""

    evaluation: Evaluated
    feedback: str


@dataclass(frozen=True, slots=True)
class InfrastructureFailed[Evaluated: StageEvaluation]:
    """The machinery failed, so nothing is known about the candidate.

    ``kind`` is ``INFRASTRUCTURE`` or ``AMBIGUOUS``. The feedback describes the machinery
    and is never a repair instruction for the implementer.
    """

    evaluation: Evaluated
    feedback: str
    kind: Literal[BenchmarkFailureKind.INFRASTRUCTURE, BenchmarkFailureKind.AMBIGUOUS]


type EvaluationVerdict[Evaluated: StageEvaluation] = (
    EvaluationPassed[Evaluated] | CandidateFailed[Evaluated] | InfrastructureFailed[Evaluated]
)


def verdict_of[Evaluated: StageEvaluation](evaluation: Evaluated) -> EvaluationVerdict[Evaluated]:
    """The verdict of one finished evaluation, read from its feedback and failure kind."""
    feedback = evaluation.feedback
    if feedback is None:
        return EvaluationPassed(evaluation)
    kind = evaluation.failure_kind
    match kind:
        case BenchmarkFailureKind.WORKLOAD:
            return CandidateFailed(evaluation, feedback)
        case BenchmarkFailureKind.INFRASTRUCTURE | BenchmarkFailureKind.AMBIGUOUS:
            return InfrastructureFailed(evaluation, feedback, kind)
        case None:
            message = "a failed evaluation does not say whose fault the failure is"
            raise RuntimeContractError(message)
        case _:
            assert_never(kind)


@dataclass(slots=True)
class SettlingMeasurement:
    """Decides, reading by reading, whether a measurement is settled or is measured again.

    A policy measures in a loop and feeds each reading to ``observe`` until it returns a
    verdict. An infrastructure failure is measured again, an ambiguous one once more and
    then counts against the candidate, both within ``limit`` submissions, the bound core
    applies (``vs_core.api.may_resubmit``). ``InfrastructureFailed`` is returned only for
    an infrastructure failure that outlasted the bound, and a policy never charges it to
    the candidate. The decision is pure; the policy keeps the effect (the await).
    """

    limit: int = DEFAULT_MAX_MEASUREMENT_SUBMISSIONS
    submissions: int = 0

    def observe[Evaluated: StageEvaluation](
        self, evaluation: Evaluated
    ) -> EvaluationVerdict[Evaluated] | None:
        """The verdict of this reading, or ``None`` when it is to be measured again."""
        self.submissions += 1
        verdict = verdict_of(evaluation)
        if not isinstance(verdict, InfrastructureFailed):
            return verdict
        failure = job_failure([verdict.kind])
        if may_resubmit(failure, submissions=self.submissions, limit=self.limit):
            return None
        if verdict.kind is BenchmarkFailureKind.AMBIGUOUS:
            # Measured as often as it may be and still unexplained: it counts against the candidate.
            return CandidateFailed(verdict.evaluation, verdict.feedback)
        return verdict
