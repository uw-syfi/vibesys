"""Independently generated canonical proof expectations and schema-valid facts."""

import hashlib
import json

from hypothesis import strategies as st

import vs_core.api as core


@st.composite
def receipt_facts(draw: st.DrawFn) -> tuple[core.Stop, core.DecisionReceipt]:
    suffix = draw(st.integers(min_value=0, max_value=10_000))
    decision = core.Stop(
        decision_id=core.DecisionId(root=f"stop-{suffix}"),
        scope=core.Scope(
            owner=core.RunId(root=f"run-{suffix}"), generation=draw(st.integers(0, 20))
        ),
        mode=draw(st.sampled_from(("drain", "cancel"))),
        result=core.RunResultProposal(outcome="cancelled", reason="test"),
    )
    return decision, core.DecisionReceipt(
        decision_id=decision.decision_id,
        decision=decision,
        payload_digest=hashlib.sha256(
            json.dumps(
                decision.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest(),
        feedback=core.Accepted(decision_id=decision.decision_id),
    )


def fact_digest(value: core.Value) -> str:
    """Independent wire fingerprint for facts without unordered collections."""
    return hashlib.sha256(
        json.dumps(value.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@st.composite
def request_facts(draw: st.DrawFn) -> tuple[core.InspectRequest, core.Intent]:
    suffix = draw(st.integers(0, 10_000))
    scope = core.Scope(
        owner=core.AttemptId(root=f"attempt-{suffix}"), generation=draw(st.integers(0, 20))
    )
    request = core.InspectRequest(
        request_id=core.RequestId(root=f"query-{suffix}"),
        scope=scope,
        target=core.RequestId(root=f"target-{suffix}"),
        admission_id=core.DecisionId(root=f"episode-{suffix}"),
        deadline_at=100.0,
    )
    assert request.request_id is not None
    return request, core.Intent(
        request_id=request.request_id,
        request=request,
        payload_digest=fact_digest(request),
        lifecycle=core.LifecycleClass.QUERY,
        phase=core.IntentPhase.COMPLETED,
        reconcile_deadline_at=100.0,
    )


@st.composite
def observation_facts(draw: st.DrawFn) -> tuple[core.Intent, core.Observation]:
    request, intent = draw(request_facts())
    assert request.request_id is not None
    observation = core.Observation(
        event_id=core.EventId(root=f"event-{request.request_id.root}"),
        request_id=request.request_id,
        scope=request.scope,
        admission_id=request.admission_id,
        sequence=draw(st.integers(0, 100)),
        observed_at=10.0,
        status=core.ObservationStatus.SUCCEEDED,
        accepted=True,
        terminal=True,
        resource_id=core.ResourceId(root="lease"),
        released=True,
        children_complete=True,
    )
    return intent, observation


@st.composite
def admission_facts(draw: st.DrawFn) -> tuple[core.AttemptView, core.Scope, core.DecisionId]:
    request, _ = draw(request_facts())
    initial = core.initial_state()
    assert isinstance(request.scope.owner, core.AttemptId)
    assert request.admission_id is not None
    attempt = core.AttemptView(
        attempt_id=request.scope.owner,
        generation=request.scope.generation,
        item_id=core.ItemId(root="item"),
        phase=core.AttemptPhase.ACTIVE,
        workspace=core.WorkspacePlan(
            mode=core.WorkspaceMode.EXCLUSIVE_ROOT, base=initial.run.facts.baseline
        ),
        budget=core.AttemptBudget(),
        admission_id=request.admission_id,
    )
    return attempt, request.scope, request.admission_id


@st.composite
def dependency_facts(draw: st.DrawFn) -> tuple[core.RequestBase, core.DecisionReceipt, core.Intent]:
    decision, receipt = draw(receipt_facts())
    intent, observation = draw(observation_facts())
    intent = intent.model_copy(
        update={"observation": observation, "sequence": observation.sequence}
    )
    request = core.RequestBase(
        scope=decision.scope,
        deadline_at=100.0,
        decision_dependencies=(decision.decision_id,),
        depends_on=(intent.request_id,),
    )
    receipt = receipt.model_copy(update={"completion": core.CompletionStatus.SUCCEEDED})
    return request, receipt, intent


@st.composite
def stop_facts(draw: st.DrawFn) -> tuple[core.RunState, core.Stop]:
    canonical, receipt = draw(receipt_facts())
    run = core.initial_state().run.model_copy(
        update={
            "run_id": canonical.scope.owner,
            "generation": canonical.scope.generation,
            "receipts": (receipt,),
            "result": canonical.result,
            "status": core.RunStatus.CLOSING,
        }
    )
    return run, canonical


@st.composite
def closure_facts(draw: st.DrawFn) -> tuple[core.AttemptView, core.AttemptClosure]:
    attempt, _, episode = draw(admission_facts())
    closure = core.AttemptClosure(
        disposition=draw(st.sampled_from(("park", "cancel", "settle"))),
        requested_at=float(draw(st.integers(0, 100))),
        authority=core.RequestId(root="closure"),
        admission_id=episode,
    )
    return attempt.model_copy(update={"closure": closure}), closure
