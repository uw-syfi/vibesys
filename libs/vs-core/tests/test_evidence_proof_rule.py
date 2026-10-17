"""Only the framework's own measurement of a revision is proof of it.

An agent's evaluation may run while other agent work shares its host, so the framework never
counts it: it measures the revision it retains itself, serially, with the official gates. The
record of an agent's evaluation (``local-validation``) can therefore never carry the kinds that
eligibility and the accuracy proof select, whatever else the record says; every other purpose can.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core

PROOF = (core.EvidenceKind.CORRECTNESS, core.EvidenceKind.BENCHMARK)
PURPOSES = ("baseline", "local-validation", "official", "profile")


def record(purpose: str, kind: core.EvidenceKind) -> core.EvidenceRef:
    return core.EvidenceRef(
        evidence_id=core.EvidenceId(root="evidence"),
        kind=kind,
        purpose=purpose,  # ty: ignore[invalid-argument-type]
        scope=core.Scope(owner=core.AttemptId(root="attempt"), generation=0),
        source_request=core.RequestId(root="request"),
        candidate=core.RevisionRef.of_git_commit("candidate"),
        observation_sequence=1,
        evaluator_digest="evaluator",
        workload_digest="workload",
        environment_digest="environment",
        provenance="trusted",
        status=core.ObservationStatus.SUCCEEDED,
    )


@given(purpose=st.sampled_from(PURPOSES), kind=st.sampled_from(list(core.EvidenceKind)))
def test_an_agents_own_evaluation_is_never_a_correctness_or_benchmark_proof(
    purpose: str, kind: core.EvidenceKind
) -> None:
    if purpose == "local-validation" and kind in PROOF:
        with pytest.raises(ValueError, match="local-validation record cannot be"):
            record(purpose, kind)
    else:
        assert record(purpose, kind).kind is kind
