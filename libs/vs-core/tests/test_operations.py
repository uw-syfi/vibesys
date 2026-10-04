"""Library-owned operation wire and startup falsification contracts."""

import json
from typing import ClassVar, Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel, ValidationError

from vs_core.api import (
    Accepted,
    Capabilities,
    ContractError,
    DecisionId,
    DecisionSubmitted,
    EnvelopeMigration,
    EventCursor,
    HostFence,
    HostId,
    LifecycleClass,
    Operation,
    OperationDescriptor,
    OperationMigration,
    OperationRegistration,
    OperationRegistry,
    OperationRequest,
    OperationSchemaRef,
    OperationWire,
    Rejected,
    RejectionCode,
    RunEnvelope,
    SchemaRef,
    Scope,
    StrategyState,
    Value,
    initial_state,
    step,
    validate_startup,
)


class ArtifactPutOutcome(Value):
    """A pure owning-library outcome, without importing its execution roles."""

    status: Literal["succeeded", "pending", "cancelled", "rejected", "failed", "unknown"]
    artifact_digest: str


class ArtifactPut(OperationRequest):
    """The owning library defines this value; core never imports that library."""

    kind: Literal["project.artifact.put"] = "project.artifact.put"
    lifecycle: Literal[LifecycleClass.IDEMPOTENT_WRITE] = LifecycleClass.IDEMPOTENT_WRITE
    outcome_model: ClassVar[type[BaseModel]] = ArtifactPutOutcome
    content: str


class JobOutcome(Value):
    status: Literal["succeeded", "pending", "cancelled", "rejected", "failed", "unknown"]
    exit_code: int | None


class OwnedJobRequest(OperationRequest):
    kind: Literal["job.capture"] = "job.capture"
    lifecycle: Literal[LifecycleClass.OWNED_JOB] = LifecycleClass.OWNED_JOB
    outcome_model: ClassVar[type[BaseModel]] = JobOutcome
    command: tuple[str, ...]


def registry() -> OperationRegistry:
    """Closed codec for one write and one owned job."""
    return OperationRegistry(
        (
            OperationRegistration(
                descriptor=OperationDescriptor(
                    kind="project.artifact.put",
                    request_schema=SchemaRef(name="artifact-put", version=1),
                    outcome_schema=SchemaRef(name="artifact-put-outcome", version=1),
                    lifecycle=LifecycleClass.IDEMPOTENT_WRITE,
                    inspect=True,
                ),
                request_model=ArtifactPut,
                outcome_model=ArtifactPutOutcome,
            ),
            OperationRegistration(
                descriptor=OperationDescriptor(
                    kind="job.capture",
                    request_schema=SchemaRef(name="capture", version=1),
                    outcome_schema=SchemaRef(name="capture-outcome", version=1),
                    lifecycle=LifecycleClass.OWNED_JOB,
                    inspect=True,
                    cancel=True,
                    watch=True,
                ),
                request_model=OwnedJobRequest,
                outcome_model=JobOutcome,
            ),
        )
    )


def reference(descriptor: OperationDescriptor) -> OperationSchemaRef:
    return OperationSchemaRef(
        kind=descriptor.kind,
        request_schema=descriptor.request_schema,
        outcome_schema=descriptor.outcome_schema,
        lifecycle=descriptor.lifecycle,
    )


def operation_state() -> tuple[OperationRegistry, RunEnvelope[StrategyState]]:
    codec = registry()
    state = initial_state()
    declaration = state.run.declaration.model_copy(
        update={"required_operations": tuple(reference(item) for item in codec.descriptors)}
    )
    offered = Capabilities(operations=codec.descriptors)
    capabilities = validate_startup(declaration, offered)
    state = state.model_copy(
        update={
            "registry": codec.descriptors,
            "run": state.run.model_copy(
                update={"declaration": declaration, "capabilities": capabilities}
            ),
        }
    )
    envelope = RunEnvelope[StrategyState](
        schema_version=1,
        fence=HostFence(host_id=HostId(root="host"), epoch=1),
        strategy_id=declaration.strategy_id,
        state_schema=declaration.state_schema,
        core=state,
        strategy=StrategyState(schema_version=1),
        event_cursor=EventCursor(sequence=0),
    )
    return codec, envelope


