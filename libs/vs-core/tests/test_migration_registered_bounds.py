"""Selected v2 migration cannot give registered work missing v3 bound authority."""

import json
from pathlib import Path
from typing import ClassVar, Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel

import vs_core.api as core


class Outcome(core.Value):
    """Immutable owning-library outcome for the migration codec fixture."""

    status: Literal["pending"] = "pending"


class MeasuredJob(core.OperationRequest):
    """A custom measurement used the same wire before history contracts existed."""

    kind: Literal["test.migration.measurement"] = "test.migration.measurement"
    lifecycle: Literal[core.LifecycleClass.OWNED_JOB] = core.LifecycleClass.OWNED_JOB
    outcome_model: ClassVar[type[BaseModel]] = Outcome
    identity: core.MeasurementIdentity


class ResumeTurn(core.OperationRequest):
    """Registered resume can be prepared before its invocation ledger exists."""

    kind: Literal["test.migration.resume"] = "test.migration.resume"
    lifecycle: Literal[core.LifecycleClass.SESSION_TURN] = core.LifecycleClass.SESSION_TURN
    outcome_model: ClassVar[type[BaseModel]] = Outcome
    turn: core.TurnSpec


def measurement_identity(request: core.OperationRequest) -> core.MeasurementIdentity:
    assert isinstance(request, MeasuredJob)
    return request.identity


def normalized_turn(request: core.OperationRequest) -> core.TurnSpec:
    assert isinstance(request, ResumeTurn)
    return request.turn


def legacy_payload(
    request: MeasuredJob | ResumeTurn,
    scope: core.Scope,
    phase: core.IntentPhase,
    *,
    measured: bool = True,
) -> tuple[core.OperationRegistry, str]:
    """Embed one frozen wire into bytes persisted by untouched current main."""
    old = json.loads((Path(__file__).parent / "fixtures" / "envelope-v2-main.json").read_text())
    descriptor = core.OperationDescriptor(
        kind=request.kind,
        request_schema=core.SchemaRef(name="request", version=1),
        outcome_schema=core.SchemaRef(name="outcome", version=1),
        lifecycle=request.lifecycle,
        resource_pool=core.PoolId(root="jobs") if isinstance(request, MeasuredJob) else None,
        inspect=True,
        cancel=True,
        watch=True,
    )
    registration = core.OperationRegistration(
        descriptor=descriptor,
        request_model=type(request),
        outcome_model=Outcome,
        normalize_measurement=measurement_identity
        if measured and isinstance(request, MeasuredJob)
        else None,
        normalize_turn=normalized_turn if isinstance(request, ResumeTurn) else None,
    )
    codec = core.OperationRegistry((registration,))
    decision = codec.validate_decision(
        core.Operation(
            decision_id=core.DecisionId(root="registered"),
            scope=scope,
            request=request,
            deadline_at=100.0,
        )
    )
    executable = core.ExecuteRegisteredOperation(
        request_id=core.RequestId(root="registered"),
        decision_id=decision.decision_id,
        scope=scope,
        deadline_at=100.0,
        operation_id=core.OperationId(root="registered"),
        operation=codec.encode(request),
        retry_limit=0,
    )
    intent = core.Intent(
        request_id=core.RequestId(root="registered"),
        request=executable,
        payload_digest="v2",
        lifecycle=request.lifecycle,
        phase=phase,
        reconcile_deadline_at=100.0,
    ).model_dump(mode="json")
    intent.pop("setup_failure")
    intent.pop("evaluation_result")
    intent["request"].pop("inputs")
    receipt = core.DecisionReceipt(
        decision_id=decision.decision_id,
        decision=decision,
        payload_digest="v2",
        feedback=core.Accepted(decision_id=decision.decision_id),
    ).model_dump(mode="json")
    receipt["decision"].pop("normalized_measurement")
    old["core"]["registry"] = [descriptor.model_dump(mode="json")]
    old["core"]["run"]["capabilities"]["operations"] = [descriptor.model_dump(mode="json")]
    old["core"]["intents"]["intents"] = [intent]
    old["core"]["run"]["receipts"] = [receipt]
    return codec, json.dumps(old)


