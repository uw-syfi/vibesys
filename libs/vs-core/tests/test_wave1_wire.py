"""Wave-1 wire contracts reject implicit migration and preserve owner proofs."""

import json
from typing import ClassVar, Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel, ValidationError

from vs_core.api import (
    ENVELOPE_SCHEMA_VERSION,
    OLDEST_SUPPORTED_ENVELOPE_VERSION,
    AttemptId,
    AttemptRef,
    Capabilities,
    ContinuationId,
    ContractError,
    DecisionId,
    DecisionSubmitted,
    EventCursor,
    EventId,
    LifecycleClass,
    Observation,
    ObservationStatus,
    Operation,
    OperationDescriptor,
    OperationNormalizationKind,
    OperationRegistration,
    OperationRegistry,
    OperationRequest,
    Rejected,
    RejectionCode,
    RequestId,
    RequestObserved,
    ResourceId,
    RunEnvelope,
    SchemaRef,
    Scope,
    ScopeReopenNormalization,
    StrategyState,
    TargetObservation,
    Value,
    Wait,
    Within,
    initial_state,
    step,
    upgrade_envelope,
)


class ReopenOutcome(Value):
    """Owning-library proof that admission positively reopened."""

    scope: Scope
    admission: Literal["reopened", "closed", "unknown"]


class ReopenRequest(OperationRequest):
    """Pure registered payload leaves episode assignment to the core."""

    kind: Literal["evaluation.scope.reopen"] = "evaluation.scope.reopen"
    lifecycle: Literal[LifecycleClass.IDEMPOTENT_WRITE] = LifecycleClass.IDEMPOTENT_WRITE
    outcome_model: ClassVar[type[BaseModel]] = ReopenOutcome
    target: ScopeReopenNormalization


def normalize_reopen(request: OperationRequest) -> ScopeReopenNormalization:
    assert isinstance(request, ReopenRequest)
    return request.target


def reopen_registration() -> OperationRegistration:
    return OperationRegistration(
        descriptor=OperationDescriptor(
            kind="evaluation.scope.reopen",
            request_schema=SchemaRef(name="scope-reopen", version=1),
            outcome_schema=SchemaRef(name="scope-reopened", version=1),
            lifecycle=LifecycleClass.IDEMPOTENT_WRITE,
            normalization=OperationNormalizationKind.SCOPE_REOPEN,
            inspect=True,
        ),
        request_model=ReopenRequest,
        outcome_model=ReopenOutcome,
        normalize_scope_reopen=normalize_reopen,
    )


def envelope() -> RunEnvelope[StrategyState]:
    state = initial_state()
    return RunEnvelope[StrategyState](
        schema_version=ENVELOPE_SCHEMA_VERSION,
        strategy_id=state.run.declaration.strategy_id,
        state_schema=state.run.declaration.state_schema,
        core=state,
        strategy=StrategyState(schema_version=1),
        event_cursor=EventCursor(sequence=0),
    )


@given(
    st.integers().filter(
        lambda value: not OLDEST_SUPPORTED_ENVELOPE_VERSION <= value <= ENVELOPE_SCHEMA_VERSION
    )
)
def test_envelope_versions_out_of_range_are_rejected_before_decoding_new_contracts(
    version: int,
) -> None:
    codec = OperationRegistry()
    # Deliberately absent core proves the version barrier precedes nested decode.
    named = rf"schema_version.*envelope version {version} is not supported"
    with pytest.raises(ContractError, match=named):
        codec.decode_envelope(RunEnvelope[StrategyState], json.dumps({"schema_version": version}))
    with pytest.raises(ContractError, match=named):
        upgrade_envelope(json.dumps({"schema_version": version}), ("schema_version",))


@given(st.integers().filter(lambda value: value != ENVELOPE_SCHEMA_VERSION))
def test_a_run_writes_only_the_current_envelope_version(version: int) -> None:
    with pytest.raises(ContractError, match="schema_version"):
        OperationRegistry().encode_envelope(
            envelope().model_copy(update={"schema_version": version})
        )


