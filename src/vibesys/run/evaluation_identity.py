"""The non-candidate identities of a run's trusted evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vs_evaluation.api import ContentDigest

if TYPE_CHECKING:
    from vs_runtime.api import RunFacts
    from vs_runtime.api.infrastructure import RunEnvironmentView, TrustedEvaluationPlan


@dataclass(frozen=True, slots=True)
class SemanticEvaluationIdentity:
    """Stable non-candidate identities supplied by product composition."""

    evaluator: ContentDigest
    workload: ContentDigest
    environment: ContentDigest


def semantic_evaluation_identity(
    plan: TrustedEvaluationPlan, facts: RunFacts, environment: RunEnvironmentView
) -> SemanticEvaluationIdentity:
    """The non-candidate identities of one run: its evaluator, workload and environment.

    One definition for every consumer that must agree on them: the core run facts and the
    agent evaluation bridge. Each digest is the SHA-256 of the canonical text of its
    resolved input.
    """

    def digest(value: str) -> ContentDigest:
        return ContentDigest.sha256(value.encode())

    return SemanticEvaluationIdentity(
        evaluator=digest(repr(plan)),
        workload=digest(facts.model_dump_json()),
        environment=digest(repr(environment)),
    )


__all__ = ["SemanticEvaluationIdentity", "semantic_evaluation_identity"]
