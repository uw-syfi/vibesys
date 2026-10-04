"""Registered pure normalizers cannot bypass typed immutable contract ingress."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, ClassVar, Literal, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic import BaseModel


class Outcome(core.Value):
    status: Literal["succeeded"] = "succeeded"


class TurnOperation(core.OperationRequest):
    kind: Literal["test.normalized-turn"] = "test.normalized-turn"
    lifecycle: Literal[core.LifecycleClass.SESSION_TURN] = core.LifecycleClass.SESSION_TURN
    outcome_model: ClassVar[type[BaseModel]] = Outcome


class BadNormalization(StrEnum):
    NONE = "none"
    DICT = "dict"
    WRONG_MODEL = "wrong-model"
    MISSING_FIELDS = "missing-fields"
    INVALID_VALUE = "invalid-value"
    MUTABLE_VALUE = "mutable-value"


def _turn() -> core.TurnSpec:
    return core.TurnSpec(
        session=core.SessionSpec(
            session_id=core.SessionId(root="session"),
            role_id=core.RoleId(root="role"),
            policy="fresh",
            lifetime="ephemeral",
            access=core.Access.READ_ONLY,
        ),
        invocation_id=core.InvocationId(root="invocation"),
        workspace=core.Scope(owner=core.RunId(root="run"), generation=0),
        prompts=(),
        output_schema=core.SchemaRef(name="output", version=1),
        deadline_at=10.0,
        charge_class="paid",
    )


def _reopen() -> core.ScopeReopenNormalization:
    return core.ScopeReopenNormalization(
        attempt=core.AttemptRef(attempt_id=core.AttemptId(root="attempt"), generation=0),
        continuation_id=core.ContinuationId(root="continuation"),
        park_authority=core.RequestId(root="park"),
        resolved_cancelled_jobs=(),
    )


def _registry(
    family: Literal["turn", "reopen"], returned: object
) -> tuple[core.OperationRegistry, core.OperationRequest]:
    # These callables deliberately violate the extension's return annotation to
    # exercise runtime ingress. No live implementation or module is replaced.
    if family == "turn":
        normalizer = cast("Callable[[core.OperationRequest], core.TurnSpec]", lambda _: returned)
        registration = core.OperationRegistration(
            descriptor=core.OperationDescriptor(
                kind="test.normalized-turn",
                request_schema=core.SchemaRef(name="turn", version=1),
                outcome_schema=core.SchemaRef(name="outcome", version=1),
                lifecycle=core.LifecycleClass.SESSION_TURN,
                inspect=True,
                cancel=True,
                watch=True,
            ),
            request_model=TurnOperation,
            outcome_model=Outcome,
            normalize_turn=normalizer,
        )
        request: core.OperationRequest = TurnOperation()
    else:
        reopen_normalizer = cast(
            "Callable[[core.OperationRequest], core.ScopeReopenNormalization]", lambda _: returned
        )
        registration = core.OperationRegistration(
            descriptor=core.OperationDescriptor(
                kind="evaluation.scope.reopen",
                request_schema=core.SchemaRef(name="reopen", version=1),
                outcome_schema=core.SchemaRef(name="reopened", version=1),
                lifecycle=core.LifecycleClass.IDEMPOTENT_WRITE,
                inspect=True,
                normalization=core.OperationNormalizationKind.SCOPE_REOPEN,
            ),
            request_model=core.ScopedAdmissionReopen,
            outcome_model=core.ScopedAdmissionReopenOutcome,
            normalize_scope_reopen=reopen_normalizer,
        )
        request = core.ScopedAdmissionReopen(**_reopen().model_dump(exclude={"kind"}))
    return core.OperationRegistry((registration,)), request


def _bad_result(family: Literal["turn", "reopen"], bad: BadNormalization) -> object:
    valid = _turn() if family == "turn" else _reopen()
    match bad:
        case BadNormalization.NONE:
            return None
        case BadNormalization.DICT:
            return valid.model_dump()
        case BadNormalization.WRONG_MODEL:
            return Outcome()
        case BadNormalization.MISSING_FIELDS:
            return type(valid).model_construct()
        case BadNormalization.INVALID_VALUE:
            update = (
                {"max_turns": 0}
                if family == "turn"
                else {
                    "resolved_cancelled_jobs": (
                        core.ResourceId(root="job"),
                        core.ResourceId(root="job"),
                    )
                }
            )
            return valid.model_copy(update=update)
        case BadNormalization.MUTABLE_VALUE:
            update = {"prompts": []} if family == "turn" else {"resolved_cancelled_jobs": []}
            return valid.model_copy(update=update)


@pytest.mark.parametrize("family", ["turn", "reopen"])
@given(bad=st.sampled_from(tuple(BadNormalization)))
def test_bad_normalizer_results_raise_named_contract_error(
    family: Literal["turn", "reopen"], bad: BadNormalization
) -> None:
    registry, request = _registry(family, _bad_result(family, bad))
    normalize = registry.normalize_turn if family == "turn" else registry.normalize_scope_reopen
    with pytest.raises(core.ContractError) as raised:
        normalize(request)
    assert raised.value.path == (
        "operation",
        request.kind,
        f"normalize_{family if family == 'turn' else 'scope_reopen'}",
    )


@pytest.mark.parametrize("family", ["turn", "reopen"])
def test_valid_normalization_preserves_contract_and_input(
    family: Literal["turn", "reopen"],
) -> None:
    value = _turn() if family == "turn" else _reopen()
    before = value.model_dump_json()
    registry, request = _registry(family, value)
    normalize = registry.normalize_turn if family == "turn" else registry.normalize_scope_reopen
    normalized = normalize(request)
    assert normalized == value
    assert value.model_dump_json() == before
    assert normalized is not value


@pytest.mark.parametrize("family", ["turn", "reopen"])
@given(invalid=st.sampled_from((False, 0, "normalizer", (), {})))
def test_noncallable_normalizers_fail_at_registration(
    family: Literal["turn", "reopen"], invalid: object
) -> None:
    registry, _ = _registry(family, _turn() if family == "turn" else _reopen())
    descriptor = registry.descriptors[0]
    if family == "turn":
        entry = core.OperationRegistration(
            descriptor=descriptor,
            request_model=TurnOperation,
            outcome_model=Outcome,
            normalize_turn=cast("Callable[[core.OperationRequest], core.TurnSpec]", invalid),
        )
    else:
        entry = core.OperationRegistration(
            descriptor=descriptor,
            request_model=core.ScopedAdmissionReopen,
            outcome_model=core.ScopedAdmissionReopenOutcome,
            normalize_scope_reopen=cast(
                "Callable[[core.OperationRequest], core.ScopeReopenNormalization]", invalid
            ),
        )
    with pytest.raises(core.ContractError) as raised:
        core.OperationRegistry((entry,))
    expected = "normalize_turn" if family == "turn" else "normalize_scope_reopen"
    assert raised.value.path == ("registry", 0, expected)
