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
from vibesys.run.slurm_evaluation import SlurmSemanticEvaluationExecutor
from vs_evaluation.api import (
    ContentDigest,
    ProfilerAgentService,
    ProfilerAgentServiceHooks,
    TrustedEvidence,
)
from vs_evaluation.api.testing import FakeProfilerTurnProvision
from vs_project.api import StateNamespace
from vs_runtime.api.infrastructure import TrustedEvaluationPlan
from vs_runtime.api.testing import FakeEvaluation, FakeRun
from vs_sandbox.api.slurm import SlurmEvaluationPlan, SlurmExecutionPolicy
from vs_slurm.api import SlurmConfig, SlurmSshTransport

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


@pytest.mark.asyncio
@pytest.mark.parametrize("capture", [True, False], ids=["capture", "no-capture"])
async def test_the_slurm_executor_and_its_fake_agree_on_profiling(
    tmp_path: Path, *, capture: bool
) -> None:
    """A Slurm run profiles exactly when its plan carries the trusted capture.

    The loop harness's profiled input stands for the capture case and a
    ``FakeEvaluation(profiling_supported=True)`` stands for it in unit tests.
    """
    run = FakeRun(PLUGIN, project_root=tmp_path, supports_parallel_candidates=True)
    config = SlurmConfig(
        name="test", remote_workspace_root="/runs", transport=SlurmSshTransport(host="test")
    )
    namespace = _namespace(tmp_path, "evaluation-agent")
    executor = SlurmSemanticEvaluationExecutor(
        config,
        SlurmExecutionPolicy(),
        SlurmEvaluationPlan(
            config_path=tmp_path / "slurm.toml",
            benchmark_command=("python", "benchmark.py"),
            profile_command=("python", "rocprof_profiler/remote_capture.py") if capture else None,
        ),
        TrustedEvaluationPlan(accuracy_command="unused", benchmark_command="unused"),
        run.workspaces,
        namespace,
        tmp_path / "handles",
    )
    backend = SemanticEvaluationBackend(
        run.evaluation, run.workspaces, namespace, _identity(), executor=executor
    )

    async def no_handles(_scope: str | None) -> tuple[str, ...]:
        return ()

    production = EvidenceReusingEvaluation(
        run.evaluation,
        backend,
        run_id=run.run_id,
        scope_handles=no_handles,
        profiler=_profiler(tmp_path),
    )
    try:
        assert await production.can_profile() is capture
        fake = FakeEvaluation(profiling_supported=capture)
        assert await production.can_profile() == await fake.can_profile()
    finally:
        await backend.close()