def measured_request(digest: str) -> MeasuredJob:
    state = core.initial_state()
    return MeasuredJob(
        identity=core.MeasurementIdentity(
            purpose="official",
            candidate=state.run.facts.baseline,
            evaluator_digest=digest,
            workload_digest="workload",
            environment_digest="environment",
            recipe_digest="recipe",
            stages=(),
        )
    )


@given(st.text(alphabet="abcdef0123456789", min_size=1, max_size=20))
def test_prepared_registered_measurement_requires_reconstructable_attempt_history(
    digest: str,
) -> None:
    scope = core.Scope(owner=core.AttemptId(root="attempt"), generation=0)
    codec, source = legacy_payload(measured_request(digest), scope, core.IntentPhase.PREPARED)
    with pytest.raises(core.ContractError, match=r"history.*unavailable"):
        codec.migrate_envelope(
            core.RunEnvelope[core.StrategyState], source, core.v2_to_v3_migration(codec)
        )


@given(
    st.sampled_from(
        (core.IntentPhase.DISPATCHED, core.IntentPhase.RECONCILING, core.IntentPhase.COMPLETED)
    )
)
def test_live_registered_measurement_keeps_ownership_and_unavailable_history(
    phase: core.IntentPhase,
) -> None:
    scope = core.Scope(owner=core.AttemptId(root="attempt"), generation=0)
    codec, source = legacy_payload(measured_request("evaluator"), scope, phase)
    loaded = codec.migrate_envelope(
        core.RunEnvelope[core.StrategyState], source, core.v2_to_v3_migration(codec)
    )
    assert loaded.core.intents.intents[0].phase == phase
    assert loaded.core.intents.recovery.phase == core.RecoveryPhase.REQUIRED
    assert (
        loaded.core.attempts.attempts[0].evaluation_history.availability
        == core.EvaluationHistoryAvailability.UNAVAILABLE
    )
    assert codec.decode_envelope(type(loaded), codec.encode_envelope(loaded)) == loaded


def test_prepared_generic_job_without_measurement_authority_stays_recoverable() -> None:
    scope = core.Scope(owner=core.AttemptId(root="attempt"), generation=0)
    codec, source = legacy_payload(
        measured_request("evaluator"), scope, core.IntentPhase.PREPARED, measured=False
    )
    loaded = codec.migrate_envelope(
        core.RunEnvelope[core.StrategyState], source, core.v2_to_v3_migration(codec)
    )
    assert loaded.core.intents.intents[0].phase == core.IntentPhase.PREPARED
    assert loaded.core.intents.recovery.phase == core.RecoveryPhase.REQUIRED


@given(st.text(alphabet="abcdef0123456789", min_size=1, max_size=20))
def test_prepared_registered_resume_needs_publication_proof_before_invocation_exists(
    identity: str,
) -> None:
    scope = core.Scope(owner=core.RunId(root="run"), generation=0)
    request = ResumeTurn(
        turn=core.TurnSpec(
            session=core.SessionSpec(
                session_id=core.SessionId(root="session"),
                role_id=core.RoleId(root="role"),
                policy="reuse",
                lifetime="owner",
                access=core.Access.READ_ONLY,
            ),
            invocation_id=core.InvocationId(root=identity),
            continuation_id=core.ContinuationId(root="waiting"),
            workspace=scope,
            prompts=(),
            output_schema=core.SchemaRef(name="output", version=1),
            deadline_at=100.0,
            charge_class="resume",
        )
    )
    codec, source = legacy_payload(request, scope, core.IntentPhase.PREPARED)
    with pytest.raises(core.ContractError, match=r"resume.*unavailable"):
        codec.migrate_envelope(
            core.RunEnvelope[core.StrategyState], source, core.v2_to_v3_migration(codec)
        )
