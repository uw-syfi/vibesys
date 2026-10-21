"""The operator control schema and the one entry point every control ingress calls."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Any, Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from vs_core.api import StopMode
from vs_runtime.api.infrastructure import (
    CONTROL_REQUEST,
    CancelAttemptRequest,
    CancelItemRequest,
    ChannelRunControl,
    ControlAck,
    ControlAction,
    ControlStatus,
    PauseRequest,
    RefusalReason,
    ResumeRequest,
    SetMaxParallelRequest,
    SteerMode,
    SteerRequest,
    StopRequest,
    WithdrawRequest,
    create_run_control_channel,
)
from vs_runtime.api.testing import (
    FakeRunControlEventSink,
    UnsafeControlSchemaError,
    control_schema_fields,
)
from vs_sim.api.testing import SimThreads

if TYPE_CHECKING:
    from vs_runtime.api.infrastructure import ControlRequest

_TEXT = st.text(min_size=1, max_size=40).filter(str.strip)
_IDS = st.sampled_from(["a", "b", "c", "d"])


def _requests(ids: st.SearchStrategy[str]) -> st.SearchStrategy[ControlRequest]:
    return st.one_of(
        st.builds(SteerRequest, control_id=ids, text=_TEXT),
        st.builds(PauseRequest, control_id=ids),
        st.builds(ResumeRequest, control_id=ids, fallback=st.booleans()),
        st.builds(ResumeRequest, control_id=ids, when=st.just("capacity_returns")),
        st.builds(StopRequest, control_id=ids, mode=st.sampled_from(StopMode)),
        st.builds(SteerRequest, control_id=ids, text=_TEXT, mode=st.just(SteerMode.STANDING)),
        st.builds(WithdrawRequest, control_id=ids, withdraws=ids),
        st.builds(CancelAttemptRequest, control_id=ids, attempt=ids),
        st.builds(CancelItemRequest, control_id=ids, item=ids),
        st.builds(
            SetMaxParallelRequest,
            control_id=ids,
            max_parallel=st.integers(min_value=1, max_value=8),
        ),
    )


requests = _requests(st.from_regex(r"[A-Za-z0-9][A-Za-z0-9._-]{0,20}", fullmatch=True))


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
        CONTROL_REQUEST.validate_python({"control_id": "c", "action": "rewrite_checker"})


@pytest.mark.parametrize("control_id", [None, "", "../x", "a b", ".hidden", "x" * 129])
def test_a_request_needs_a_control_id_that_is_one_name(control_id: str | None) -> None:
    payload: dict[str, object] = {"action": "pause"}
    if control_id is not None:
        payload["control_id"] = control_id
    with pytest.raises(ValidationError, match="control_id"):
        CONTROL_REQUEST.validate_python(payload)


def test_fallback_cannot_wait_for_capacity() -> None:
    with pytest.raises(ValidationError, match="fallback resumes now"):
        CONTROL_REQUEST.validate_python(
            {"control_id": "c", "action": "resume", "fallback": True, "when": "capacity_returns"}
        )


_REVIEWED_FIELDS = frozenset(
    {
        ("steer", "control_id", "string"),
        ("steer", "action", "string"),
        ("steer", "text", "string"),
        ("steer", "target", "model:SteerTarget"),
        ("steer", "target.kind", "enum:SteerTargetKind"),
        ("steer", "target.id", "string?"),
        ("steer", "mode", "enum:SteerMode"),
        ("withdraw", "control_id", "string"),
        ("withdraw", "action", "string"),
        ("withdraw", "withdraws", "string"),
        ("park", "control_id", "string"),
        ("park", "action", "string"),
        ("pause", "control_id", "string"),
        ("pause", "action", "string"),
        ("resume", "control_id", "string"),
        ("resume", "action", "string"),
        ("resume", "fallback", "boolean"),
        ("resume", "when", "string"),
        ("stop", "control_id", "string"),
        ("stop", "action", "string"),
        ("stop", "mode", "enum:StopMode"),
        ("cancel_attempt", "control_id", "string"),
        ("cancel_attempt", "action", "string"),
        ("cancel_attempt", "attempt", "string"),
        ("cancel_item", "control_id", "string"),
        ("cancel_item", "action", "string"),
        ("cancel_item", "item", "string"),
        ("set_max_parallel", "control_id", "string"),
        ("set_max_parallel", "action", "string"),
        ("set_max_parallel", "max_parallel", "integer"),
    }
)
"""Every field a sender can fill, by action, name and JSON type, as reviewed.

