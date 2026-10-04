"""Measurement identity is independent of the requesting workspace."""

from __future__ import annotations

import string

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vibesys.orchestration.dynamic import PLUGIN
from vibesys.run.evaluation_backend import SemanticEvaluationBackend, SemanticEvaluationIdentity
from vs_evaluation.api import ContentDigest, EvidenceKind
from vs_evaluation.api.testing import FakeClock, FakeEvaluationExecutor, InMemoryEvaluationNamespace
from vs_runtime.api.testing import FakeRun


class _OwnedFakeExecutor(FakeEvaluationExecutor):
    """Expose the owned executor cleanup required by the product backend."""

    async def close(self) -> None:
        """The shared Fake holds only in-memory resources."""


@pytest.mark.asyncio
@settings(max_examples=15)
@given(
    scopes=st.lists(
        st.text(alphabet=string.ascii_letters + string.digits + "_-", min_size=1, max_size=16),
        min_size=2,
        max_size=5,
        unique=True,
    ),
    patch=st.text(alphabet=st.characters(min_codepoint=0, max_codepoint=127), max_size=128),
    kinds=st.sampled_from(
        (
            (EvidenceKind.ACCURACY,),
            (EvidenceKind.BENCHMARK,),
            (EvidenceKind.PROFILE,),
            (EvidenceKind.ACCURACY, EvidenceKind.BENCHMARK),
        )
    ),
)
async def test_identical_measurements_join_across_generated_requester_scopes(
    scopes: list[str], patch: str, kinds: tuple[EvidenceKind, ...]
) -> None:
    run = FakeRun(PLUGIN, supports_parallel_candidates=True)
    executor = _OwnedFakeExecutor(
        clock=FakeClock(), supported_evidence_kinds=tuple(kind.value for kind in EvidenceKind)
    )
    identity = ContentDigest.sha256(b"same evaluator, workload and environment")
    backend = SemanticEvaluationBackend(
        run.evaluation,
        run.workspaces,
        InMemoryEvaluationNamespace(),
        SemanticEvaluationIdentity(evaluator=identity, workload=identity, environment=identity),
        executor=executor,
        submitted_time=lambda: 0.0,
    )
    handles: list[str] = []
    revisions: list[str] = []
    try:
        for scope in scopes:
            revision = await run.workspaces.root.snapshot("unchanged measured content")
            run.workspaces.set_patch(revision, patch)
            revisions.append(revision)
            submitted = await backend.submit_revision_evidence(revision, kinds, scope_id=scope)
            handles.append(submitted.handle_id)

        assert len(set(revisions)) == len(scopes)
        assert len(set(handles)) == 1
        assert len(executor.submissions) == 1
        record = await backend.recorded_snapshot(handles[0])
        assert record.request.owner_scope == scopes[0]
        assert executor.submissions[0].request == record.request
    finally:
        for handle_id in set(handles):
            await backend.cancel(handle_id)
        await backend.close()
        await run.close()
