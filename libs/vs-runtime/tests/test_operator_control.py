"""The operator control schema and the one entry point every control ingress calls."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from vs_core.api import StopMode
from vs_runtime.api.infrastructure import (
    CONTROL_REQUEST,
    ChannelRunControl,
    ControlStatus,
    PauseRequest,
    ResumeRequest,
    SteerRequest,
    StopRequest,
    create_run_control_channel,
)
from vs_runtime.api.testing import FakeRunControlEventSink
from vs_sim.api.testing import SimThreads

if TYPE_CHECKING:
    from vs_runtime.api.infrastructure import ControlRequest

_TEXT = st.text(min_size=1, max_size=40).filter(str.strip)

requests: st.SearchStrategy[ControlRequest] = st.one_of(
    st.builds(SteerRequest, text=_TEXT),
    st.just(PauseRequest()),
    st.builds(ResumeRequest, fallback=st.booleans()),
    st.just(ResumeRequest(when="capacity_returns")),
    st.builds(StopRequest, mode=st.sampled_from(StopMode)),
)


@given(requests)
def test_every_request_survives_its_json_form(request: ControlRequest) -> None:
    assert CONTROL_REQUEST.validate_json(request.model_dump_json()) == request


@given(requests, st.from_regex(r"[a-z_]{1,12}", fullmatch=True))
def test_a_request_with_a_key_its_action_does_not_declare_is_refused_naming_the_key(
    request: ControlRequest, key: str
) -> None:
    payload = request.model_dump(mode="json")
    if key in payload:
        return
    payload[key] = "x"
    with pytest.raises(ValidationError) as refused:
        CONTROL_REQUEST.validate_python(payload)
    assert any(error["loc"][-1] == key for error in refused.value.errors())


def test_an_unknown_action_is_refused() -> None:
    with pytest.raises(ValidationError, match="action"):
        CONTROL_REQUEST.validate_python({"action": "rewrite_checker"})


def test_fallback_cannot_wait_for_capacity() -> None:
    with pytest.raises(ValidationError, match="fallback resumes now"):
        CONTROL_REQUEST.validate_python(
            {"action": "resume", "fallback": True, "when": "capacity_returns"}
        )


_MEASUREMENT_WORDS = re.compile(
    r"eval|check|gate|bench|measur|metric|accura|trust|accept|partition|pool|resource|command",
    re.IGNORECASE,
)
"""Words for what decides a candidate's fate; no control field may name one."""


def _leaf_types(spec: dict[str, Any], definitions: dict[str, Any]) -> set[str]:
    """The JSON types a field may hold, following references to the enums it names."""
    if "$ref" in spec:
        target = definitions[spec["$ref"].rsplit("/", 1)[-1]]
        assert "enum" in target, f"{spec['$ref']} is a structure, not a closed choice"
        return {target.get("type", "string")}
    if "anyOf" in spec:
        return set().union(*(_leaf_types(option, definitions) for option in spec["anyOf"]))
    return {spec["type"]} if "type" in spec else {"string"}


def test_no_control_field_reaches_measurement_or_acceptance() -> None:
    """Steering cannot touch evaluation, checkers, trust rules or resource partitions.

    It holds by construction: no field is named for one, and every field is a scalar (text,
    a flag, or a closed choice), so no field can carry an evaluation command, a checker, a
    trust rule or a resource partition.
    """
    schema = CONTROL_REQUEST.json_schema()
    definitions = schema.get("$defs", {})
    fields = [
        (name, spec)
        for definition in definitions.values()
        for name, spec in definition.get("properties", {}).items()
    ]
    assert fields
    for name, spec in fields:
        assert not _MEASUREMENT_WORDS.search(name), name
        assert _leaf_types(spec, definitions) <= {"string", "boolean"}, name


@dataclass
class _Expected:
    """What the channel should hold after a sequence of controls."""

    steers: list[str] = field(default_factory=list)
    paused: bool = False
    stop: StopMode | None = None


@given(st.lists(requests, max_size=12))
def test_submit_applies_each_request_to_the_run_as_its_action_says(
    sequence: list[ControlRequest],
) -> None:
    channel = create_run_control_channel(FakeRunControlEventSink(), threads=SimThreads())
    control = ChannelRunControl(channel)
    expected = _Expected()
    for request in sequence:
        ack = control.submit(request)
        assert ack.action == request.action
        match request:
            case SteerRequest(text=text):
                expected.steers.append(text)
                assert ack.status is ControlStatus.PENDING
            case PauseRequest():
                expected.paused = True
                assert ack.status is ControlStatus.PENDING
            case ResumeRequest(when="capacity_returns"):
                # No turn is parked on a capacity limit here, so the wait is refused.
                assert ack.status is ControlStatus.REFUSED
            case ResumeRequest():
                expected.paused = False
                expected.stop = None
                assert ack.status is ControlStatus.CONSUMED
            case StopRequest(mode=mode):
                expected.stop = mode
                assert ack.status is ControlStatus.PENDING
    assert channel.take_pending_steer() == expected.steers
    assert channel.pause_requested() == expected.paused
    assert channel.stop_mode() == expected.stop