@pytest.mark.parametrize("version", [None, True, "2", 2.0])
def test_envelope_version_must_be_an_explicit_integer(version: object) -> None:
    with pytest.raises(ContractError, match="migration"):
        OperationRegistry().decode_envelope(
            RunEnvelope[StrategyState], json.dumps({"schema_version": version})
        )


@pytest.mark.parametrize(
    "source",
    ["[" * 100_000, "[" * 100_000 + "]" * 100_000, '{"schema_version":' + "[" * 100_000, "\ud800"],
    ids=["deep-open", "deep-closed", "deep-under-key", "lone-surrogate"],
)
def test_a_hostile_envelope_is_a_contract_error(source: str) -> None:
    with pytest.raises(ContractError):
        OperationRegistry().decode_envelope(RunEnvelope[StrategyState], source)


def test_an_envelope_older_than_the_oldest_supported_names_both_versions() -> None:
    codec = OperationRegistry()
    stored = envelope().model_copy(update={"schema_version": OLDEST_SUPPORTED_ENVELOPE_VERSION - 1})
    with pytest.raises(
        ContractError,
        match=rf"envelope version {OLDEST_SUPPORTED_ENVELOPE_VERSION - 1} is not supported; "
        rf"versions {OLDEST_SUPPORTED_ENVELOPE_VERSION} to {ENVELOPE_SCHEMA_VERSION}",
    ):
        codec.decode_envelope(RunEnvelope[StrategyState], stored.model_dump_json())


@given(
    st.from_regex(RequestId.model_json_schema()["properties"]["root"]["pattern"], fullmatch=True)
)
def test_scope_reopen_normalization_is_bound_to_the_registered_payload(authority: str) -> None:
    codec = OperationRegistry((reopen_registration(),))
    state = initial_state()
    target = ScopeReopenNormalization(
        attempt=AttemptRef(attempt_id=AttemptId(root="a"), generation=0),
        continuation_id=ContinuationId(root="wait"),
        park_authority=RequestId(root=authority),
        resolved_cancelled_jobs=(),
    )
    decision = Operation(
        decision_id=DecisionId(root="reopen"),
        scope=Scope(owner=state.run.run_id, generation=0),
        request=ReopenRequest(target=target),
    )
    validated = codec.validate_decision(decision)
    assert decision.registered_scope_reopen is None
    assert validated.normalized_scope_reopen == target
    assert validated.registered_scope_reopen == target
    assert codec.decode(codec.encode(validated.request)) == decision.request
    restored = Operation.model_validate_json(
        validated.model_dump_json(), context={"operation_registry": codec}
    )
    assert restored.registered_scope_reopen == target


def test_scope_reopen_normalizer_must_match_the_descriptor_declaration() -> None:
    registration = reopen_registration()
    with pytest.raises(ContractError, match="normalize_scope_reopen"):
        OperationRegistry(
            (
                OperationRegistration(
                    descriptor=registration.descriptor,
                    request_model=registration.request_model,
                    outcome_model=registration.outcome_model,
                ),
            )
        )
    with pytest.raises(ValidationError, match="normalization"):
        OperationDescriptor.model_validate(
            {
                **registration.descriptor.model_dump(),
                "lifecycle": LifecycleClass.QUERY,
            }
        )


def observation(request: str, sequence: int) -> Observation:
    state = initial_state()
    return Observation(
        event_id=EventId(root=request),
        request_id=RequestId(root=request),
        scope=Scope(owner=state.run.run_id, generation=0),
        sequence=sequence,
        observed_at=10.0,
        status=ObservationStatus.SUCCEEDED,
    )


