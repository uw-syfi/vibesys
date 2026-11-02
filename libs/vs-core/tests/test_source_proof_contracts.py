"""Durable source proofs remain attributable after aggregate observations change."""

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

import vs_core.api as core


def source_observation(source: str, sequence: int, resource: str = "child") -> core.Observation:
    return core.Observation(
        event_id=core.EventId(root=f"{source}:{sequence}"),
        request_id=core.RequestId(root=source),
        scope=core.Scope(owner=core.RunId(root="run"), generation=0),
        resource_id=core.ResourceId(root=resource),
        sequence=sequence,
        observed_at=float(sequence),
        status=core.ObservationStatus.SUCCEEDED,
        accepted=True,
        terminal=True,
        released=True,
        children_complete=True,
    )


@given(sequence=st.integers(min_value=0, max_value=10000))
def test_evidence_acceptance_receipt_is_frozen_after_later_source_observations(
    sequence: int,
) -> None:
    state = core.initial_state()
    observed = source_observation("measurement", sequence, "job")
    receipt = core.EvidenceAcceptanceReceipt(observation=observed)
    evidence = core.EvidenceRef(
        evidence_id=core.EvidenceId(root="evidence"),
        kind=core.EvidenceKind.BENCHMARK,
        purpose="official",
        scope=observed.scope,
        source_request=observed.request_id,
        candidate=state.run.facts.baseline,
        observation_sequence=sequence,
        evaluator_digest="evaluator",
        workload_digest="workload",
        environment_digest="environment",
        provenance="trusted",
        status=observed.status,
        acceptance_receipt=receipt,
    )
    later = observed.model_copy(update={"sequence": sequence + 1, "diagnostic": "later cleanup"})
    job = core.RegisteredOwnedJob(
        wait=core.WaitOn(timer=0.0),
        operation_id=core.OperationId(root="measurement"),
        request_id=observed.request_id,
        scope=observed.scope,
        resource_pool=core.PoolId(root="jobs"),
        resource_id=observed.resource_id,
        observation=later,
        evidence=(evidence,),
    )
    state = state.model_copy(
        update={"evaluation": core.EvaluationState(registered_jobs=(job,), evidence=(evidence,))}
    )
    result = core.step(
        state,
        core.RunControlEvent(
            control=core.ControlInput(control_id=core.ControlId(root="steer"), action="steer"),
            now_at=0.0,
        ),
    )
    restored = core.CoreState.model_validate_json(result.state.model_dump_json())
    assert restored.evaluation.evidence[0].acceptance_receipt == receipt
    assert restored.evaluation.registered_jobs[0].observation == later


@pytest.mark.parametrize(
    "fault", ["source", "scope", "sequence", "status", "nonacceptance", "unknown"]
)
@given(sequence=st.integers(min_value=0, max_value=10000))
def test_evidence_receipts_reject_changed_or_unknown_acceptance(fault: str, sequence: int) -> None:
    observed = source_observation("measurement", sequence, "job")
    state = core.initial_state()
    data = {
        "evidence_id": core.EvidenceId(root="evidence"),
        "kind": core.EvidenceKind.BENCHMARK,
        "purpose": "official",
        "scope": observed.scope,
        "source_request": observed.request_id,
        "candidate": state.run.facts.baseline,
        "observation_sequence": sequence,
        "evaluator_digest": "evaluator",
        "workload_digest": "workload",
        "environment_digest": "environment",
        "provenance": "trusted",
        "status": observed.status,
    }
    match fault:
        case "source":
            data["source_request"] = core.RequestId(root="other")
        case "scope":
            data["scope"] = core.Scope(owner=core.RunId(root="run"), generation=1)
        case "sequence":
            data["observation_sequence"] = sequence + 1
        case "status":
            data["status"] = core.ObservationStatus.FAILED
        case "nonacceptance":
            observed = observed.model_copy(update={"accepted": False})
        case "unknown":
            observed = observed.model_copy(update={"status": core.ObservationStatus.UNKNOWN})
    with pytest.raises(ValidationError):
        core.EvidenceRef.model_validate({**data, "acceptance_receipt": {"observation": observed}})
