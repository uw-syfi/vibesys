"""Workspace acquisition faults settle the real semantic producer."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vibesys.run.evaluation_backend import SemanticEvaluationBackend, SemanticEvaluationIdentity
from vs_evaluation.api import ContentDigest, EvaluationState, EvidenceKind
from vs_evaluation.api.testing import InMemoryEvaluationNamespace
from vs_runtime.api import AgentEvaluationStatus, RuntimeContractError
from vs_runtime.api.testing import FakeEvaluation, FakeWorkspace, FakeWorkspaces

if TYPE_CHECKING:
    from vs_runtime.api import CandidateWorkspace


class _FailingCreationWorkspaces(FakeWorkspaces):
    """Fail one owned boundary before allocating a candidate workspace."""

    def __init__(self, root: FakeWorkspace, error: Exception) -> None:
        super().__init__(root, supports_parallel_candidates=True)
        self.error = error
        self.creation_finished = asyncio.Event()

    async def create_candidate(
        self, from_revision: str | None = None, *, member_id: str | None = None
    ) -> CandidateWorkspace:
        del from_revision, member_id
        try:
            raise self.error
        finally:
            # There is no await between this signal and propagation to the
            # producer. Its synchronous terminal publication happens before a
            # waiter resumes, so no timing or scheduling guess is needed.
            self.creation_finished.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [OSError, RuntimeContractError])
@settings(max_examples=12)
@given(message=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789 ", min_size=1, max_size=80))
async def test_candidate_creation_failure_settles_the_real_producer(
    error_type: type[Exception], message: str
) -> None:
    with TemporaryDirectory(prefix="candidate-creation-failure-") as directory:
        workspaces = _FailingCreationWorkspaces(
            FakeWorkspace(path=Path(directory) / "project"), error_type(message)
        )
        backend = SemanticEvaluationBackend(
            FakeEvaluation(),
            workspaces,
            InMemoryEvaluationNamespace(),
            SemanticEvaluationIdentity(
                evaluator=ContentDigest.sha256(b"evaluator"),
                workload=ContentDigest.sha256(b"workload"),
                environment=ContentDigest.sha256(b"environment"),
            ),
            submitted_time=lambda: 100.0,
        )
        async with AsyncExitStack() as cleanup:
            cleanup.push_async_callback(workspaces.close)
            cleanup.push_async_callback(backend.close)
            revision = await workspaces.root.snapshot("candidate")
            submitted = await backend.submit_revision_evidence(
                revision,
                (EvidenceKind.ACCURACY, EvidenceKind.BENCHMARK),
                scope_id="candidate",
            )
            await workspaces.creation_finished.wait()
            assert await backend.status(submitted.handle_id) is EvaluationState.FAILED
            record = await backend.recorded_snapshot(submitted.handle_id)
            assert record.failure == message
            assert not record.stage_results
            assert not workspaces.candidates
            (projection,) = await backend.agent_evaluations((submitted.handle_id,))
            assert projection.status is AgentEvaluationStatus.FAILED
            assert not projection.stages
            assert not await backend.evidence_for(
                workspaces.root, (EvidenceKind.ACCURACY, EvidenceKind.BENCHMARK)
            )
