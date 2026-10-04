"""Registered measurement authority and exact version-2 input manifest migration."""

import hashlib
import json
from pathlib import Path
from typing import ClassVar, Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel

import vs_core.api as core


class Output(core.Value):
    status: Literal["succeeded"] = "succeeded"


class OpaqueMetadata(core.Value):
    evidence_id: str
    observation_sequence: int


class PersistedTurn(core.OperationRequest):
    kind: Literal["fixture.turn"] = "fixture.turn"
    lifecycle: Literal[core.LifecycleClass.SESSION_TURN] = core.LifecycleClass.SESSION_TURN
    outcome_model: ClassVar[type[BaseModel]] = Output
    turn: core.TurnSpec
    metadata: OpaqueMetadata


def normalize_turn(request: core.OperationRequest) -> core.TurnSpec:
    assert isinstance(request, PersistedTurn)
    return request.turn


def persisted_turn_codec() -> core.OperationRegistry:
    return core.OperationRegistry(
        (
            core.OperationRegistration(
                descriptor=core.OperationDescriptor(
                    kind="fixture.turn",
                    request_schema=core.SchemaRef(name="fixture-turn", version=1),
                    outcome_schema=core.SchemaRef(name="fixture-output", version=1),
                    lifecycle=core.LifecycleClass.SESSION_TURN,
                    inspect=True,
                    cancel=True,
                    watch=True,
                ),
                request_model=PersistedTurn,
                outcome_model=Output,
                normalize_turn=normalize_turn,
            ),
        )
    )