@given(st.text())
def test_library_owned_write_round_trips_atomic_envelope_and_stable_outbox(content: str) -> None:
    codec, envelope = operation_state()
    request = ArtifactPut(content=content)
    wire = codec.encode(request)
    assert type(codec.decode(wire)) is ArtifactPut
    assert codec.decode(wire) == request
    decision = Operation(
        decision_id=DecisionId(root="write"),
        scope=Scope(owner=envelope.core.run.run_id, generation=0),
        request=request,
        deadline_at=100.0,
    )
    decision = codec.validate_decision(decision)
    result = step(envelope.core, DecisionSubmitted(decision=decision, expected_revision=0))
    assert len(result.requests) == 1
    assert result.requests[0].request_id == result.state.intents.intents[0].request_id
    saved = envelope.model_copy(update={"core": result.state})
    loaded = codec.decode_envelope(RunEnvelope[StrategyState], codec.encode_envelope(saved))
    assert loaded == saved
    stored_decision = loaded.core.run.receipts[0].decision
    assert isinstance(stored_decision, Operation)
    assert type(stored_decision.request) is ArtifactPut
    assert loaded.core.intents.intents[0].request_id == result.requests[0].request_id
    assert (
        step(loaded.core, DecisionSubmitted(decision=decision, expected_revision=0)).requests == ()
    )


def test_owned_job_and_validated_outcome_keep_their_original_subtype() -> None:
    codec = registry()
    request = OwnedJobRequest(command=("capture", "immutable-input"))
    wire = codec.encode(request)
    assert codec.decode(wire) == request
    outcome = codec.decode_outcome(
        wire.schema_ref, JobOutcome(status="unknown", exit_code=None).model_dump_json()
    )
    assert type(outcome) is JobOutcome
    assert outcome.exit_code is None


def test_missing_changed_and_unknown_schemas_fail_closed() -> None:
    codec = registry()
    wire = codec.encode(ArtifactPut(content="exact payload"))
    with pytest.raises(ContractError, match="unregistered"):
        OperationRegistry().decode(wire)
    changed = wire.schema_ref.model_copy(
        update={"request_schema": SchemaRef(name="artifact-put", version=2)}
    )
    with pytest.raises(ContractError, match="migration required"):
        codec.decode(wire.model_copy(update={"schema_ref": changed}))
    with pytest.raises(ValidationError):
        codec.decode(
            wire.model_copy(
                update={
                    "payload_json": '{"kind":"project.artifact.put","lifecycle":"idempotent_external_write","content":"x","extra":1}'
                }
            )
        )
    with pytest.raises(ValidationError, match="registered codec"):
        Operation.model_validate_json(
            '{"kind":"operation","decision_id":{"kind":"decision","root":"d"},"scope":{"owner":{"kind":"run","root":"run"},"generation":0},"request":{"kind":"project.artifact.put","lifecycle":"idempotent_external_write","content":"x"},"deadline_at":10.0}'
        )


def test_startup_capabilities_are_the_declared_intersection() -> None:
    state = initial_state()
    declaration = state.run.declaration.model_copy(
        update={"optional": frozenset({"park", "suspend"})}
    )
    result = validate_startup(declaration, Capabilities(lifecycle=frozenset({"park", "steer"})))
    assert result.lifecycle == frozenset({"park"})
    with pytest.raises(ContractError, match="required capability"):
        validate_startup(
            declaration.model_copy(update={"required": frozenset({"interrupt"})}), Capabilities()
        )
    with pytest.raises(ContractError, match="required operation"):
        validate_startup(
            declaration.model_copy(
                update={"required_operations": (reference(registry().descriptors[0]),)}
            ),
            Capabilities(),
        )
    bad = registry().descriptors[1].model_copy(update={"watch": False})
    with pytest.raises(ContractError, match="cancel/watch"):
        validate_startup(declaration, Capabilities(operations=(bad,)))


class WrongArtifactPut(OperationRequest):
    kind: Literal["project.artifact.put"] = "project.artifact.put"
    lifecycle: Literal[LifecycleClass.IDEMPOTENT_WRITE] = LifecycleClass.IDEMPOTENT_WRITE
    outcome_model: ClassVar[type[BaseModel]] = ArtifactPutOutcome
    wrong: str


def test_unregistered_same_kind_model_cannot_enter_outbox_or_poison_receipts() -> None:
    codec, envelope = operation_state()
    proposal = Operation(
        decision_id=DecisionId(root="invalid"),
        scope=Scope(owner=envelope.core.run.run_id, generation=0),
        request=WrongArtifactPut(wrong="wrong-schema"),
        deadline_at=100.0,
    )
    with pytest.raises(ContractError, match="unregistered request model"):
        codec.validate_decision(proposal)
    result = step(envelope.core, DecisionSubmitted(decision=proposal, expected_revision=0))
    assert isinstance(result.events[0], Rejected)
    assert result.events[0].code == RejectionCode.UNKNOWN_SCHEMA
    assert result.requests == ()
    assert result.state.run.receipts[0].decision is None
    saved = envelope.model_copy(update={"core": result.state})
    assert codec.decode_envelope(RunEnvelope[StrategyState], codec.encode_envelope(saved)) == saved


