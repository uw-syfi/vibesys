"""Local semantic evaluation preserves the policy-selected benchmark axes."""

from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from vs_evaluation.api import (
    ContentDigest,
    EvaluationRequest,
    EvaluationStep,
    EvidenceFingerprints,
    EvidenceKind,
    SemanticEvaluationStage,
)
from vs_runtime.api import BenchmarkObjective, MetricDirection, PollingEvaluationExecutor
from vs_runtime.api.testing import FakeEvaluation, FakeWorkspace, FakeWorkspaces
from vs_sim.api.testing import VirtualClock, run_virtual


@given(
    st.lists(
        st.tuples(
            st.text(alphabet="abcdef_", min_size=1, max_size=20),
            st.sampled_from(list(MetricDirection)),
        ),
        unique_by=lambda pair: pair[0],
        max_size=5,
    )
)
def test_every_selected_axis_reaches_the_benchmark(axes: list[tuple[str, MetricDirection]]) -> None:
    objectives = tuple(
        BenchmarkObjective(name=name, direction=direction) for name, direction in axes
    )

    async def exercise() -> None:
        evaluation = FakeEvaluation()
        workspaces = FakeWorkspaces(
            FakeWorkspace(path=Path("/workspace")), supports_parallel_candidates=True
        )
        snapshot = await workspaces.root.snapshot("candidate")
        executor = PollingEvaluationExecutor(evaluation, workspaces, objectives=objectives)
        digest = ContentDigest.sha256(b"trusted")
        stage = SemanticEvaluationStage(
            snapshot=snapshot,
            kind=EvidenceKind.BENCHMARK,
            fingerprints=EvidenceFingerprints(
                candidate=digest, evaluator=digest, workload=digest, environment=digest
            ),
        )
        request = EvaluationRequest(
            key="measurement",
            stages=(EvaluationStep(name="benchmark", payload=stage.model_dump(mode="json")),),
        )
        gate = evaluation.gate("benchmark", 0)
        try:
            await executor.submit(request, handle_id="measurement")
            await gate.entered.wait()
            assert evaluation.benchmark_calls[0].objectives == objectives
        finally:
            gate.release()
            await executor.close()

    run_virtual(VirtualClock(), exercise())
