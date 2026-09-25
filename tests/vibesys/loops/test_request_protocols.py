"""The loop-side structural request view must stay in sync with the public DTO."""

from enum import StrEnum

import pytest

from vibesys.api.contracts import LoopKind, ResumeRef, RunRequest
from vibesys.loops.request import LoopKindValue, LoopRunRequest, ResumeReference


def _declared_properties(protocol: type) -> dict[str, property]:
    return {name: member for name, member in vars(protocol).items() if isinstance(member, property)}


def test_every_loop_request_property_is_a_run_request_field() -> None:
    declared = _declared_properties(LoopRunRequest)
    assert declared, "LoopRunRequest declares no read-only properties"
    missing = sorted(set(declared) - set(RunRequest.model_fields))
    assert missing == []


def test_every_declared_property_is_read_only_and_documented() -> None:
    for protocol in (LoopRunRequest, LoopKindValue, ResumeReference):
        for name, member in _declared_properties(protocol).items():
            assert member.fset is None, f"{protocol.__name__}.{name} must be read-only"
            assert member.__doc__, f"{protocol.__name__}.{name} needs a docstring"


@pytest.mark.parametrize("protocol", [LoopRunRequest, LoopKindValue, ResumeReference])
def test_protocol_members_are_bodiless_stubs(protocol: type) -> None:
    for name, member in _declared_properties(protocol).items():
        assert member.fget is not None
        assert member.fget(None) is None, f"{protocol.__name__}.{name} must not compute anything"


def test_loop_kind_matches_the_value_protocol() -> None:
    assert issubclass(LoopKind, StrEnum)
    assert set(_declared_properties(LoopKindValue)) == {"value"}
    assert all(isinstance(kind.value, str) for kind in LoopKind)


def test_resume_ref_matches_the_resume_protocol() -> None:
    assert set(_declared_properties(ResumeReference)) == {"run_id"}
    assert set(ResumeRef.model_fields) >= {"run_id"}
    assert ResumeRef(run_id="run-1").run_id == "run-1"