def test_decision_dependencies_keep_authoritative_request_ids() -> None:
    codec, envelope = operation_state()
    scope = Scope(owner=envelope.core.run.run_id, generation=0)
    first = codec.validate_decision(
        Operation(
            decision_id=DecisionId(root="a"),
            scope=scope,
            request=ArtifactPut(content="first"),
            deadline_at=100.0,
        )
    )
    result = step(envelope.core, DecisionSubmitted(decision=first, expected_revision=0))
    assert isinstance(result.events[0], Accepted)
    first_ids = result.events[0].request_ids
    assert first_ids == (result.requests[0].request_id,)
    second = codec.validate_decision(
        Operation(
            decision_id=DecisionId(root="b"),
            scope=scope,
            depends_on=(first.decision_id,),
            request=ArtifactPut(content="dependent"),
            deadline_at=100.0,
        )
    )
    dependent = step(result.state, DecisionSubmitted(decision=second, expected_revision=1))
    assert dependent.requests[0].depends_on == first_ids
    assert isinstance(dependent.events[0], Accepted)
    assert tuple(value.decision_id for value in dependent.events[0].dependencies) == (
        first.decision_id,
    )


@given(st.text(min_size=1, max_size=30))
def test_copied_operation_cannot_reuse_proof_for_changed_payload(content: str) -> None:
    codec, envelope = operation_state()
    original = Operation(
        decision_id=DecisionId(root="copied"),
        scope=Scope(owner=envelope.core.run.run_id, generation=0),
        request=ArtifactPut(content="original"),
        deadline_at=100.0,
    )
    validated = codec.validate_decision(original)
    changed = validated.model_copy(update={"request": ArtifactPut(content="changed:" + content)})
    result = step(envelope.core, DecisionSubmitted(decision=changed, expected_revision=0))
    assert result.requests == ()
    assert isinstance(result.events[0], Rejected)
    assert result.events[0].code == RejectionCode.UNKNOWN_SCHEMA
    assert result.state.intents == envelope.core.intents


class OpenKindRequest(ArtifactPut):
    kind: str = "project.artifact.put"


def test_registered_operation_models_require_closed_literal_tags() -> None:
    codec = registry()
    with pytest.raises(ContractError, match="Literal"):
        OperationRegistry(
            (
                OperationRegistration(
                    descriptor=codec.descriptors[0],
                    request_model=OpenKindRequest,
                    outcome_model=ArtifactPutOutcome,
                ),
            )
        )


def test_envelope_strategy_schema_change_requires_explicit_migration() -> None:
    codec, envelope = operation_state()
    changed = envelope.model_copy(update={"state_schema": SchemaRef(name="changed", version=2)})
    with pytest.raises(ContractError, match="migration"):
        codec.decode_envelope(RunEnvelope[StrategyState], changed.model_dump_json())


def normalize_old_artifact(source: str) -> str:
    payload = json.loads(source)
    payload["content"] = payload.pop("old_content")
    return json.dumps(payload)


def normalize_old_envelope(source: str) -> str:
    payload = json.loads(source)
    payload["schema_version"] = 1
    return json.dumps(payload)


def test_operation_migration_is_explicit_and_validates_original_subtype() -> None:
    codec = registry()
    current = reference(codec.descriptors[0])
    old = current.model_copy(
        update={"request_schema": SchemaRef(name="old-artifact-put", version=1)}
    )
    wire = OperationWire(
        schema_ref=old,
        payload_json=json.dumps(
            {
                "kind": "project.artifact.put",
                "lifecycle": "idempotent_external_write",
                "old_content": "retained",
            }
        ),
    )
    with pytest.raises(ContractError, match="migration"):
        codec.decode(wire)
    converted = codec.migrate_operation(
        wire, OperationMigration(source=old, target=current, rewrite=normalize_old_artifact)
    )
    assert codec.decode(converted) == ArtifactPut(content="retained")
    with pytest.raises(ContractError, match="source"):
        codec.migrate_operation(
            converted,
            OperationMigration(source=old, target=current, rewrite=normalize_old_artifact),
        )


def test_envelope_migration_requires_selected_source_and_registered_target() -> None:
    codec, envelope = operation_state()
    legacy = json.loads(envelope.model_dump_json())
    legacy["schema_version"] = 0
    source = json.dumps(legacy)
    migration = EnvelopeMigration(
        source_version=0, target_version=1, rewrite=normalize_old_envelope
    )
    assert codec.migrate_envelope(RunEnvelope[StrategyState], source, migration) == envelope
    with pytest.raises(ContractError, match="source"):
        codec.migrate_envelope(RunEnvelope[StrategyState], envelope.model_dump_json(), migration)
    with pytest.raises(ContractError, match="target"):
        codec.migrate_envelope(
            RunEnvelope[StrategyState],
            source,
            EnvelopeMigration(source_version=0, target_version=2, rewrite=normalize_old_envelope),
        )
