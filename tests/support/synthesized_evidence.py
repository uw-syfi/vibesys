"""An evidence lookup for tests whose executors fabricate core evidence without a ledger.

Scripted executors answer with evidence refs whose ids are plain labels, and record no
``TrustedEvidence``. A bridge test that does not care what was measured reads those refs through
this lookup, which states a passing, metric-free record for any key. Tests of what a record
says use ``FakeEvidenceLedger`` fed through its producer port instead.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from vs_evaluation.api import (
    ContentDigest,
    EvidenceFingerprints,
    EvidenceKind,
    EvidenceOutcome,
    TrustedEvidence,
)
from vs_runtime.api.core import EvidenceEntry

if TYPE_CHECKING:
    from vs_core.api import EvidenceKey

_DIGEST = ContentDigest(value="0" * 64)


class SynthesizedEvidence:
    """A lookup that has a passing record for every key it is asked about."""

    def lookup(self, key: EvidenceKey) -> EvidenceEntry:
        """A passing benchmark record that carries the key's evidence id as its identity."""
        identity = f"{key.source_request.root}\0{key.evidence_id.root}"
        return EvidenceEntry(
            evidence=TrustedEvidence(
                evidence_id=hashlib.sha256(identity.encode()).hexdigest(),
                evaluation_id=key.source_request.root,
                stage_name="benchmark",
                kind=EvidenceKind.BENCHMARK,
                fingerprints=EvidenceFingerprints(
                    candidate=_DIGEST, evaluator=_DIGEST, workload=_DIGEST, environment=_DIGEST
                ),
                trusted_inputs=_DIGEST,
                outcome=EvidenceOutcome.PASSED,
                accepted_round=0,
            ),
            purpose="local-validation",
        )
