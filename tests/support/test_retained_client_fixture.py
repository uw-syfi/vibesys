"""Every event a retained journey fixture can hold is converted, set aside by name, or refused by name."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.support.retained_events import (
    ANNOUNCEMENTS,
    CORE_TYPES,
    SESSION_FACTS,
    FixtureRefusedError,
    core_event,
    replay_of,
)

from server.events import EventType
from vibesys.events import CoreEvent
from vibesys.records.api import MappingState, map_event

ROOT = Path(__file__).resolve().parents[2]
# Wire events with no core journal form: a fixture holding one must fail loudly, naming it.
UNCONVERTIBLE = frozenset({"steer_delivery", "run_interrupted"})
OUTPUT = "output"


def sample(kind: str) -> dict[str, object]:
    """A minimal frontend event of that type; its data is only needed for output."""
    event: dict[str, object] = {
        "protocol_version": 1,
        "sequence": 1,
        "run_id": "20261010-120000-11111111-dynamic",
        "timestamp": "2026-10-15T16:30:51Z",
        "type": kind,
        "span_id": "s-1",
        "diagnostic": None,
        "chat_thread_id": None,
    }
    if kind == OUTPUT:
        event["data"] = {"kind": "output", "stream": "stdout", "source": "backend", "content": "x"}
    return event


def test_every_wire_event_type_has_exactly_one_fate() -> None:
    """A type added to the server wire is classified here, or this fails: nothing is dropped silently."""
    for kind in sorted(member.value for member in EventType):
        if kind in ANNOUNCEMENTS or kind in SESSION_FACTS:
            assert core_event(sample(kind)) is None, kind
        elif kind in UNCONVERTIBLE:
            with pytest.raises(ValueError, match=kind):
                core_event(sample(kind))
        elif kind == OUTPUT:
            converted = core_event(sample(kind))
            assert converted is not None
            assert converted["type"] == "subprocess_output"
            CoreEvent.model_validate(converted)
        else:
            assert kind in CORE_TYPES, f"{kind} is not classified"


@settings(deadline=None, max_examples=15)
@given(
    lines=st.lists(
        st.tuples(
            st.sampled_from(["stdout", "stderr"]),
            st.sampled_from(["backend", "evaluator"]),
            st.text(alphabet=st.characters(blacklist_categories=("Cs",)), min_size=1, max_size=20),
        ),
        min_size=1,
        max_size=12,
    )
)
def test_server_output_lines_keep_their_bytes_and_order_per_stream(
    lines: list[tuple[str, str, str]],
) -> None:
    """Whatever the interleaving, each stream's content is its lines concatenated in order."""
    state = MappingState("20261010-120000-11111111-dynamic", native_intents=True, project_root="/w")
    streams: dict[str, bytearray] = {}
    for index, (stream, source, content) in enumerate(lines):
        original = sample(OUTPUT)
        original["sequence"] = index + 1
        original["data"] = {
            "kind": "output",
            "stream": stream,
            "source": source,
            "content": content,
        }
        event = core_event(original)
        assert event is not None
        projected = map_event(state, CoreEvent.model_validate(event))
        state = projected.state
        for emitted in projected.streams:
            buffer = streams.setdefault(emitted.id, bytearray())
            assert len(buffer) == emitted.offset
            buffer.extend(emitted.content)
    expected: dict[tuple[str, str], bytes] = {}
    for stream, source, content in lines:
        expected[(source, stream)] = expected.get((source, stream), b"") + content.encode()
    assert sorted(bytes(value) for value in streams.values()) == sorted(expected.values())


FIXTURES = sorted((ROOT / "clients/libs/run-fixtures/src/data").glob("*.json"))


@pytest.mark.parametrize("path", FIXTURES, ids=lambda path: path.stem)
def test_every_committed_run_fixture_converts(path: Path) -> None:
    """The journeys' own run data reaches the native path whole, in every state a fixture takes."""
    fixture = json.loads(path.read_text())
    if "runId" not in fixture:
        pytest.skip("not a run: a catalogue of examples")
    assert replay_of(fixture).batches


CONFORMANCE = ROOT / "tests/conformance/fixtures/run-summary"


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("two-ideas", "agent: Field required"),
        ("predates-trace", "activity"),
    ],
)
def test_conformance_runs_that_predate_the_native_shapes_are_refused_by_name(
    name: str, reason: str
) -> None:
    """These inputs belong to the retained server summary; the native path refuses them typed."""
    fixture = json.loads((CONFORMANCE / f"{name}.json").read_text())
    with pytest.raises(FixtureRefusedError, match=reason):
        replay_of({"runId": "20261010-120000-11111111-dynamic", "events": fixture["events"]})
