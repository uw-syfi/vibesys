"""Any context field holding evidence references reaches its template as ledger readings."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from vs_core.api import (
    EvidenceId,
    EvidenceKind,
    EvidenceRef,
    ObservationStatus,
    OperationRequest,
    RequestId,
    RevisionId,
    RevisionRef,
    RunId,
    Scope,
    Until,
    Value,
)
from vs_evaluation.api import (
    ContentDigest,
    EvidenceFingerprints,
    EvidenceMetric,
    EvidenceOutcome,
    TrustedEvidence,
)
from vs_evaluation.api import EvidenceKind as StageKind
from vs_project.api import Project
from vs_prompts.api import TemplateRenderer
from vs_runtime.api import ArtifactStore
from vs_runtime.api.core import ExecutionContext, RenderArtifactsOwner
from vs_runtime.api.testing import FakeEvidenceLedger
from vs_sim.api.testing import VirtualClock, run_virtual

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import BaseModel

_DIGEST = ContentDigest(value="a" * 64)
_REVISION = RevisionRef(revision_id=RevisionId(root="cand"), digest="d")


class _Facts(Value):
    """A context whose evidence fields have different names, and one that is not evidence."""

    evidence: tuple[EvidenceRef, ...] = ()
    anything_else: tuple[EvidenceRef, ...] = ()
    names: tuple[str, ...] = ()


class _Request:
    template = "t.j2"
    subject = "s"
    ordinal = 1

    def __init__(self, context: BaseModel) -> None:
        self.context = context


def _held() -> tuple[EvidenceRef, TrustedEvidence]:
    evidence = TrustedEvidence(
        evidence_id="b" * 64,
        evaluation_id="evaluation",
        stage_name="benchmark",
        kind=StageKind.BENCHMARK,
        fingerprints=EvidenceFingerprints(
            candidate=_DIGEST, evaluator=_DIGEST, workload=_DIGEST, environment=_DIGEST
        ),
        trusted_inputs=_DIGEST,
        outcome=EvidenceOutcome.PASSED,
        metrics=(EvidenceMetric(name="rate", value=3001.7, direction="max", unit="calls/s"),),
        accepted_round=0,
    )
    ref = EvidenceRef(
        evidence_id=EvidenceId(root=evidence.evidence_id),
        kind=EvidenceKind.BENCHMARK,
        purpose="local-validation",
        scope=Scope(owner=RunId(root="run"), generation=0),
        source_request=RequestId(root="request"),
        candidate=_REVISION,
        observation_sequence=1,
        evaluator_digest="e",
        workload_digest="w",
        environment_digest="n",
        provenance="trusted",
        status=ObservationStatus.SUCCEEDED,
    )
    return ref, evidence


def _render(tmp_path: Path, template: str, context: BaseModel) -> str:
    ref, evidence = _held()
    roots = tmp_path / "templates"
    roots.mkdir()
    (roots / "t.j2").write_text(template)
    project = tmp_path / "project"
    project.mkdir()
    store = ArtifactStore(Project.open(project).state.state_store_namespace("render"))
    ledger = FakeEvidenceLedger()
    ledger.record(ref.source_request, evidence, ref.purpose)
    owner = RenderArtifactsOwner(TemplateRenderer(roots), store, None, ledger, {})
    fence = ExecutionContext(
        now_at=1.0,
        payload_digest="d",
        until=Until(at=100.0),
    )
    request = cast("OperationRequest", _Request(context))
    outcome = run_virtual(VirtualClock(0.0), owner.execute(request, fence))
    assert outcome["status"] == "succeeded", outcome
    prompts = cast("tuple[Any, ...]", outcome["prompts"])
    (artifact,) = prompts
    return (store.read_digest(artifact.digest) or b"").decode()


def test_every_field_of_evidence_references_is_read_from_the_ledger(tmp_path: Path) -> None:
    ref, _ = _held()
    context = _Facts(evidence=(ref,), anything_else=(ref,), names=("x",))

    text = _render(
        tmp_path,
        "{% for e in evidence %}{{ e.metrics[0].value }} {{ e.passed }}{% endfor %}"
        "|{% for e in anything_else %}{{ e.metrics[0].unit }}{% endfor %}|{{ names | join(',') }}",
        context,
    )

    assert text == "3001.7 True|calls/s|x"
