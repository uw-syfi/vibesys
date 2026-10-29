"""Render a prompt context the way the render operation does, with a trusted evidence ledger.

The render owner derives the ``reply`` variables from the role's reply model and states each
cited ``EvidenceRef`` from the run's evidence ledger. A test that needs either renders through
it instead of re-deriving them.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

from vs_core.api import (
    EvidenceId,
    EvidenceKind,
    EvidenceRef,
    HostFence,
    HostId,
    ObservationStatus,
    RequestId,
    RevisionId,
    RevisionRef,
    RunId,
    Scope,
    Until,
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

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from pydantic import BaseModel

    from vs_core.api import ArtifactRef, OperationRequest, SchemaRef

REVISION = RevisionRef(revision_id=RevisionId(root="cand"), digest="d")
SCOPE = Scope(owner=RunId(root="run"), generation=0)
_DIGEST = ContentDigest(value="a" * 64)

type Held = tuple[EvidenceRef, TrustedEvidence]
"""A cited evidence record and the trusted record the ledger holds for it."""


def trusted_record(
    value: float, unit: str, direction: Literal["max", "min"], *, passed: bool, evidence_id: str
) -> Held:
    """One benchmark record: the ``EvidenceRef`` a prompt cites and the ledger's record of it."""
    evidence = TrustedEvidence(
        evidence_id=evidence_id,
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
        kind=EvidenceKind.BENCHMARK,
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


class _Request:
    ordinal = 1

    def __init__(self, subject: str, context: BaseModel) -> None:
        self.subject = subject
        self.context = context
        # A search's prompt context names its template; core's exchanges name theirs by kind.
        dumped = context.model_dump()
        self.template = f"{dumped.get('template', dumped.get('kind'))}.j2"


@dataclass(frozen=True)
class PromptRender:
    """A strategy's way of rendering: its template roots, reply models and run-level variables."""

    templates: tuple[Path, ...]
    """The strategy's own template directory first, then the directories it falls back to."""
    replies: Mapping[SchemaRef, type[BaseModel]]
    variables: Mapping[str, object] | None = None

    def render(
        self,
        root: Path,
        context: BaseModel,
        *,
        subject: str = "implementer",
        held: Held | None = None,
    ) -> str:
        """The text the render operation produces for ``context``, in a project under ``root``."""
        project = root / "project"
        project.mkdir()
        store = ArtifactStore(Project.open(project).state.state_store_namespace("render"))
        ledger = FakeEvidenceLedger()
        if held is not None:
            ref, evidence = held
            ledger.record(ref.source_request, evidence, ref.purpose)
        owner = RenderArtifactsOwner(
            TemplateRenderer(self.templates[0], fallback_roots=self.templates[1:]),
            store,
            self.variables,
            ledger,
            self.replies,
        )
        fence = ExecutionContext(
            fence=HostFence(host_id=HostId(root="host"), epoch=1),
            now_at=1.0,
            payload_digest="d",
            until=Until(at=100.0),
        )
        request = cast("OperationRequest", _Request(subject, context))
        outcome = asyncio.run(owner.execute(request, fence))
        assert outcome["status"] == "succeeded", outcome
        (artifact,) = cast("tuple[ArtifactRef, ...]", outcome["prompts"])
        return (store.read_digest(artifact.digest) or b"").decode()
