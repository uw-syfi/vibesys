"""Generated values preserve the old envelope and validate both sides of the proto boundary."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from typing import TYPE_CHECKING, get_args

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from pydantic import TypeAdapter, ValidationError

import vs_core.api as core
from vs_core.api import values as shared_values
from vs_core.api.testing import sample_limits, sample_retries
from vs_proto.api import LegacyEvent, core_agent_facts, core_common
from vs_proto.api import ValidationError as ProtoValidationError
from vs_proto.api import core_control as core_state
from vs_runtime.api.core_values import from_proto, to_proto, value_types

if TYPE_CHECKING:
    from collections.abc import Callable

_ROOT = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_/é", min_size=1, max_size=30)


def _sample(cls: type[core.CoreValue], root: str, count: int) -> core.CoreValue:
    identity = core.ControlId(root=root)
    scope = core.Scope(owner=core.RunId(root=root), generation=count)
    artifact = core.ArtifactRef(artifact_id=core.ArtifactId(root=root), digest=root)
    invocation = core.InvocationRef(
        session_id=core.SessionId(root=root),
        invocation_id=core.InvocationId(root=root),
        generation=count,
    )
    values: dict[type[core.CoreValue], Callable[[], core.CoreValue]] = {
        core.TimestampValue: lambda: core.TimestampValue(seconds=-count, nanos=count),
        core.SchemaRef: lambda: core.SchemaRef(name=root, version=count + 1),
        core.ArtifactRef: lambda: artifact,
        core.Scope: lambda: scope,
        core.InvocationRef: lambda: invocation,
        core.ScopeInputTarget: lambda: core.ScopeInputTarget(scope=scope),
        core.ItemInputTarget: lambda: core.ItemInputTarget(item_id=core.ItemId(root=root)),
        core.InvocationInputTarget: lambda: core.InvocationInputTarget(invocation=invocation),
        core.RoleInputTarget: lambda: core.RoleInputTarget(role_id=core.RoleId(root=root)),
        core.PoolCapacity: lambda: core.PoolCapacity(
            pool_id=core.PoolId(root=root), capacity=count + 1
        ),
        core.RequestRetries: lambda: sample_retries(count % 11),
        core.Limits: lambda: sample_limits(max_parallel=count + 1),
        core.RunPark: lambda: core.RunPark(control_id=identity, epoch=count),
        core.RunPause: lambda: core.RunPause(
            owner=core.PauseOwner.OPERATOR, capacity_wait=bool(count % 2)
        ),
        core.ControlInput: lambda: core.ControlInput(
            control_id=identity, action="pause", owner=core.PauseOwner.QUOTA
        ),
    }
    if issubclass(cls, core.Identity):
        return cls(root=root)
    if cls.__name__ in _shared_seeds():
        return TypeAdapter(cls).validate_json(json.dumps(_shared_seeds()[cls.__name__]))
    return values[cls]()


@pytest.mark.parametrize("cls", value_types())
@given(root=_ROOT, count=st.integers(min_value=0, max_value=100))
def test_every_generated_value_roundtrips_through_proto_and_both_json_forms(
    cls: type[core.CoreValue], root: str, count: int
) -> None:
    value = _sample(cls, root, count)
    assert not hasattr(value, "__dict__")
    message = to_proto(value)
    assert from_proto(type(message).from_binary(message.to_binary())) == value
    assert from_proto(type(message).from_json(message.to_json())) == value
    assert TypeAdapter(cls).validate_json(value.model_dump_json()) == value
    assert hash(value) == hash(TypeAdapter(cls).validate_json(value.model_dump_json()))
    with pytest.raises(FrozenInstanceError):
        setattr(value, fields(value)[0].name, None)


@pytest.mark.parametrize(("name", "cls"), [("control", core.ControlInput), ("limits", core.Limits)])
def test_differential_acceptance_and_json_against_the_pre_migration_validators(
    name: str, cls: type[core.CoreValue]
) -> None:
    # Captured by running the old public constructors before deleting them. The fixture
    # pins both decisions and normalized JSON, rather than preserving a second validator.
    path = Path(__file__).parent / "fixtures" / "core-values" / f"{name}.json"
    for case in json.loads(path.read_text()):
        source = case["source"]
        if case["accepted"]:
            value = cls.model_validate_json(source)
            assert json.loads(value.model_dump_json()) == case["output"]
            assert from_proto(to_proto(value)) == value
        else:
            with pytest.raises(ValidationError):
                cls.model_validate_json(source)


@given(
    root=_ROOT,
    action=st.sampled_from(get_args(core.ControlAction.__value__)),
)
def test_every_control_action_roundtrips(root: str, action: core.ControlAction) -> None:
    notice = core.ArtifactRef(artifact_id=core.ArtifactId(root=root), digest=root)
    choices: dict[str, dict[str, object]] = {
        "park": {},
        "pause": {"owner": core.PauseOwner.OPERATOR},
        "resume": {"when": "now"},
        "stop": {"mode": core.StopMode.CANCEL},
        "steer": {
            "artifact": notice,
            "target": core.RoleInputTarget(role_id=core.RoleId(root=root)),
            "input_mode": core.InputMode.STANDING,
        },
        "withdraw": {"withdraws": core.InputId(root=root)},
        "cancel_attempt": {"attempt": core.AttemptId(root=root), "artifact": notice},
        "cancel_item": {"item": core.ItemId(root=root), "artifact": notice},
        "set_max_parallel": {"max_parallel": 3, "artifact": notice},
    }
    extra = choices[action]
    value = core.ControlInput.model_validate(
        {"control_id": core.ControlId(root=root), "action": action, **extra}
    )
    assert from_proto(to_proto(value)) == value


@given(values=st.dictionaries(_ROOT, st.integers()), reverse=st.booleans())
def test_generated_maps_are_immutable_and_preserve_json_spelling(
    values: dict[str, int], *, reverse: bool
) -> None:
    items = dict(reversed(tuple(values.items()))) if reverse else values
    frozen = core.FrozenMap(items)
    values_copy = dict(items)
    items.clear()
    assert dict(frozen) == values_copy
    assert hash(frozen) == hash(core.FrozenMap(dict(reversed(tuple(values_copy.items())))))
    adapter = TypeAdapter(core.FrozenMap[str, int])
    assert adapter.validate_json(adapter.dump_json(frozen)) == frozen
    with pytest.raises(AttributeError):
        frozen.extra = 1


@given(count=st.integers(min_value=2**63, max_value=10**150))
def test_unbounded_legacy_counters_roundtrip_without_int64_narrowing(count: int) -> None:
    value = sample_limits(max_parallel=count, max_attempts=count)
    assert from_proto(to_proto(value)) == value


@pytest.mark.parametrize("value", [object(), 1, "unknown"])
def test_unsupported_values_fail_by_name(value: object) -> None:
    with pytest.raises(TypeError, match="Unsupported core value"):
        to_proto(value)


def test_unsupported_valid_proto_messages_fail_by_name() -> None:
    with pytest.raises(TypeError, match="Unsupported core message"):
        from_proto(LegacyEvent(json="{}"))


@given(owner=st.sampled_from([core_common.PauseOwner.UNSPECIFIED, core_common.PauseOwner(99)]))
def test_proto_validation_runs_before_core_values_are_constructed(
    owner: core_common.PauseOwner,
) -> None:
    message = core_state.ControlInput(
        control_id=core_common.ControlId(root="c", kind="control"),
        action=core_common.ControlAction.PAUSE,
        owner=owner,
    )
    with pytest.raises(ProtoValidationError):
        from_proto(message)


@given(version=st.integers(max_value=0))
def test_schema_version_scalar_is_rejected_at_construction(version: int) -> None:
    with pytest.raises(ValidationError, match="version"):
        core.SchemaRef(name="schema", version=version)


_IDENTITIES = tuple(cls for cls in value_types() if issubclass(cls, core.Identity))


@given(source=st.sampled_from(_IDENTITIES), target=st.sampled_from(_IDENTITIES), root=_ROOT)
@example(source=core.RunId, target=core.Identity, root="c")
def test_nominal_identity_domains_cannot_be_substituted(
    source: type[core.Identity], target: type[core.Identity], root: str
) -> None:
    value = source(root=root)
    if source is target:
        assert target.model_validate(value) == value
    else:
        with pytest.raises(ValidationError):
            target.model_validate(value)


def _shared_seeds() -> dict[str, object]:
    path = Path(__file__).parent / "fixtures" / "core-values" / "shared-seeds.json"
    return json.loads(path.read_text())


def test_shared_value_differential_fixtures_preserve_old_acceptance_and_json() -> None:
    path = Path(__file__).parent / "fixtures" / "core-values" / "shared-values.json"
    for case in json.loads(path.read_text()):
        cls = getattr(shared_values, case["type"])
        if case["accepted"]:
            value = TypeAdapter(cls).validate_json(case["source"])
            assert json.loads(value.model_dump_json()) == case["output"], case
            assert from_proto(to_proto(value)) == value
        else:
            with pytest.raises(ValidationError):
                TypeAdapter(cls).validate_json(case["source"])


@given(
    field=st.sampled_from(["source_request", "scope", "observation_sequence", "status"]),
    sequence=st.integers(min_value=1, max_value=10**100),
)
@example(field="observation_sequence", sequence=2**100)
def test_immutable_acceptance_proof_stays_bound_to_its_original_evidence(
    field: str, sequence: int
) -> None:
    value = shared_values.EvidenceRef.model_validate_json(
        json.dumps(_shared_seeds()["EvidenceRef"])
    )
    message = to_proto(value)
    assert isinstance(message, core_agent_facts.EvidenceRef)
    receipt = message.acceptance_receipt
    assert receipt is not None
    if field == "source_request":
        assert receipt.source_request is not None
        receipt.source_request.root = "other-source"
    elif field == "scope":
        assert receipt.scope is not None
        receipt.scope.generation = str(sequence)
    elif field == "observation_sequence":
        receipt.observation_sequence = str(value.observation_sequence + sequence)
    else:
        receipt.status = type(receipt.status).FAILED
    with pytest.raises(ProtoValidationError):
        from_proto(message)
    payload = json.loads(value.model_dump_json())
    changed = payload["acceptance_receipt"]
    if field == "source_request":
        changed[field]["root"] = "other-source"
    elif field == "scope":
        changed[field]["generation"] = sequence
    elif field == "observation_sequence":
        changed[field] += sequence
    else:
        changed[field] = "failed"
    with pytest.raises(ValidationError, match="acceptance_receipt"):
        shared_values.EvidenceRef.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("name", ["LifecycleClass", "ObservationStatus"])
def test_shared_enum_dependencies_keep_the_existing_public_identity(name: str) -> None:
    assert getattr(core, name) is getattr(shared_values, name)
