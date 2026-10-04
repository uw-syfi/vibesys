"""Typed contract values: digest scheme, descendant manifest and retained selections."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core

COMMITS = st.text(alphabet="0123456789abcdef", min_size=7, max_size=64)


@given(commit=COMMITS)
def test_git_commit_refs_round_trip_through_the_wire(commit: str) -> None:
    ref = core.RevisionRef.of_git_commit(commit)
    assert ref.revision_id == core.RevisionId(root=commit)
    assert ref.digest == f"{core.DigestScheme.GIT_COMMIT}:{commit}"
    assert ref.git_commit == commit
    assert core.RevisionRef.model_validate_json(ref.model_dump_json()) == ref


@given(commit=COMMITS, other=COMMITS, scheme=st.sampled_from(["git-commit:", "tree:", ""]))
def test_git_commit_is_none_unless_digest_names_the_same_commit(
    commit: str, other: str, scheme: str
) -> None:
    ref = core.RevisionRef(revision_id=core.RevisionId(root=commit), digest=f"{scheme}{other}")
    expected = commit if scheme == "git-commit:" and other == commit else None
    assert ref.git_commit == expected


@pytest.mark.parametrize("commit", ["", " abc", "abc ", "git-commit:abc"])
def test_of_git_commit_rejects_non_bare_commit_ids(commit: str) -> None:
    with pytest.raises(core.ContractValidationError):
        core.RevisionRef.of_git_commit(commit)


def test_child_manifest_must_cover_children_and_claim_completeness() -> None:
    scope = core.Scope(owner=core.AttemptId(root="a"), generation=0)
    base = core.Observation(
        event_id=core.EventId(root="e"),
        request_id=core.RequestId(root="r"),
        scope=scope,
        sequence=1,
        observed_at=1.0,
        status=core.ObservationStatus.SUCCEEDED,
    )
    child = core.ResourceId(root="child")
    manifest = core.ChildManifest(members=(child,), basis="enumerated")
    complete = base.model_copy(update={"children_complete": True})
    with pytest.raises(ValueError, match="child_manifest"):
        core.Observation.model_validate(
            {
                **complete.model_dump(),
                "children": (child,),
                "child_manifest": core.ChildManifest(basis="enumerated"),
            }
        )
    with pytest.raises(ValueError, match="child_manifest"):
        core.Observation.model_validate({**base.model_dump(), "child_manifest": manifest})
    with pytest.raises(ValueError, match="duplicate"):
        core.ChildManifest(members=(child, child), basis="lifecycle-closed")
    ok = core.Observation.model_validate({**complete.model_dump(), "child_manifest": manifest})
    assert ok.descendants == (child,)
    assert core.Observation.model_validate_json(ok.model_dump_json()) == ok


@given(
    eligible=st.booleans(),
    retention=st.sampled_from(["discard", "wip", "candidate"]),
    same_id=st.booleans(),
    same_revision=st.booleans(),
    duplicated=st.booleans(),
)
def test_settlement_state_retains_only_the_selected_eligible_candidate(
    *, eligible: bool, retention: str, same_id: bool, same_revision: bool, duplicated: bool
) -> None:
    baseline = core.initial_state().run.facts.baseline
    other = baseline.model_copy(update={"digest": "other"})
    row = core.Settlement.model_validate(
        {
            "settlement_id": core.SettlementId(root="s"),
            "attempt": core.AttemptRef(attempt_id=core.AttemptId(root="a"), generation=0),
            "candidate": baseline,
            "assessments": (),
            "eligible": eligible,
            "retention": retention,
            "outcome": "succeeded",
        }
    )
    state = core.SettlementState(settlements=(row, row) if duplicated else (row,))
    selection = core.RetainedCandidate(
        settlement_id=core.SettlementId(root="s" if same_id else "x"),
        revision=baseline if same_revision else other,
    )
    expected = (
        eligible and retention == "candidate" and same_id and same_revision and not duplicated
    )
    assert state.retains(selection) == expected
    assert not state.retains(core.TrustedBaseline(revision=baseline))
    assert core.SettlementState.model_validate_json(state.model_dump_json()) == state


def evidence_row(
    name: str, *, request: str = "r", receipt: bool = True, **updates: object
) -> core.EvidenceRef:
    base = core.initial_state().run.facts
    scope = core.Scope(owner=core.AttemptId(root="a"), generation=0)
    status = updates.get("status", core.ObservationStatus.SUCCEEDED)
    assert isinstance(status, core.ObservationStatus)
    observed = core.Observation(
        event_id=core.EventId(root="e"),
        request_id=core.RequestId(root=request),
        scope=scope,
        sequence=2,
        observed_at=2.0,
        status=status,
        accepted=True,
        terminal=True,
    )
    data: dict[str, object] = {
        "evidence_id": core.EvidenceId(root=name),
        "kind": core.EvidenceKind.CORRECTNESS,
        "purpose": "official",
        "scope": scope,
        "source_request": core.RequestId(root=request),
        "candidate": base.baseline,
        "observation_sequence": 2,
        "evaluator_digest": base.evaluator_digest,
        "workload_digest": base.workload_digest,
        "environment_digest": base.environment_digest,
        "provenance": "trusted",
        "status": status,
        "acceptance_receipt": core.EvidenceAcceptanceReceipt(observation=observed)
        if receipt
        else None,
    }
    return core.EvidenceRef.model_validate({**data, **updates})


@given(
    kind=st.sampled_from(list(core.EvidenceKind)),
    status=st.sampled_from([core.ObservationStatus.SUCCEEDED, core.ObservationStatus.FAILED]),
    flaws=st.sets(st.sampled_from(["self-report", "no-receipt", "other-candidate", "twin"])),
)
def test_accuracy_proof_is_the_unique_trusted_accepted_successful_correctness_record(
    kind: core.EvidenceKind, status: core.ObservationStatus, flaws: set[str]
) -> None:
    baseline = core.initial_state().run.facts.baseline
    updates: dict[str, object] = {"kind": kind, "status": status}
    if "self-report" in flaws:
        updates["provenance"] = "self-report"
    if "other-candidate" in flaws:
        updates["candidate"] = baseline.model_copy(update={"digest": "elsewhere"})
    row = evidence_row("e", receipt="no-receipt" not in flaws, **updates)
    twin = evidence_row("e", request="r2", receipt="no-receipt" not in flaws, **updates)
    rows = (row, twin)
    state = core.EvaluationState(evidence=rows if "twin" in flaws else rows[:1])
    qualifies = (
        kind == core.EvidenceKind.CORRECTNESS and status == core.ObservationStatus.SUCCEEDED
    ) and not flaws
    assert state.accuracy_proof(baseline) == (row if qualifies else None)
    for item in state.evidence:
        assert state.evidence_for(item.key) == item
    missing = row.key.model_copy(update={"source_request": core.RequestId(root="x")})
    assert state.evidence_for(missing) is None
