"""The actual protoc plugin's generated maps and timestamps retain their value semantics."""

from __future__ import annotations

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from pydantic import ValidationError

from scripts.corevalue.tests.api import FrozenMap, MapValue, TimestampValue, decode, encode
from vs_core.api import (
    ENVELOPE_SCHEMA_VERSION,
    EventCursor,
    HostFence,
    HostId,
    OperationRegistry,
    RunEnvelope,
    StrategyState,
    initial_state,
)


class GeneratedMapState(StrategyState):
    value: MapValue


@given(counts=st.dictionaries(st.text(max_size=10), st.integers(min_value=-100, max_value=100)))
def test_generated_maps_work_inside_the_existing_persisted_envelope(counts: dict[str, int]) -> None:
    state = initial_state()
    envelope = RunEnvelope[GeneratedMapState](
        schema_version=ENVELOPE_SCHEMA_VERSION,
        fence=HostFence(host_id=HostId(root="host"), epoch=1),
        strategy_id=state.run.declaration.strategy_id,
        state_schema=state.run.declaration.state_schema,
        core=state,
        strategy=GeneratedMapState(
            schema_version=state.run.declaration.state_schema.version,
            value=MapValue(counts=FrozenMap(counts)),
        ),
        event_cursor=EventCursor(sequence=0),
    )
    registry = OperationRegistry()
    encoded = registry.encode_envelope(envelope)
    assert registry.decode_envelope(RunEnvelope[GeneratedMapState], encoded) == envelope
    assert registry.reload_envelope(RunEnvelope[GeneratedMapState], envelope) == envelope


@given(
    counts=st.dictionaries(
        st.text(max_size=20), st.integers(min_value=-(2**63), max_value=2**63 - 1)
    ),
    labels=st.lists(st.text(max_size=20), max_size=8).map(tuple),
    seconds=st.integers(min_value=-62135596800, max_value=253402300799),
    nanos=st.integers(min_value=0, max_value=999999999),
)
@example(counts={"z": 2, "a": 1}, labels=("x",), seconds=-1, nanos=999999999)
def test_generated_map_and_well_known_type_roundtrip_canonically(
    counts: dict[str, int], labels: tuple[str, ...], seconds: int, nanos: int
) -> None:
    at = TimestampValue(seconds=seconds, nanos=nanos)
    value = MapValue(
        counts=FrozenMap(counts),
        labels=labels,
        at=at,
        history=(at,),
        marked=FrozenMap(dict.fromkeys(counts, at)),
    )
    reversed_value = MapValue(
        counts=FrozenMap(dict(reversed(tuple(counts.items())))),
        labels=labels,
        at=at,
        history=(at,),
        marked=FrozenMap(dict.fromkeys(reversed(tuple(counts)), at)),
    )
    assert value == reversed_value
    assert hash(value) == hash(reversed_value)
    assert encode(value).to_binary() == encode(reversed_value).to_binary()
    assert decode(encode(value)) == value
    message = encode(value)
    assert decode(type(message).from_json(message.to_json())) == value
    assert MapValue.model_validate_json(value.model_dump_json()) == value


@given(nanos=st.one_of(st.integers(max_value=-1), st.integers(min_value=1000000000)))
def test_timestamp_rejects_out_of_range_nanoseconds_at_construction(nanos: int) -> None:
    with pytest.raises(ValidationError, match="nanos"):
        TimestampValue(seconds=0, nanos=nanos)


@given(seconds=st.one_of(st.integers(max_value=-62135596801), st.integers(min_value=253402300800)))
def test_timestamp_rejects_out_of_range_seconds_at_construction(seconds: int) -> None:
    with pytest.raises(ValidationError, match="seconds"):
        TimestampValue(seconds=seconds, nanos=0)
