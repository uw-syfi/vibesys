"""The journey fixtures' event adapter: which frontend events are core journal facts, and as what."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from vibesys.events import CoreEvent, CoreEventType
from vibesys.records.api import MappedEvent, MappingState, map_event
from vs_proto.api import ClientReplay, ClientReplayBatch, EpochStarted, Record
from vs_runlog.api import fold_skeleton, project_head

CORE_TYPES = frozenset(kind.value for kind in CoreEventType)

# Wire events the server announces or answers with. They were never core journal facts, so a run
# log has no record of them.
ANNOUNCEMENTS = frozenset(
    {
        "server_started",
        "server_ready",
        "run_status_changed",
        "status_query",
        "control",
        "configuration_failed",
    }
)
# Chat is a session's own log, not the run's: the journeys seed it through the session tail.
SESSION_FACTS = frozenset({"chat", "chat_thread_created", "steer_proposed"})
# The frontend envelope's decorations, which no core event carries.
# Any wall-clock time is past this many seconds since the epoch; a synthetic run counts from zero.
_WALL_SECONDS = 1_000_000_000
DECORATIONS = ("protocol_version", "span_id", "diagnostic", "chat_thread_id")


def core_event(original: dict[str, Any]) -> dict[str, Any] | None:
    """The core journal event a retained frontend event stands for; None when it is not a core fact.

    Server output lines become the subprocess output the run log records as a stream. A type
    that is neither a core event nor listed above is an error naming it, never a silent drop.
    """
    kind = original["type"]
    if kind in ANNOUNCEMENTS or kind in SESSION_FACTS:
        return None
    event = {key: value for key, value in original.items() if key not in DECORATIONS}
    if "invocation_id" in event:
        invocation = event.pop("invocation_id")
        event.setdefault("execution_id", invocation)
    if kind == "output":
        data = event.pop("data")
        event["type"] = "subprocess_output"
        event["data"] = {
            "kind": "subprocess_output",
            "process_id": data["source"],
            "process_kind": data["source"],
            "stream": data["stream"],
            "content": data["content"],
        }
    elif kind not in CORE_TYPES:
        message = f"fixture event type {kind!r} has no core journal form"
        raise ValueError(message)
    span = (event.get("data") or {}).get("span")
    if span and span.get("status") in ("running", "waiting"):
        # Synthetic fixtures reuse finished span values in STARTED events.
        # The current writer publishes their ending only with terminal status.
        span.pop("ended_at", None)
    return event


class FixtureRefusedError(ValueError):
    """A fixture holds an event the native projection cannot map; names the event and why."""


def _project(state: MappingState, event: dict[str, Any], original: dict[str, Any]) -> MappedEvent:
    try:
        return map_event(state, CoreEvent.model_validate(event))
    except (TypeError, ValueError) as error:
        message = f"event {original.get('sequence')} ({original['type']}) is refused: {error}"
        raise FixtureRefusedError(message) from error


def _units(fixture: dict[str, Any]) -> dict[str, str]:
    """Native fixture headers declare units from the canonical scenario, before any readings."""
    objective = fixture.get("objective")
    units = {} if objective is None else {objective["metric"]: objective["unit"]}
    for event in fixture["events"]:
        span = (event.get("data") or {}).get("span") or {}
        label = span.get("label") or {}
        if label.get("kind") != "gate":
            continue
        attrs = json.loads(label.get("attrs_json") or "{}")
        for row in attrs.get("metrics", []):
            unit = row.get("unit") or ""
            previous = units.setdefault(row["name"], unit)
            if previous != unit:
                message = f"conflicting metric unit for {row['name']}"
                raise ValueError(message)
    return units


def _wall_clock_spans(fixture: dict[str, Any]) -> bool:
    """Whether every span of the fixture is stamped with wall-clock seconds."""
    starts = [
        event["data"]["span"]["started_at"]
        for event in fixture["events"]
        if (event.get("data") or {}).get("span")
    ]
    return bool(starts) and all(start > _WALL_SECONDS for start in starts)


def replay_of(fixture: dict[str, Any]) -> ClientReplay:
    """The records, blobs and streams the shipping projection makes of a fixture's events."""
    replay = ClientReplay()
    units = _units(fixture)
    streams: dict[str, bytearray] = {}
    # A recorded run's spans carry wall-clock times, as a live run's do: its run clock starts at its
    # first event. Synthetic fixtures count seconds from zero and are left as they are.
    wall = _wall_clock_spans(fixture)
    origin = datetime.fromisoformat(fixture["events"][0]["timestamp"]).timestamp()
    state = MappingState(
        str(fixture["runId"]),
        native_intents=True,
        project_root="/work/queue-rs",
        wall_span_clock=wall,
        clock_epochs=((origin, 0),) if wall else (),
    )
    for original in fixture["events"]:
        event = core_event(original)
        if event is None:
            continue
        projected = _project(state, event, original)
        state = projected.state
        for record in projected.records:
            value = None if record.body is None else record.body.value
            if isinstance(value, EpochStarted):
                for series in value.series:
                    series.unit = units.get(series.name, series.unit)
        replay.batches.append(
            ClientReplayBatch(
                source_sequence=original["sequence"],
                records=[record.to_binary() for record in projected.records],
            )
        )
        for blob in projected.blobs:
            replay.blobs[blob.sha256] = blob.content
        for stream in projected.streams:
            content = streams.setdefault(stream.id, bytearray())
            if len(content) != stream.offset:
                message = "fixture stream offset is not contiguous"
                raise ValueError(message)
            content.extend(stream.content)
    replay.streams.update({key: bytes(value) for key, value in streams.items()})
    return project_heads(
        replay, fixture.get("liveAfter") if fixture.get("state") == "live" else None
    )


def project_heads(replay: ClientReplay, from_source: int | None = None) -> ClientReplay:
    """The current replay prefix's head is produced by the shipping writer projection."""
    records: list[Record] = []
    prefix = max(
        (
            index
            for index, batch in enumerate(replay.batches)
            if from_source is not None and batch.source_sequence <= from_source
        ),
        default=-1,
    )
    for index, batch in enumerate(replay.batches):
        records.extend(Record.from_binary(wire) for wire in batch.records)
        if index == len(replay.batches) - 1 or (from_source is not None and index >= prefix):
            batch.head = project_head(fold_skeleton(records, log_offset=0))
    return replay
