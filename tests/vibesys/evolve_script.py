"""Scripted far side of an evolve run: what its agents reply and what its benchmarks measure.

`EvolveExecutors` is the dynamic script's executor set (workspaces, sessions, measurements, the
evaluator) with the evolve search's roles and operations: the mutator, the judge and the profiler
answer from their queues, and the operations the search declares (render, verify, interpret,
export) are answered by the evolve owners.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from tests.support.dynamic_script import FACTS, TERMS, Executors, lease

from vibesys.candidates.api import limits_for
from vibesys.orchestration.evolve.core_policy.api import requirements_for
from vibesys.orchestration.evolve.search.api import (
    OPERATIONS,
    EvidenceReadings,
    EvolveConfig,
    ExportPatch,
    InterpretEvidence,
    ParentVerification,
    PatchExport,
    RenderedArtifacts,
    RenderRoleArtifacts,
    VerifyParentRevision,
    budget,
    evolve,
)
from vs_core.api import (
    ArtifactId,
    ArtifactRef,
    DispatchTurn,
    ExecuteRegisteredOperation,
    FlowState,
    ResumeSessionTurn,
    RunEnvelope,
)
from vs_core.testing.builders import sample_retries
from vs_core.testing.drive import Answer, Harness, Succeeded, Unknown

if TYPE_CHECKING:
    from vs_core.api import Limits


class _Bounds:
    """The declarations of a backend whose requests are retried twice and polled quickly."""

    request_retries = sample_retries(2)
    observe_interval_seconds = 1.0
    observe_backoff_cap_seconds = 1.0
    cancellation_bound_seconds = 60.0
    cleanup_bound_seconds = 60.0
    reconciliation_bound_seconds = 5000.0
    queue_wait_budget_seconds = 43_200.0
    queue_notice_interval_seconds = 600.0


def config(**overrides: object) -> EvolveConfig:
    """A one-generation, one-child config with two cold starts, overridden by the caller."""
    return EvolveConfig.model_validate(
        {
            "max_generations": 1,
            "children_per_generation": 1,
            "k_top_inspirations": 0,
            "k_random_inspirations": 0,
            "selection_temperature": 1.0,
            "frontier_bias": 0.7,
            "bootstrap_max_attempts": 2,
            "seed": 7,
            **overrides,
        }
    )


def limits(selected: EvolveConfig) -> Limits:
    """The core limits a run of ``selected`` is given, as the host derives them."""
    return limits_for(budget(selected), _Bounds())


def harness(selected: EvolveConfig, **overrides: object) -> Harness:
    """What hosts the evolve search of ``selected`` on the real core."""
    hosting = Harness(
        registry=OPERATIONS.registry(),
        facts=FACTS,
        limits=limits(selected),
        envelope_type=RunEnvelope[FlowState],
        requirements=requirements_for(TERMS),
        schemas=evolve.reply_schemas,
    )
    return replace(hosting, **overrides)


def mutated(summary: str = "tweak") -> str:
    """A mutator's reply."""
    return json.dumps({"summary": summary, "hypothesis": "faster", "expected_behavior": "up"})


def reviewed(*, passed: bool = True) -> str:
    """A judge's reply."""
    return json.dumps(
        {
            "analysis": "analysis",
            "feedback": "" if passed else "fix it",
            "verdict": "pass" if passed else "fail",
        }
    )


def profiled() -> str:
    """A profiler's reply."""
    return json.dumps({"analysis": "a", "bottlenecks": "b", "suggestions": "s"})


@dataclass
class EvolveExecutors(Executors):
    """The dynamic script's executors, answering the evolve search's roles and operations."""

    mutator: deque[str | Answer] = field(default_factory=deque)

    def _turn(self, request: DispatchTurn | ResumeSessionTurn) -> Answer:
        role = request.turn.session.role_id.root.removeprefix("evolve-")
        queue = {"mutator": self.mutator, "judge": self.judge, "profiler": self.profiler}[role]
        if not queue and self.exhausted is None:
            return Unknown()
        reply = queue.popleft() if queue else self.exhausted
        assert reply is not None
        if not isinstance(reply, str):
            return reply
        return Succeeded(output_json=reply, resource_id=lease(request.turn.session.session_id))

    def _operation(self, request: ExecuteRegisteredOperation) -> Answer:
        typed = OPERATIONS.registry().decode(request.operation)
        if isinstance(typed, VerifyParentRevision):
            return Succeeded(outcome=ParentVerification(status="succeeded", verified=True))
        if isinstance(typed, ExportPatch):
            return Succeeded(
                outcome=PatchExport(status="succeeded", patch=f"patch {typed.revision}")
            )
        if isinstance(typed, InterpretEvidence):
            return Succeeded(
                outcome=EvidenceReadings(
                    status="succeeded",
                    readings=tuple(self.readings[item.evidence_id] for item in typed.evidence),
                )
            )
        assert isinstance(typed, RenderRoleArtifacts)
        return Succeeded(
            outcome=RenderedArtifacts(
                status="succeeded",
                prompts=(
                    ArtifactRef(
                        artifact_id=ArtifactId(root=f"prompt:{request.operation_id.root}"),
                        digest="prompt",
                    ),
                ),
            )
        )
