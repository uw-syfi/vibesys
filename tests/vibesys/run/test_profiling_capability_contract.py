"""The Fake evaluation reports the same profiling capability as the production one it replaces.

Policy offers profile workstreams only when ``Evaluation.can_profile`` holds, so
a Fake that claims more than production would let a test pass a loop that
production would run differently. Each case pairs a production evaluation for
one run environment with the Fake configuration tests use for that environment.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vibesys.orchestration.dynamic import PLUGIN
from vibesys.run.evaluation_backend import (
    EvidenceReusingEvaluation,
    SemanticEvaluationBackend,
    SemanticEvaluationIdentity,
)
from vs_evaluation.api import (
    ContentDigest,
    ProfilerAgentService,
    ProfilerAgentServiceHooks,
    TrustedEvidence,
)
from vs_evaluation.api.testing import FakeProfilerTurnProvision
from vs_project.api import StateNamespace
from vs_runtime.api.testing import FakeEvaluation, FakeRun

if TYPE_CHECKING:
    from pathlib import Path


def _namespace(tmp_path: Path, name: str) -> StateNamespace:
    root = tmp_path / ".vibesys" / "state" / name
    root.mkdir(parents=True)
    return StateNamespace(project_root=tmp_path, root=root, portable=False)


def _identity() -> SemanticEvaluationIdentity:
    digest = ContentDigest.sha256(b"identity")
    return SemanticEvaluationIdentity(evaluator=digest, workload=digest, environment=digest)


async def _snapshot(scope: str | None) -> str:
    return f"snapshot:{scope}"


async def _no_evidence(
    _principal: str, _scope: str | None, _snapshot: str, _ids: tuple[str, ...]
) -> tuple[TrustedEvidence, ...]:
    return ()


def _profiler(tmp_path: Path) -> ProfilerAgentService:
    return ProfilerAgentService(
        FakeProfilerTurnProvision(),
        _namespace(tmp_path, "profiler-agent"),
        ProfilerAgentServiceHooks(candidate_snapshot=_snapshot, resolve_evidence=_no_evidence),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("profiler", [True, False], ids=["profiler", "no-profiler"])
async def test_the_local_executor_and_the_default_fake_cannot_profile(
    tmp_path: Path, *, profiler: bool
) -> None:
    """The local semantic executor produces no profile evidence, with or without a profiler."""
    run = FakeRun(PLUGIN, project_root=tmp_path, supports_parallel_candidates=True)
    backend = SemanticEvaluationBackend(
        run.evaluation, run.workspaces, _namespace(tmp_path, "evaluation-agent"), _identity()
    )

    async def no_handles(_scope: str | None) -> tuple[str, ...]:
        return ()

    production = EvidenceReusingEvaluation(
        run.evaluation,
        backend,
        run_id=run.run_id,
        scope_handles=no_handles,
        profiler=_profiler(tmp_path) if profiler else None,
    )
    try:
        assert await production.can_profile() is False
        assert await production.can_profile() == await FakeEvaluation().can_profile()
    finally:
        await backend.close()
