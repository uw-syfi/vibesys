"""Typed attempt and review facts shared by agent-run record builders."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, assert_never

from vibesys.evaluators.gates import FrameworkBenchmarkOutcome
from vibesys.roles.common import Verdict

if TYPE_CHECKING:
    from vibesys.agent_run.state import AgentRunState
    from vibesys.roles.implementer import ImplementerResponse
    from vibesys.roles.single_agent import SingleAgentRoundResponse
    from vs_loop_state.api import JudgeVerdict, PerfProvenance


class JudgeSkipReason(StrEnum):
    """Why one implementer attempt received no independent judge verdict."""

    NOT_REACHED = "not_reached"
    UNPARSEABLE_IMPLEMENTATION = "unparseable_implementation"
    SPARSE_REVIEW_POLICY = "sparse_review_policy"


@dataclass(frozen=True, slots=True)
class JudgeReviewed:
    """An independent judge audited this attempt and returned a verdict."""

    verdict: Verdict


@dataclass(frozen=True, slots=True)
class JudgeSkipped:
    """No judge ran for this attempt, for the given reason."""

    reason: JudgeSkipReason


type JudgeOutcome = JudgeReviewed | JudgeSkipped


def attempt_was_reviewed(outcome: JudgeOutcome) -> bool:
    """Return whether an independent judge ruled on this attempt."""
    match outcome:
        case JudgeReviewed():
            return True
        case JudgeSkipped():
            return False
        case _:
            assert_never(outcome)


def recorded_judge_verdict(outcome: JudgeOutcome) -> JudgeVerdict:
    """Persist a skipped review as deferred, never as a prior verdict."""
    match outcome:
        case JudgeReviewed(verdict=verdict):
            match verdict:
                case Verdict.PASS:
                    return "pass"
                case Verdict.FAIL:
                    return "fail"
                case _:
                    assert_never(verdict)
        case JudgeSkipped():
            return "deferred"
        case _:
            assert_never(outcome)


class AttemptDecision(StrEnum):
    """What the shared retry executor should do after a policy turn."""

    RETRY = "retry"
    FINISH = "finish"
    OFFICIAL = "official"


@dataclass
class AttemptState:
    """Mutable facts that survive retries within one framework round."""

    agent_run_state: AgentRunState
    feedback: str | None
    implementation: ImplementerResponse | None = None
    single_agent_response: SingleAgentRoundResponse | None = None
    judge: JudgeOutcome = field(default_factory=lambda: JudgeSkipped(JudgeSkipReason.NOT_REACHED))
    passed: bool = False
    review_started: bool = False
    revalidation_required: bool = False
    official_reason: str | None = None
    framework_benchmark: FrameworkBenchmarkOutcome = field(
        default_factory=FrameworkBenchmarkOutcome
    )
    framework_perf_metric: float | None = None
    retry: int = 0


@dataclass(frozen=True)
class PerformanceProjection:
    """Performance evidence produced by an attempt policy for the round record."""

    metric: float | None
    unit: str | None
    provenance: PerfProvenance | None
    profile_skipped: bool
    accepted_metrics: dict[str, float]
    accepted_evaluation_artifact: str | None
    next_single_response: SingleAgentRoundResponse | None