def test_nested_inspection_outcome_restores_the_target_registered_subtype() -> None:
    codec = OperationRegistry((reopen_registration(),))
    registration = reopen_registration()
    request = ReopenRequest(
        target=ScopeReopenNormalization(
            attempt=AttemptRef(attempt_id=AttemptId(root="a"), generation=0),
            continuation_id=ContinuationId(root="wait"),
            park_authority=RequestId(root="park"),
            resolved_cancelled_jobs=(),
        )
    )
    wire = codec.encode(request)
    target = observation("original", 1)
    query = observation("query", 4)
    outcome = ReopenOutcome(scope=target.scope, admission="reopened")
    event = codec.validate_event(
        RequestObserved(
            observation=query,
            target=TargetObservation(
                observation=target,
                operation_schema=wire.schema_ref,
                outcome_schema=registration.descriptor.outcome_schema,
                outcome=outcome,
            ),
        )
    )
    assert event.target is not None
    assert event.target.outcome_is_registered
    assert event.target.outcome == outcome
    restored = RequestObserved.model_validate_json(
        event.model_dump_json(), context={"operation_registry": codec}
    )
    assert restored.target is not None
    assert type(restored.target.outcome) is ReopenOutcome
    assert restored.target.observation.request_id != restored.observation.request_id


def test_unknown_target_fields_fail_at_the_contract_boundary() -> None:
    with pytest.raises(ValidationError, match="fabricated"):
        TargetObservation.model_validate(
            {"observation": observation("target", 1), "fabricated": "proof"}
        )


@pytest.mark.parametrize("source", ["{", "[]", "null", "{}", '{"unexpected": 2}'])
def test_envelope_invalid_headers_have_a_named_contract_error(source: str) -> None:
    with pytest.raises(ContractError) as failure:
        OperationRegistry().decode_envelope(RunEnvelope[StrategyState], source)
    assert failure.value.path == ("schema_version",)


def test_undeclared_scope_normalizer_is_rejected() -> None:
    registration = reopen_registration()
    descriptor = registration.descriptor.model_copy(
        update={"normalization": OperationNormalizationKind.NONE}
    )
    with pytest.raises(ContractError, match="normalize_scope_reopen"):
        OperationRegistry(
            (
                OperationRegistration(
                    descriptor=descriptor,
                    request_model=ReopenRequest,
                    outcome_model=ReopenOutcome,
                    normalize_scope_reopen=normalize_reopen,
                ),
            )
        )


@pytest.mark.parametrize("change", ["payload", "normalization"])
def test_copied_scope_reopen_cannot_reuse_a_changed_registered_proof(change: str) -> None:
    codec = OperationRegistry((reopen_registration(),))
    state = initial_state()
    state = state.model_copy(
        update={
            "registry": codec.descriptors,
            "run": state.run.model_copy(
                update={"capabilities": Capabilities(operations=codec.descriptors)}
            ),
        }
    )
    target = ScopeReopenNormalization(
        attempt=AttemptRef(attempt_id=AttemptId(root="a"), generation=0),
        continuation_id=ContinuationId(root="wait"),
        park_authority=RequestId(root="park"),
        resolved_cancelled_jobs=(),
    )
    proposal = codec.validate_decision(
        Operation(
            decision_id=DecisionId(root="reopen"),
            scope=Scope(owner=state.run.run_id, generation=0),
            request=ReopenRequest(target=target),
        )
    )
    changed = target.model_copy(update={"park_authority": RequestId(root="new-park")})
    fields = (
        {"request": ReopenRequest(target=changed)}
        if change == "payload"
        else {"normalized_scope_reopen": changed}
    )
    result = step(
        state,
        DecisionSubmitted(
            decision=Wait(request=proposal.model_copy(update=fields), until=Within(limit=100.0)),
            expected_revision=0,
        ),
    )
    assert result.requests == ()
    assert isinstance(result.events[0], Rejected)
    assert result.events[0].code == RejectionCode.UNKNOWN_SCHEMA


def test_discovered_child_cannot_claim_its_parents_registered_outcome() -> None:
    codec = OperationRegistry((reopen_registration(),))
    request = ReopenRequest(
        target=ScopeReopenNormalization(
            attempt=AttemptRef(attempt_id=AttemptId(root="a"), generation=0),
            continuation_id=ContinuationId(root="wait"),
            park_authority=RequestId(root="park"),
            resolved_cancelled_jobs=(),
        )
    )
    wire = codec.encode(request)
    child = ResourceId(root="child")
    target = observation("original", 1).model_copy(update={"resource_id": child})
    with pytest.raises(ValidationError, match="child lease"):
        TargetObservation(
            observation=target,
            target_resource=child,
            operation_schema=wire.schema_ref,
        )
