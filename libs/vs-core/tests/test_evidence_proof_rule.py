"""A framework measurement is trusted whoever requested it.

An agent's evaluation runs through the framework's executor on a committed revision, with the
same gates as the framework's own check, so its records are as trusted as the check's. The
purpose of a run says who asked for it; it never limits which kinds the record carries. Several
runs of one revision are samples of one measurement (`vs_core._comparison`), not rivals.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core

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
def test_no_requester_limits_the_kinds_a_framework_record_carries(
    purpose: str, kind: core.EvidenceKind
) -> None:
    assert record(purpose, kind).kind is kind
