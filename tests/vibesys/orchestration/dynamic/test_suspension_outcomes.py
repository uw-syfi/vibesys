"""Evaluation stage outcomes remain diagnostic without promoting unaccepted evidence."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic.test_plugin_suspension import _open

from vibesys.orchestration.dynamic import PLUGIN
from vibesys.orchestration.dynamic.agents import IMPLEMENTER
from vibesys.orchestration.dynamic.models import DynamicState
from vibesys.run.evaluation_backend import SemanticEvaluationBackend, SemanticEvaluationIdentity
from vs_evaluation.api import (
    ArtifactDigest,
    ContentDigest,
    EvaluationState,
    EvaluationStepResult,
    EvidenceKind,
    StageState,
    StoredEvaluation,
    TrustedEvidence,
)
from vs_evaluation.api.testing import InMemoryEvaluationNamespace
from vs_runtime.api import (
    AccuracyEvaluation,
    AgentToolBindingContext,
    BenchmarkEvaluation,
    RuntimeContractError,
)
from vs_runtime.api.testing import FakeRun

_FAILURE = "quick benchmark prefix-cache preflight: cached_tokens=0"


async def _produce(root: Path, *, semantic_failure: bool) -> StoredEvaluation:
    producer_run = FakeRun(PLUGIN, project_root=root, supports_parallel_candidates=True)
    revision = await producer_run.workspaces.root.snapshot("root")
    candidate = await producer_run.workspaces.create_candidate(revision, member_id="held")
    producer_run.evaluation.script_accuracy(AccuracyEvaluation(executed=True))
    producer_run.evaluation.script_benchmark(
        BenchmarkEvaluation(executed=True, feedback=_FAILURE if semantic_failure else None)
    )
    digest = ContentDigest.sha256(b"immutable capture")
    backend = SemanticEvaluationBackend(
        producer_run.evaluation,
        producer_run.workspaces,
        InMemoryEvaluationNamespace(),
        SemanticEvaluationIdentity(evaluator=digest, workload=digest, environment=digest),
    )
    backend.bind(AgentToolBindingContext(IMPLEMENTER, candidate, "held", str))
    submitted = await backend.submit_revision_evidence(
        revision, (EvidenceKind.ACCURACY, EvidenceKind.BENCHMARK), scope_id=candidate.id
    )
    await backend.await_result(submitted.handle_id, 10)
    produced = await backend.recorded_snapshot(submitted.handle_id)
    produced = produced.model_copy(
        update={"request": produced.request.model_copy(update={"stop_on_failure": False})}
    )
    await backend.close()
    return produced


async def _exercise(
    root: Path,
    stages: tuple[StageState | None, StageState | None],
    *,
    semantic_failure: bool,
    accepted: bool,
) -> None:
    produced = await _produce(root, semantic_failure=semantic_failure)
    opened = await _open(root, submission=produced)
    assert opened.handle == produced.handle_id
    try:
        task = opened.start()
        waiting = await opened.waiting(task)
        digest = ContentDigest.sha256(b"immutable capture")
        results = []
        evidence_ids = []
        artifacts = []
        for index, (kind, state) in enumerate(
            zip((EvidenceKind.ACCURACY, EvidenceKind.BENCHMARK), stages, strict=True)
        ):
            if state is None:
                continue
            evidence = None
            if state in {StageState.SUCCEEDED, StageState.FAILED}:
                source = TrustedEvidence.model_validate(produced.stage_results[index].result)
                if state is StageState.SUCCEEDED:
                    evidence_ids.append(source.evidence_id)
                artifact = f"{kind.value}/result.json"
                if state is StageState.SUCCEEDED:
                    artifacts.append(artifact)
                evidence = source.model_copy(
                    update={"artifacts": (ArtifactDigest(path=artifact, digest=digest),)}
                )
            results.append(
                EvaluationStepResult(
                    name=kind.value,
                    state=state,
                    failure=_FAILURE if state is StageState.FAILED else None,
                    result=evidence.model_dump(mode="json") if evidence is not None else None,
                )
            )
        evaluation_state = (
            EvaluationState.FAILED
            if StageState.FAILED in stages
            else EvaluationState.CANCELED
            if StageState.CANCELED in stages or None in stages
            else EvaluationState.SUCCEEDED
        )
        opened.evaluation.accepted_evidence[opened.handle] = tuple(evidence_ids) if accepted else ()
        opened.evaluations.executor.set_state(
            opened.handle,
            evaluation_state,
            stage_results=tuple(results),
            failure=_FAILURE if evaluation_state is EvaluationState.FAILED else None,
        )
        await opened.evaluations.coordinator.status(opened.handle)
        report = await opened.evaluations.coordinator.recorded_snapshot(opened.handle)
        opened.evaluation.submitted_reports[opened.handle] = report.model_dump_json()
        if StageState.SUCCEEDED in stages and not accepted:
            with pytest.raises(RuntimeContractError, match="unaccepted"):
                await task
            assert len(opened.calls) == 1
            return
        await task
        final = await opened.run.state.load(DynamicState)
        assert final is not None
        assert len(final.search.rounds) == 1
        assert final.workstreams[0].budget == waiting.workstreams[0].budget
        assert len(opened.calls) == 2
        message = opened.calls[-1].message
        expected_status = (
            "failed"
            if semantic_failure or evaluation_state is EvaluationState.FAILED
            else "canceled"
            if evaluation_state is EvaluationState.CANCELED
            else "passed"
        )
        assert f"Terminal status: {expected_status}" in message
        _assert_resume_trust(message, stages, evidence_ids, artifacts, accepted=accepted)
        if semantic_failure or StageState.FAILED in stages:
            assert _FAILURE in message
        assert opened.calls[-1].expected_provider_session_id is not None
    finally:
        opened.client.close()


def _assert_resume_trust(
    message: str,
    stages: tuple[StageState | None, StageState | None],
    evidence_ids: list[str],
    artifacts: list[str],
    *,
    accepted: bool,
) -> None:
    references = json.loads(message.split("Artifact references: ", 1)[1].splitlines()[0])
    assert references == artifacts
    trusted_ids = json.loads(message.split("Evidence IDs: ", 1)[1].splitlines()[0])
    assert trusted_ids == (evidence_ids if accepted else [])
    trusted_detail = message.split("Trusted terminal result:\n", 1)[1].splitlines()[0]
    trusted = json.loads(trusted_detail)
    for stage in trusted["stage_results"]:
        if stage["state"] != StageState.SUCCEEDED.value:
            assert stage["result"] is None
    if StageState.FAILED in stages:
        diagnostics = message.split("Unaccepted stage diagnostics", 1)[1]
        for kind, state in zip(
            (EvidenceKind.ACCURACY, EvidenceKind.BENCHMARK), stages, strict=True
        ):
            if state is StageState.FAILED:
                assert f"{kind.value}/result.json" in diagnostics


def test_r22_accuracy_passed_and_benchmark_preflight_failed_resume_same_session(
    tmp_path: Path,
) -> None:
    """Both execution stages succeeded; the benchmark semantic evidence failed."""
    asyncio.run(
        _exercise(
            tmp_path,
            (StageState.SUCCEEDED, StageState.SUCCEEDED),
            semantic_failure=True,
            accepted=True,
        )
    )


@settings(max_examples=32)
@example(stages=(StageState.SUCCEEDED, StageState.FAILED), accepted=True)
@example(stages=(StageState.SUCCEEDED, StageState.SUCCEEDED), accepted=False)
@example(stages=(None, StageState.CANCELED), accepted=True)
@given(
    stages=st.tuples(
        st.sampled_from((StageState.SUCCEEDED, StageState.FAILED, StageState.CANCELED, None)),
        st.sampled_from((StageState.SUCCEEDED, StageState.FAILED, StageState.CANCELED, None)),
    ),
    accepted=st.booleans(),
)
def test_stage_outcomes_and_acceptance_preserve_resume_trust(
    stages: tuple[StageState | None, StageState | None], *, accepted: bool
) -> None:
    with TemporaryDirectory() as directory:
        asyncio.run(_exercise(Path(directory), stages, semantic_failure=False, accepted=accepted))
