"""Current-main version-2 persisted bytes never gain fabricated v3 authority."""

import json
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api import (
    ENVELOPE_SCHEMA_VERSION,
    ContractError,
    EvaluationHistoryAvailability,
    OperationRegistry,
    ProposalSubmitted,
    RunEnvelope,
    StrategyState,
    project,
    step,
    v2_to_v3_migration,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "envelope-v2-main.json"


def _load(source: str) -> RunEnvelope[StrategyState]:
    codec = OperationRegistry()
    return codec.migrate_envelope(RunEnvelope[StrategyState], source, v2_to_v3_migration(codec))


def test_current_main_persisted_state_requires_selected_migration_and_roundtrips() -> None:
    source = _FIXTURE.read_text()
    codec = OperationRegistry()
    with pytest.raises(ContractError, match="migration"):
        codec.decode_envelope(RunEnvelope[StrategyState], source)
    loaded = _load(source)
    assert loaded.schema_version == ENVELOPE_SCHEMA_VERSION
    assert (
        codec.decode_envelope(RunEnvelope[StrategyState], codec.encode_envelope(loaded)) == loaded
    )
    old = json.loads(source)
    assert loaded.core.run.model_dump(mode="json") == old["core"]["run"] | {
        "limits": old["core"]["run"]["limits"] | {"pool_capacities": []},
    }
    attempt = loaded.core.attempts.attempts[0]
    assert attempt.charges[0].charged == 1
    assert attempt.evaluation_history.availability == EvaluationHistoryAvailability.UNAVAILABLE
    assert attempt.terminal_reason is None
    assert loaded.core.sessions.run_checkpoints == ()
    continuation = loaded.core.evaluation.continuations[0]
    assert continuation.authorization_receipt is None
    assert continuation.deadline_at == old["core"]["evaluation"]["continuations"][0]["deadline_at"]
    child = loaded.core.intents.children[0]
    assert not child.watermark_history_complete
    assert len(child.observation_watermarks) == 1
    assert child.observation_watermarks[0].observation == child.observation
    result = step(loaded.core, ProposalSubmitted(decisions=(), expected_revision=loaded.revision))
    assert project(result.state).attempts[0].evaluation_history == attempt.evaluation_history
    assert result.requests == ()
    assert loaded.fence.model_dump(mode="json") == old["fence"]
    assert loaded.event_cursor.model_dump(mode="json") == old["event_cursor"]


@given(
    st.integers(min_value=0, max_value=10000),
    st.floats(min_value=0, max_value=10000, allow_nan=False),
)
def test_migration_preserves_original_source_sequences_and_deadlines(
    sequence: int, deadline: float
) -> None:
    old = json.loads(_FIXTURE.read_text())
    observation = old["core"]["intents"]["children"][0]["observation"]
    observation["sequence"] = sequence
    old["core"]["run"]["deadline_at"] = deadline
    old["core"]["evaluation"]["continuations"][0]["deadline_at"] = deadline
    loaded = _load(json.dumps(old))
    child = loaded.core.intents.children[0]
    assert child.observation_watermarks[0].observation.sequence == sequence
    assert not child.watermark_history_complete
    assert loaded.core.run.deadline_at == deadline
    assert loaded.core.evaluation.continuations[0].deadline_at == deadline
    codec = OperationRegistry()
    assert (
        codec.decode_envelope(RunEnvelope[StrategyState], codec.encode_envelope(loaded)) == loaded
    )


@pytest.mark.parametrize(
    "area", ["run", "attempts", "sessions", "evaluation", "intents", "settlement"]
)
def test_migration_rejects_unknown_keys_in_every_area(area: str) -> None:
    old = json.loads(_FIXTURE.read_text())
    old["core"][area]["fabricated_authority"] = True
    with pytest.raises(ValueError, match="fabricated_authority"):
        _load(json.dumps(old))


def test_migration_does_not_infer_other_source_watermarks_from_latest_observation() -> None:
    old = json.loads(_FIXTURE.read_text())
    child = old["core"]["intents"]["children"][0]
    child["source_requests"].append({"kind": "request", "root": "second-source"})
    loaded = _load(json.dumps(old))
    migrated = loaded.core.intents.children[0]
    assert not migrated.watermark_history_complete
    assert len(migrated.observation_watermarks) == 1
    assert len(migrated.source_requests) > len(migrated.observation_watermarks)


@pytest.mark.parametrize(
    "path",
    [
        ("core", "attempts", "attempts", 0, "budget", "repeated_failure_limit"),
        ("core", "attempts", "attempts", 0, "evaluation_history"),
        ("core", "attempts", "attempts", 0, "terminal_reason"),
        ("core", "intents", "children", 0, "observation_watermarks"),
        ("core", "intents", "children", 0, "watermark_history_complete"),
        ("core", "sessions", "run_checkpoints"),
        ("core", "run", "limits", "pool_capacities"),
        ("core", "evaluation", "continuations", 0, "preceding_submission"),
        ("core", "evaluation", "continuations", 0, "authorization_receipt"),
    ],
)
def test_version2_rejects_every_introduced_contract_field(path: tuple[str | int, ...]) -> None:
    old = json.loads(_FIXTURE.read_text())
    target = old
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = None
    with pytest.raises(ContractError, match=str(path[-1])):
        _load(json.dumps(old))


def _version2_ensure_intent() -> dict[str, object]:
    scope = core.Scope(owner=core.RunId(root="run"), generation=0)
    request = core.EnsureSession(
        request_id=core.RequestId(root="ensure"),
        scope=scope,
        deadline_at=100.0,
        spec=core.SessionSpec(
            session_id=core.SessionId(root="session"),
            role_id=core.RoleId(root="planner"),
            policy="fresh",
            lifetime="ephemeral",
            access=core.Access.READ_ONLY,
        ),
    )
    intent = core.Intent(
        request_id=core.RequestId(root="ensure"),
        request=request,
        payload_digest="payload",
        lifecycle=core.LifecycleClass.IDEMPOTENT_WRITE,
        phase=core.IntentPhase.PREPARED,
        reconcile_deadline_at=100.0,
    ).model_dump(mode="json")
    intent.pop("setup_failure")
    intent.pop("evaluation_result", None)
    return intent


@pytest.mark.parametrize("field", ["setup_failure", "evaluation_result"])
def test_version2_rejects_new_authority_fields_in_legacy_intent(field: str) -> None:
    old = json.loads(_FIXTURE.read_text())
    intent = _version2_ensure_intent()
    old["core"]["intents"]["intents"] = [intent]
    intent[field] = None
    with pytest.raises(ContractError, match=field):
        _load(json.dumps(old))


def test_version2_rejects_new_bound_in_accepted_start_receipt() -> None:
    old = json.loads(_FIXTURE.read_text())
    state = core.initial_state()
    identity = core.DecisionId(root="queued-start")
    decision = core.StartAttempt(
        decision_id=identity,
        scope=core.Scope(owner=state.run.run_id, generation=0),
        attempt_id=core.AttemptId(root="queued"),
        item_id=core.ItemId(root="item"),
        workspace=core.WorkspacePlan(
            mode=core.WorkspaceMode.ISOLATED_CHILD, base=state.run.facts.baseline
        ),
        budget=core.AttemptBudget(),
    )
    receipt = core.DecisionReceipt(
        decision_id=identity,
        decision=decision,
        payload_digest="payload",
        feedback=core.Accepted(decision_id=identity),
    ).model_dump(mode="json")
    old["core"]["run"]["receipts"] = [receipt]
    with pytest.raises(ContractError, match="repeated_failure_limit"):
        _load(json.dumps(old))


def _version2_intent(request: core.Request, phase: core.IntentPhase) -> dict[str, object]:
    identity = request.request_id
    assert identity is not None
    intent = core.Intent(
        request_id=identity,
        request=request,
        payload_digest="legacy-payload",
        lifecycle=core.LifecycleClass.SESSION_TURN,
        phase=phase,
        reconcile_deadline_at=request.deadline_at,
    ).model_dump(mode="json")
    intent.pop("setup_failure")
    intent.pop("evaluation_result", None)
    return intent


def _legacy_resume(deadline: float) -> core.ResumeSessionTurn:
    scope = core.Scope(owner=core.AttemptId(root="historical"), generation=0)
    continuation = core.ContinuationId(root="waiting")
    turn = core.TurnSpec(
        session=core.SessionSpec(
            session_id=core.SessionId(root="session"),
            role_id=core.RoleId(root="implementer"),
            policy="reuse",
            lifetime="owner",
            access=core.Access.WRITE_CANDIDATE,
        ),
        invocation_id=core.InvocationId(root="next"),
        continuation_id=continuation,
        workspace=scope,
        prompts=(),
        output_schema=core.SchemaRef(name="result", version=1),
        deadline_at=deadline,
        charge_class="resume",
    )
    return core.ResumeSessionTurn(
        request_id=core.RequestId(root="resume"),
        scope=scope,
        admission_id=core.DecisionId(root="admitted"),
        deadline_at=deadline,
        turn=turn,
        continuation_id=continuation,
    )


@given(st.integers(min_value=20, max_value=10000))
def test_prepared_legacy_resume_cannot_gain_authorization_from_absent_v3_receipt(
    deadline: int,
) -> None:
    old = json.loads(_FIXTURE.read_text())
    old["core"]["evaluation"]["continuations"][0]["phase"] = "authorized"
    old["core"]["intents"]["intents"] = [
        _version2_intent(_legacy_resume(float(deadline)), core.IntentPhase.PREPARED)
    ]
    with pytest.raises(ContractError, match="resume publication/history"):
        _load(json.dumps(old))


@given(
    phase=st.sampled_from([core.IntentPhase.DISPATCHED, core.IntentPhase.RECONCILING]),
    deadline=st.integers(min_value=20, max_value=10000),
)
def test_started_legacy_resume_keeps_inspection_identity_behind_fresh_recovery(
    phase: core.IntentPhase, deadline: int
) -> None:
    old = json.loads(_FIXTURE.read_text())
    request = _legacy_resume(float(deadline))
    old["core"]["intents"]["intents"] = [_version2_intent(request, phase)]
    loaded = _load(json.dumps(old))
    assert loaded.core.intents.recovery.phase == core.RecoveryPhase.REQUIRED
    assert loaded.core.intents.intents[0].request == request
    assert loaded.core.intents.intents[0].phase == phase
    assert loaded.core.evaluation.continuations[0].authorization_receipt is None
    assert loaded.core.run.status == core.RunStatus.RUNNING
    with pytest.raises(ContractError, match="recovery"):
        step(loaded.core, core.DispatchAuthorized(request_id=core.RequestId(root="resume")))


@given(st.integers(min_value=20, max_value=10000))
def test_prepared_attempt_measurement_cannot_gain_complete_history_from_migration(
    deadline: int,
) -> None:
    old = json.loads(_FIXTURE.read_text())
    baseline = core.initial_state().run.facts
    plan = core.MeasurementPlan(
        purpose="official",
        candidate=baseline.baseline,
        evaluator_digest=baseline.evaluator_digest,
        workload_digest=baseline.workload_digest,
        environment_digest=baseline.environment_digest,
        stages=(core.MeasurementStage(stage_id="benchmark", execution_budget=float(deadline)),),
        policy="ordered",
        recipe=core.ArtifactRef(artifact_id=core.ArtifactId(root="recipe"), digest="recipe"),
        submitted_at=0.0,
        queue_allowance=0.0,
        deadline_at=float(deadline),
    )
    request = core.SubmitMeasurement(
        request_id=core.RequestId(root="measurement"),
        scope=core.Scope(owner=core.AttemptId(root="historical"), generation=0),
        admission_id=core.DecisionId(root="admitted"),
        deadline_at=float(deadline),
        plan=plan,
    )
    intent = _version2_intent(request, core.IntentPhase.PREPARED)
    intent["lifecycle"] = core.LifecycleClass.OWNED_JOB
    old["core"]["intents"]["intents"] = [intent]
    with pytest.raises(ContractError, match="attempt evaluation history"):
        _load(json.dumps(old))


def test_migration_requires_fresh_recovery_even_for_legacy_ready_generic_intent() -> None:
    old = json.loads(_FIXTURE.read_text())
    old["core"]["intents"]["intents"] = [_version2_ensure_intent()]
    assert old["core"]["intents"]["recovery"]["phase"] == "ready"
    loaded = _load(json.dumps(old))
    assert loaded.core.intents.recovery.phase == core.RecoveryPhase.REQUIRED
    assert loaded.core.intents.recovery.epoch == old["core"]["intents"]["recovery"]["epoch"]
    assert loaded.core.run.status == core.RunStatus.RUNNING
    with pytest.raises(ContractError, match="recovery"):
        step(loaded.core, core.DispatchAuthorized(request_id=core.RequestId(root="ensure")))


@given(st.integers(min_value=20, max_value=10000))
def test_legacy_invocation_cannot_claim_new_paid_cycle_history_prefix(deadline: int) -> None:
    old = json.loads(_FIXTURE.read_text())
    request = _legacy_resume(float(deadline))
    invocation = core.Invocation(
        invocation=core.InvocationRef(
            session_id=request.turn.session.session_id,
            invocation_id=request.turn.invocation_id,
            generation=request.scope.generation,
        ),
        scope=request.scope,
        turn=request.turn,
        phase=core.SessionPhase.ACQUIRING,
    ).model_dump(mode="json")
    invocation.pop("evaluation_prefix")
    old["core"]["sessions"]["invocations"] = [invocation]
    loaded = _load(json.dumps(old))
    assert loaded.core.sessions.invocations[0].evaluation_prefix is None
    assert loaded.core.intents.recovery.phase == core.RecoveryPhase.REQUIRED
    invocation["evaluation_prefix"] = {"ordinal": 0, "submission_id": None}
    with pytest.raises(ContractError, match="evaluation_prefix"):
        _load(json.dumps(old))