A steer changes what agents are told. ``cancel_attempt`` and ``cancel_item`` change
selection: a cancelled attempt is never judged, so it can win nothing, and whoever can reach
a control ingress can aim one at a rival (``docs/contributing/sandboxing.md`` says which
agent confinement leaves the control socket reachable). ``set_max_parallel`` only lowers how
much runs at once. No field here carries an evaluation command, a checker, a trust rule or a
resource partition. A new field fails ``test_every_control_field_is_reviewed`` until it is
added here.
"""

_REVIEWED_MODELS = frozenset({"SteerTarget"})
"""The structures a control may hold: a steer's target names a role, attempt or item."""


def test_every_control_field_is_reviewed() -> None:
    schema = CONTROL_REQUEST.json_schema()
    assert control_schema_fields(schema, models=_REVIEWED_MODELS) == _REVIEWED_FIELDS


def test_the_tags_of_the_union_are_the_control_actions() -> None:
    mapping = CONTROL_REQUEST.json_schema()["discriminator"]["mapping"]
    assert set(mapping) == set(ControlAction)


class _Target(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item: str
    attempt: int | None = None


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Pad(_Strict):
    action: Literal["pad"] = "pad"


class _Allowed(_Strict):
    action: Literal["cancel"] = "cancel"
    count: int
    target: _Target


class _Untyped(_Strict):
    action: Literal["a"] = "a"
    anything: Any


class _Nested(_Strict):
    action: Literal["b"] = "b"
    either: str | int


class _Listed(_Strict):
    action: Literal["c"] = "c"
    texts: list[str]


class _Open(_Strict):
    action: Literal["d"] = "d"
    extra: dict[str, str]


class _UnreviewedModel(_Strict):
    action: Literal["e"] = "e"
    target: _Target


class _Ignoring(BaseModel):
    model_config = ConfigDict(extra="ignore")
    action: Literal["f"] = "f"


class _IgnoringTarget(BaseModel):
    model_config = ConfigDict(extra="ignore")
    item: str


class _IgnoringNested(_Strict):
    action: Literal["g"] = "g"
    target: _IgnoringTarget


_ALLOWED = TypeAdapter(Annotated[_Allowed | _Pad, Field(discriminator="action")])
_UNSAFE = {
    "any": TypeAdapter(Annotated[_Untyped | _Pad, Field(discriminator="action")]),
    "nested union": TypeAdapter(Annotated[_Nested | _Pad, Field(discriminator="action")]),
    "list": TypeAdapter(Annotated[_Listed | _Pad, Field(discriminator="action")]),
    "open object": TypeAdapter(Annotated[_Open | _Pad, Field(discriminator="action")]),
    "unreviewed model": TypeAdapter(
        Annotated[_UnreviewedModel | _Pad, Field(discriminator="action")]
    ),
    "member ignoring unknown keys": TypeAdapter(
        Annotated[_Ignoring | _Pad, Field(discriminator="action")]
    ),
    "nested model ignoring unknown keys": TypeAdapter(
        Annotated[_IgnoringNested | _Pad, Field(discriminator="action")]
    ),
}
_UNSAFE_MODELS = frozenset({"_IgnoringTarget"})
"""Allowed, so the nested case fails on its open keys, not on its being unreviewed."""


def test_the_check_lists_integers_and_allowed_models_field_by_field() -> None:
    schema = _ALLOWED.json_schema()

    assert control_schema_fields(schema, models=frozenset({"_Target"})) == {
        ("cancel", "action", "string"),
        ("cancel", "count", "integer"),
        ("cancel", "target", "model:_Target"),
        ("cancel", "target.item", "string"),
        ("cancel", "target.attempt", "integer?"),
        ("pad", "action", "string"),
    }


@pytest.mark.parametrize("case", sorted(_UNSAFE))
def test_the_check_refuses_a_field_without_one_reviewed_type(case: str) -> None:
    with pytest.raises(UnsafeControlSchemaError):
        control_schema_fields(_UNSAFE[case].json_schema(), models=_UNSAFE_MODELS)


@dataclass
class _Expected:
    """What the channel should hold after a sequence of controls."""

    steers: list[str] = field(default_factory=list)
    withdrawals: list[str] = field(default_factory=list)
    actions: list[ControlRequest] = field(default_factory=list)
    paused: bool = False
    quota_paused: bool = False
    stop: StopMode | None = None
    fallback: bool = False
    answered: dict[str, tuple[ControlRequest, ControlAck]] = field(default_factory=dict)


def _queued(request: ControlRequest, expected: _Expected) -> bool:
    """Record a request the channel only queues for the run to take; False for any other.

    With no check installed, the run decides at take whether a withdrawal's guidance stands
    and whether an action's subject is there.
    """
    match request:
        case SteerRequest(text=text):
            expected.steers.append(text)
        case WithdrawRequest(withdraws=withdraws):
            expected.withdrawals.append(withdraws)
        case CancelAttemptRequest() | CancelItemRequest() | SetMaxParallelRequest():
            expected.actions.append(request)
        case _:
            return False
    return True


def _expected_status(request: ControlRequest, expected: _Expected) -> ControlStatus:
    if _queued(request, expected):
        return ControlStatus.PENDING
    match request:
        case PauseRequest():
            expected.paused = True
            return ControlStatus.PENDING
        case ResumeRequest(when=when, fallback=fallback) if when == "capacity_returns" or (
            fallback and (not expected.quota_paused or expected.stop)
        ):
            # No turn is parked for the operator here, so a wait for capacity is refused; a
            # switch to the fallback answers only a quota stop's pause with a turn waiting.
            return ControlStatus.REFUSED
        case ResumeRequest(fallback=fallback):
            expected.paused = expected.quota_paused = False
            expected.stop = None
            expected.fallback = fallback
            return ControlStatus.CONSUMED
        case _:
            assert isinstance(request, StopRequest)
            expected.stop = request.mode
            return ControlStatus.PENDING


@given(sequence=st.lists(_requests(_IDS), max_size=16), quota_paused=st.booleans())
def test_submit_applies_each_control_id_once_as_its_action_says(
    *, sequence: list[ControlRequest], quota_paused: bool
) -> None:
    channel = create_run_control_channel(FakeRunControlEventSink(), threads=SimThreads())
    control = ChannelRunControl(channel, SimThreads())
    expected = _Expected(paused=quota_paused, quota_paused=quota_paused)
    if quota_paused:
        # A turn waits on the pause's timed resume, never leaving: it would take a fallback.
        channel.enter_capacity(timed=True)
        channel.pause_for_quota()
    for request in sequence:
        stopping = expected.stop is not None
        ack = control.submit(request)
        assert (ack.control_id, ack.action) == (request.control_id, request.action)
        answered = expected.answered.get(request.control_id)
        if answered is None:
            assert ack.status is _expected_status(request, expected)
            expected.answered[request.control_id] = (request, ack)
        elif answered[0] == request:
            assert ack == answered[1]
        else:
            assert ack.status is ControlStatus.CONFLICT
        if ack.status is ControlStatus.REFUSED and answered is None:
            assert ack.reason is (
                RefusalReason.STOPPING if stopping else RefusalReason.NOT_PAUSED_ON_CAPACITY
            )
    assert [steer.text for steer in channel.take_pending_steer()] == expected.steers
    assert [w.withdraws for w in channel.take_pending_withdrawals()] == expected.withdrawals
    assert channel.take_pending_actions() == expected.actions
    assert channel.pause_requested() == expected.paused
    assert channel.stop_mode() == expected.stop
    assert channel.consume_fallback_request() == expected.fallback