def value_digest(value: core.Value) -> str:
    payload = json.dumps(value.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def test_main_encoder_registered_turn_migrates_exact_inputs_and_preserves_payload() -> None:
    source = (Path(__file__).parent / "fixtures" / "envelope-v2-registered-turn.json").read_text()
    old = json.loads(source)
    codec = persisted_turn_codec()
    loaded = codec.migrate_envelope(
        core.RunEnvelope[core.StrategyState], source, core.v2_to_v3_migration(codec)
    )
    intent = loaded.core.intents.intents[0]
    assert isinstance(intent.request, core.ExecuteRegisteredOperation)
    old_intent = old["core"]["intents"]["intents"][0]
    old_wire = old_intent["request"]["operation"]
    assert intent.request.operation.model_dump(mode="json") == old_wire
    assert tuple(row.input_id.root for row in intent.request.inputs) == ("input-0", "input-1")
    assert intent.request.inputs[0].artifact == intent.request.inputs[1].artifact
    assert intent.payload_digest == value_digest(intent.request)
    assert intent.payload_digest != old_intent["payload_digest"]
    receipt = loaded.core.run.receipts[0]
    assert isinstance(receipt.decision, core.Operation)
    assert isinstance(receipt.decision.request, PersistedTurn)
    assert receipt.decision.request.metadata == OpaqueMetadata(
        evidence_id="owner-payload-field", observation_sequence=71
    )
    assert receipt.payload_digest == value_digest(receipt.decision)
    assert receipt.decision.registered_turn == receipt.decision.normalized_turn
    result = core.step(
        loaded.core, core.ProposalSubmitted(decisions=(), expected_revision=loaded.revision)
    )
    assert result.requests == ()
    assert result.state.intents == loaded.core.intents
    assert (
        codec.decode_envelope(core.RunEnvelope[core.StrategyState], codec.encode_envelope(loaded))
        == loaded
    )


class RegisteredMeasurement(core.OperationRequest):
    kind: Literal["fixture.measurement"] = "fixture.measurement"
    lifecycle: Literal[core.LifecycleClass.OWNED_JOB] = core.LifecycleClass.OWNED_JOB
    outcome_model: ClassVar[type[BaseModel]] = Output
    identity: core.MeasurementIdentity


def normalize_measurement(request: core.OperationRequest) -> core.MeasurementIdentity:
    assert isinstance(request, RegisteredMeasurement)
    return request.identity


def measurement_codec(*, normalized: bool = True) -> core.OperationRegistry:
    return core.OperationRegistry(
        (
            core.OperationRegistration(
                descriptor=core.OperationDescriptor(
                    kind="fixture.measurement",
                    request_schema=core.SchemaRef(name="measurement", version=1),
                    outcome_schema=core.SchemaRef(name="output", version=1),
                    lifecycle=core.LifecycleClass.OWNED_JOB,
                    resource_pool=core.PoolId(root="jobs"),
                    inspect=True,
                    cancel=True,
                    watch=True,
                ),
                request_model=RegisteredMeasurement,
                outcome_model=Output,
                normalize_measurement=normalize_measurement if normalized else None,
            ),
        )
    )


def expected_identity(fingerprint: str) -> core.MeasurementIdentity:
    return core.MeasurementIdentity(
        purpose="official",
        candidate=core.initial_state().run.facts.baseline,
        evaluator_digest=fingerprint,
        workload_digest="workload",
        environment_digest="environment",
        recipe_digest="recipe",
        stages=(core.MeasurementStageIdentity(stage_id="benchmark"),),
    )


@given(fingerprint=st.text(alphabet="abcdef0123456789", min_size=1, max_size=32))
def test_expected_measurement_is_bound_to_canonical_registered_payload(fingerprint: str) -> None:
    identity = expected_identity(fingerprint)
    codec = measurement_codec()
    state = core.initial_state()
    state = state.model_copy(
        update={
            "registry": codec.descriptors,
            "run": state.run.model_copy(
                update={"capabilities": core.Capabilities(operations=codec.descriptors)}
            ),
        }
    )
    decision = codec.validate_decision(
        core.Operation(
            decision_id=core.DecisionId(root="measurement"),
            scope=core.Scope(owner=state.run.run_id, generation=0),
            request=RegisteredMeasurement(identity=identity),
            deadline_at=100.0,
        )
    )
    assert decision.registered_measurement == decision.normalized_measurement == identity
    changed = decision.model_copy(
        update={
            "normalized_measurement": identity.model_copy(
                update={"evaluator_digest": f"{fingerprint}:forged"}
            )
        }
    )
    result = core.step(state, core.DecisionSubmitted(decision=changed, expected_revision=0))
    assert isinstance(result.events[0], core.Rejected)
    assert result.events[0].code == core.RejectionCode.UNKNOWN_SCHEMA
    assert result.requests == ()
    restored = codec.decode_envelope(
        core.RunEnvelope[core.StrategyState],
        codec.encode_envelope(
            core.RunEnvelope[core.StrategyState](
                schema_version=core.ENVELOPE_SCHEMA_VERSION,
                fence=core.HostFence(host_id=core.HostId(root="host"), epoch=0),
                strategy_id=state.run.declaration.strategy_id,
                state_schema=state.run.declaration.state_schema,
                core=state.model_copy(
                    update={
                        "run": state.run.model_copy(
                            update={
                                "receipts": (
                                    core.DecisionReceipt(
                                        decision_id=decision.decision_id,
                                        decision=decision,
                                        payload_digest=value_digest(decision),
                                        feedback=core.Accepted(decision_id=decision.decision_id),
                                    ),
                                )
                            }
                        )
                    }
                ),
                strategy=core.StrategyState(schema_version=1),
                event_cursor=core.EventCursor(sequence=0),
            )
        ),
    )
    assert isinstance(restored.core.run.receipts[0].decision, core.Operation)
    assert restored.core.run.receipts[0].decision.registered_measurement == identity


def test_absent_measurement_normalizer_grants_no_expected_identity() -> None:
    codec = measurement_codec(normalized=False)
    request = RegisteredMeasurement(identity=expected_identity("evaluator"))
    assert codec.normalize_measurement(request) is None
    assert (
        core.RegisteredOwnedJob(
            operation_id=core.OperationId(root="job"),
            request_id=core.RequestId(root="submit"),
            scope=core.Scope(owner=core.RunId(root="run"), generation=0),
            resource_pool=core.PoolId(root="jobs"),
        ).expected_measurement
        is None
    )


def test_measurement_normalization_requires_owned_job_lifecycle() -> None:
    with pytest.raises(core.ContractError, match="only owned jobs"):
        core.OperationRegistry(
            (
                core.OperationRegistration(
                    descriptor=persisted_turn_codec().descriptors[0],
                    request_model=PersistedTurn,
                    outcome_model=Output,
                    normalize_turn=normalize_turn,
                    normalize_measurement=normalize_measurement,
                ),
            )
        )


@pytest.mark.parametrize("fault", ["decision", "membership"])
def test_registered_turn_migration_requires_exact_request_receipt(fault: str) -> None:
    old = json.loads(
        (Path(__file__).parent / "fixtures" / "envelope-v2-registered-turn.json").read_text()
    )
    intent = old["core"]["intents"]["intents"][0]
    receipt = old["core"]["run"]["receipts"][0]
    if fault == "decision":
        intent["request"]["decision_id"] = {"root": "foreign"}
    else:
        receipt["request_ids"] = []
    codec = persisted_turn_codec()
    with pytest.raises(core.ContractError, match=r"request.*receipt"):
        codec.migrate_envelope(
            core.RunEnvelope[core.StrategyState], json.dumps(old), core.v2_to_v3_migration(codec)
        )


@pytest.mark.parametrize("fault", ["registry", "capability", "schema", "descriptor"])
def test_registered_turn_migration_requires_exact_offered_descriptor(fault: str) -> None:
    old = json.loads(
        (Path(__file__).parent / "fixtures" / "envelope-v2-registered-turn.json").read_text()
    )
    registered = old["core"]["registry"][0]
    offered = old["core"]["run"]["capabilities"]["operations"][0]
    match fault:
        case "registry":
            old["core"]["registry"] = []
        case "capability":
            old["core"]["run"]["capabilities"]["operations"] = []
        case "schema":
            registered["request_schema"] = offered["request_schema"] = {
                "name": "foreign",
                "version": 1,
            }
        case "descriptor":
            offered["inspect"] = False
    codec = persisted_turn_codec()
    with pytest.raises(core.ContractError, match=r"descriptor|registry|schema"):
        codec.migrate_envelope(
            core.RunEnvelope[core.StrategyState], json.dumps(old), core.v2_to_v3_migration(codec)
        )


def changed_measurement(request: core.OperationRequest) -> core.MeasurementIdentity:
    identity = normalize_measurement(request)
    return identity.model_copy(update={"evaluator_digest": f"{identity.evaluator_digest}:changed"})


def _measurement_envelope(
    codec: core.OperationRegistry, fingerprint: str
) -> core.RunEnvelope[core.StrategyState]:
    state = core.initial_state()
    decision = codec.validate_decision(
        core.Operation(
            decision_id=core.DecisionId(root="persisted-measurement"),
            scope=core.Scope(owner=state.run.run_id, generation=0),
            request=RegisteredMeasurement(identity=expected_identity(fingerprint)),
            deadline_at=100.0,
        )
    )
    receipt = core.DecisionReceipt(
        decision_id=decision.decision_id,
        decision=decision,
        payload_digest=value_digest(decision),
        feedback=core.Accepted(decision_id=decision.decision_id),
    )
    state = state.model_copy(
        update={
            "registry": codec.descriptors,
            "run": state.run.model_copy(
                update={
                    "capabilities": core.Capabilities(operations=codec.descriptors),
                    "receipts": (receipt,),
                }
            ),
        }
    )
    return core.RunEnvelope[core.StrategyState](
        schema_version=core.ENVELOPE_SCHEMA_VERSION,
        fence=core.HostFence(host_id=core.HostId(root="host"), epoch=0),
        strategy_id=state.run.declaration.strategy_id,
        state_schema=state.run.declaration.state_schema,
        core=state,
        strategy=core.StrategyState(schema_version=1),
        event_cursor=core.EventCursor(sequence=0),
    )


@pytest.mark.parametrize("change", ["removed", "changed", "added"])
@given(fingerprint=st.text(alphabet="abcdef0123456789", min_size=1, max_size=32))
def test_persisted_measurement_normalization_drift_requires_migration(
    change: str, fingerprint: str
) -> None:
    original = measurement_codec(normalized=change != "added")
    envelope = _measurement_envelope(original, fingerprint)
    replacement = measurement_codec(normalized=change != "removed")
    if change == "changed":
        entry = core.OperationRegistration(
            descriptor=replacement.descriptors[0],
            request_model=RegisteredMeasurement,
            outcome_model=Output,
            normalize_measurement=changed_measurement,
        )
        replacement = core.OperationRegistry((entry,))
    assert original.descriptors == replacement.descriptors
    with pytest.raises(ValueError, match=r"normalized_measurement.*migration"):
        replacement.decode_envelope(
            core.RunEnvelope[core.StrategyState], original.encode_envelope(envelope)
        )


@pytest.mark.parametrize("normalized", [False, True])
@given(fingerprint=st.text(alphabet="abcdef0123456789", min_size=1, max_size=32))
def test_persisted_measurement_identity_and_replay_are_stable(
    fingerprint: str, *, normalized: bool
) -> None:
    codec = measurement_codec(normalized=normalized)
    envelope = _measurement_envelope(codec, fingerprint)
    restored = codec.decode_envelope(
        core.RunEnvelope[core.StrategyState], codec.encode_envelope(envelope)
    )
    receipt = restored.core.run.receipts[0]
    assert receipt == envelope.core.run.receipts[0]
    assert isinstance(receipt.decision, core.Operation)
    assert receipt.payload_digest == value_digest(receipt.decision)
    replay = core.step(
        restored.core,
        core.DecisionSubmitted(
            decision=codec.validate_decision(receipt.decision),
            expected_revision=restored.core.revision,
        ),
    )
    assert replay.events == ()
    assert replay.state.run.receipts == restored.core.run.receipts
    assert replay.requests == ()


@pytest.mark.parametrize("fault", ["omitted", "null", "mismatched"])
@given(fingerprint=st.text(alphabet="abcdef0123456789", min_size=1, max_size=32))
def test_persisted_measurement_value_is_validated_without_repair(
    fault: str, fingerprint: str
) -> None:
    codec = measurement_codec()
    envelope = _measurement_envelope(codec, fingerprint)
    payload = json.loads(codec.encode_envelope(envelope))
    decision = payload["core"]["run"]["receipts"][0]["decision"]
    if fault == "omitted":
        decision.pop("normalized_measurement")
    elif fault == "null":
        decision["normalized_measurement"] = None
    else:
        decision["normalized_measurement"]["evaluator_digest"] = f"{fingerprint}:forged"
    with pytest.raises(ValueError, match=r"normalized_measurement.*migration"):
        codec.decode_envelope(core.RunEnvelope[core.StrategyState], json.dumps(payload))


def test_persisted_registered_turn_uses_the_same_no_repair_boundary() -> None:
    codec = persisted_turn_codec()
    source = (Path(__file__).parent / "fixtures" / "envelope-v2-registered-turn.json").read_text()
    migrated = codec.migrate_envelope(
        core.RunEnvelope[core.StrategyState], source, core.v2_to_v3_migration(codec)
    )
    payload = json.loads(codec.encode_envelope(migrated))
    payload["core"]["run"]["receipts"][0]["decision"]["normalized_turn"] = None
    with pytest.raises(ValueError, match=r"normalized_turn.*migration"):
        codec.decode_envelope(core.RunEnvelope[core.StrategyState], json.dumps(payload))
