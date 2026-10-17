"""A prompt that cites trusted evidence states what the framework measured.

Regression: a resumed implementer and the judges were told only opaque evidence ids, so a
trusted benchmark of 3001.7 calls/s was reported "inconclusive" and the judge never saw the
number. For any trusted outcome, the rendered prompt now carries the measured value, its unit,
its direction and the stage verdict, read from the evidence ledger.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.vibesys.domain_sections import EMPTY_DOMAIN_SECTIONS

from vibesys.orchestration.evolve.core_policy.api import prompts as evolve_prompts
from vibesys.orchestration.evolve.search.api import JudgePrompt as EvolveJudgePrompt
from vibesys.orchestration.evolve.search.api import evolve
from vibesys.prompts import CORE_PROTOCOL_DIR
from vs_core.api import (
    ArtifactRef,
    EvidenceId,
    EvidenceKind,
    EvidenceRef,
    HostFence,
    HostId,
    ObservationStatus,
    RequestId,
    ResumeFacts,
    RevisionId,
    RevisionRef,
    RunId,
    Scope,
    Until,
)
from vs_evaluation.api import (
    ContentDigest,
    EvaluationStageOutcome,
    EvidenceFingerprints,
    EvidenceMetric,
    EvidenceOutcome,
    TrustedEvidence,
)
from vs_evaluation.api import EvidenceKind as StageKind
from vs_project.api import Project
from vs_prompts.api import TemplateRenderer
from vs_runtime.api import ArtifactStore, RunFacts
from vs_runtime.api.core import ExecutionContext, RenderArtifactsOwner
from vs_runtime.api.testing import FakeEvidenceLedger
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from pydantic import BaseModel

    from vs_core.api import OperationRequest

REVISION = RevisionRef(revision_id=RevisionId(root="cand"), digest="d")
SCOPE = Scope(owner=RunId(root="run"), generation=0)
_DIGEST = ContentDigest(value="a" * 64)


_FACTS = RunFacts(agent_git_writable=True, domain_id="generic", objective="Make it faster.")
_RUN = {
    "facts": _FACTS,
    "objective": _FACTS.objective,
    "environment_notes": "",
    "agent_evaluation": True,
    "domain_sections": EMPTY_DOMAIN_SECTIONS,
}


class _Request:
    subject = "implementer"
    ordinal = 1

    def __init__(self, context: BaseModel) -> None:
        self.context = context
        # A search's prompt context names its template; core's exchanges name theirs by kind.
        dumped = context.model_dump()
        self.template = f"{dumped.get('template', dumped.get('kind'))}.j2"


def _record(
    value: float, unit: str, direction: Literal["max", "min"], *, passed: bool
) -> tuple[EvidenceRef, TrustedEvidence]:
    evidence = TrustedEvidence(
        evidence_id="b" * 64,
        evaluation_id="evaluation",
        stage_name="benchmark",
        kind=StageKind.BENCHMARK,
        fingerprints=EvidenceFingerprints(
            candidate=_DIGEST, evaluator=_DIGEST, workload=_DIGEST, environment=_DIGEST
        ),
        trusted_inputs=_DIGEST,
        outcome=EvidenceOutcome.PASSED if passed else EvidenceOutcome.FAILED,
        metrics=(EvidenceMetric(name="throughput", value=value, direction=direction, unit=unit),),
        accepted_round=0,
    )
    ref = EvidenceRef(
        evidence_id=EvidenceId(root=evidence.evidence_id),
        kind=EvidenceKind.LOCAL_VALIDATION,
        purpose="local-validation",
        scope=SCOPE,
        source_request=RequestId(root="request"),
        candidate=REVISION,
        observation_sequence=1,
        evaluator_digest="e",
        workload_digest="w",
        environment_digest="n",
        provenance="trusted",
        status=ObservationStatus.SUCCEEDED,
    )
    return ref, evidence


# The reply types of the strategies whose prompts cite evidence; their schema names differ.
_REPLIES = dict(evolve.reply_schemas)


def _render(
    root: Path,
    roots: tuple[Path, ...],
    context: BaseModel,
    held: tuple[EvidenceRef, TrustedEvidence],
    variables: Mapping[str, object] | None = None,
) -> str:
    ref, evidence = held
    project = root / "project"
    project.mkdir()
    store = ArtifactStore(Project.open(project).state.state_store_namespace("render"))
    ledger = FakeEvidenceLedger()
    ledger.record(ref.source_request, evidence, ref.purpose)
    owner = RenderArtifactsOwner(
        TemplateRenderer(roots[0], fallback_roots=roots[1:]),
        store,
        variables,
        ledger,
        _REPLIES,
    )
    fence = ExecutionContext(
        fence=HostFence(host_id=HostId(root="host"), epoch=1),
        now_at=1.0,
        payload_digest="d",
        until=Until(at=100.0),
    )
    outcome = asyncio.run(owner.execute(cast("OperationRequest", _Request(context)), fence))
    assert outcome["status"] == "succeeded", outcome
    (artifact,) = cast("tuple[ArtifactRef, ...]", outcome["prompts"])
    return (store.read_digest(artifact.digest) or b"").decode()


def _cases() -> dict[str, Callable[..., str]]:
    def core(root: Path, ref: EvidenceRef, evidence: TrustedEvidence) -> str:
        context = ResumeFacts(
            agent="implementer", retained=REVISION, ended_by=None, evidence=(ref,)
        )
        return _render(root, (CORE_PROTOCOL_DIR,), context, (ref, evidence))

    def evolve_judge(root: Path, ref: EvidenceRef, evidence: TrustedEvidence) -> str:
        context = EvolveJudgePrompt(candidate=REVISION, cited_evidence=(ref,))
        return _render(
            root,
            (Path(evolve_prompts.__file__).parent, CORE_PROTOCOL_DIR),
            context,
            (ref, evidence),
            _RUN,
        )

    return {
        "single/multi/dynamic/evolve/issue_queue resume": core,
        "evolve judge": evolve_judge,
    }


@pytest.mark.parametrize("name", sorted(_cases()))
@settings(
    max_examples=examples(pr=10, nightly=15),
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    value=st.floats(0.1, 1e6, allow_nan=False).map(lambda x: round(x, 1)),
    unit=st.sampled_from(["calls/s", "tokens/s", "ms", "req/min"]),
    direction=st.sampled_from(["max", "min"]),
    passed=st.booleans(),
)
def test_a_prompt_citing_trusted_evidence_states_value_unit_direction_and_verdict(
    name: str, value: float, unit: str, direction: Literal["max", "min"], *, passed: bool
) -> None:
    ref, evidence = _record(value, unit, direction, passed=passed)
    with tempfile.TemporaryDirectory() as raw:
        text = _cases()[name](Path(raw), ref, evidence)
    assert f"{value} {unit}" in text
    assert f"better: {direction}" in text
    assert ("PASS" if passed else "FAIL") in text


@settings(max_examples=examples(pr=10, nightly=30), deadline=None)
@given(
    value=st.floats(0.1, 1e6, allow_nan=False).map(lambda x: round(x, 1)),
    unit=st.sampled_from(["calls/s", "tokens/s", "ms"]),
    direction=st.sampled_from(["max", "min"]),
    passed=st.booleans(),
)
def test_the_tool_reply_stage_carries_the_recorded_measurement(
    value: float, unit: str, direction: Literal["max", "min"], *, passed: bool
) -> None:
    _, evidence = _record(value, unit, direction, passed=passed)
    stage = EvaluationStageOutcome.of(evidence)
    (metric,) = stage.metrics
    assert (metric.value, metric.unit, metric.direction) == (value, unit, direction)
    assert stage.outcome is (EvidenceOutcome.PASSED if passed else EvidenceOutcome.FAILED)
    assert stage.evidence_id == evidence.evidence_id
