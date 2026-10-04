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


@given(
    first=st.integers(min_value=0, max_value=10000),
    second=st.integers(min_value=0, max_value=10000),
)
def test_alternating_source_watermarks_survive_public_step_and_codec(
    first: int, second: int
) -> None:
    observations = (source_observation("a", first), source_observation("b", second))
    assert observations[0].resource_id is not None
    lease = core.ChildLease(
        resource_id=observations[0].resource_id,
        scope=observations[0].scope,
        source_requests=tuple(row.request_id for row in observations),
        observation=observations[0],
        observation_watermarks=tuple(
            core.ChildObservationWatermark(source_request=row.request_id, observation=row)
            for row in observations
        ),
        watermark_history_complete=True,
    )
    state = core.initial_state()
    state = state.model_copy(
        update={"intents": state.intents.model_copy(update={"children": (lease,)})}
    )
    result = core.step(
        state,
        core.RunControlEvent(
            control=core.ControlInput(control_id=core.ControlId(root="steer"), action="steer"),
            now_at=0.0,
        ),
    )
    restored = core.CoreState.model_validate_json(result.state.model_dump_json())
    assert restored.intents.children == (lease,)
    assert tuple(mark.observation.sequence for mark in lease.observation_watermarks) == (
        first,
        second,
    )
    assert restored.intents.children[0].observation == observations[0]


@pytest.mark.parametrize(
    "fault", ["missing", "duplicate", "wrong-source", "wrong-resource", "wrong-scope", "aggregate"]
)
@given(sequence=st.integers(min_value=0, max_value=10000))
def test_complete_child_history_rejects_missing_or_misattributed_proofs(
    fault: str, sequence: int
) -> None:
    observed = source_observation("a", sequence)
    mark = core.ChildObservationWatermark(source_request=observed.request_id, observation=observed)
    data = {
        "resource_id": observed.resource_id,
        "scope": observed.scope,
        "source_requests": (observed.request_id,),
        "observation": observed,
        "observation_watermarks": (mark,),
        "watermark_history_complete": True,
    }
    match fault:
        case "missing":
            data["observation_watermarks"] = ()
        case "duplicate":
            data["observation_watermarks"] = (mark, mark)
        case "wrong-source":
            data["source_requests"] = (core.RequestId(root="other"),)
        case "wrong-resource":
            data["resource_id"] = core.ResourceId(root="other")
        case "wrong-scope":
            data["scope"] = core.Scope(owner=core.RunId(root="run"), generation=1)
        case "aggregate":
            data["observation"] = observed.model_copy(update={"sequence": sequence + 1})
    with pytest.raises(ValidationError):
        core.ChildLease.model_validate(data)


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
