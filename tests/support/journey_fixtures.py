"""Native desktop journeys: declarative core events interpreted by the shipping projector.

The replays are generated from the declarations by ``tests.support.materialize_replays`` and are
not committed; the tests project the same declarations, including blobs and streams.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.support.lineage_fixture import lineage_fixture
from tests.support.retained_events import project_heads, replay_of

from vs_proto.api import (
    ClientReplay,
    ClientReplayBatch,
    Lifecycle,
    Oneof,
    Record,
    StreamRef,
    validate,
)
from vs_runlog.api import fold_skeleton

ROOT = Path(__file__).resolve().parents[2] / "clients/apps/desktop-shell/e2e/scenarios"


def project_scenario(name: str) -> ClientReplay:
    """Project a named declaration, refusing unknown fields through the real core contracts."""
    declaration = json.loads((ROOT / f"{name}.json").read_text())
    replay = replay_of(lineage_fixture(declaration) if "lineage" in declaration else declaration)
    for item in declaration.get("nativeStreams", []):
        reference = StreamRef.from_json(json.dumps(item["reference"]))
        validate(reference)
        if reference.id in replay.streams:
            message = f"duplicate native stream id: {reference.id}"
            raise ValueError(message)
        content = item["text"].encode()
        if reference.has_field("length") and reference.length != len(content):
            message = f"stream {reference.id}: length differs from its content"
            raise ValueError(message)
        _append(replay, reference)
        replay.streams[reference.id] = content
    for native in declaration.get("nativeLifecycles", []):
        _append(replay, Lifecycle.from_json(json.dumps(native)))
    fold_skeleton(_records(replay), log_offset=0)
    return project_heads(replay)


def _records(replay: ClientReplay) -> list[Record]:
    return [Record.from_binary(wire) for batch in replay.batches for wire in batch.records]


def _append(replay: ClientReplay, value: Lifecycle | StreamRef) -> None:
    previous = _records(replay)[-1]
    row = Record(
        seq=previous.seq + 1,
        epoch=previous.epoch,
        at=previous.at,
        clock=previous.clock,
        body=Oneof("lifecycle", value) if isinstance(value, Lifecycle) else Oneof("stream", value),
    )
    validate(row)
    replay.batches.append(
        ClientReplayBatch(
            source_sequence=replay.batches[-1].source_sequence + 1,
            records=[row.to_binary()],
        )
    )


def scenario_variants(name: str) -> dict[str, ClientReplay]:
    """Cumulative native lifecycle prefixes, all derived from one canonical declaration."""
    declaration = json.loads((ROOT / f"{name}.json").read_text())
    if not declaration.get("nativeTransitions"):
        return {}
    current = project_scenario(name)
    variants = {}
    for transition in declaration.get("nativeTransitions", []):
        _append(current, Lifecycle.from_json(json.dumps(transition["lifecycle"])))
        fold_skeleton(_records(current), log_offset=0)
        current = project_heads(current)
        variants[transition["name"]] = ClientReplay.from_binary(current.to_binary())
    return variants


def live_artifacts() -> dict[str, bytes]:
    """Publish a prefix and one native delta per later event, including incremental stream bytes."""
    fixture = json.loads((ROOT / "live.json").read_text())
    events = fixture["events"]
    through = fixture["liveAfter"]
    previous = replay_of({**fixture, "events": events[:through]})
    output = {"live.initial.pb": previous.to_binary()}
    for index in range(through, len(events)):
        current = replay_of({**fixture, "events": events[: index + 1]})
        delta = ClientReplay(
            batches=current.batches[len(previous.batches) :],
            blobs={key: value for key, value in current.blobs.items() if key not in previous.blobs},
            streams={
                key: value[len(previous.streams.get(key, b"")) :]
                for key, value in current.streams.items()
                if value != previous.streams.get(key, b"")
            },
        )
        output[f"live.{events[index]['sequence']}.pb"] = delta.to_binary()
        previous = current
    return output
