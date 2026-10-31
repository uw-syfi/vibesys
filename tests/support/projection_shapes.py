"""What each scenario's projection contains, as a few lines of text a reviewer reads in a diff.

The replays the clients read are generated, not committed, so a projector change no longer shows as
a changed file. A shape is the part of a projection a person can review: how many records of each
kind, how many spans of each label kind, and how many blobs and streams. Counts do not move with
the schema hashes in the first record, so two pull requests conflict on a shape only when both
really change what the same scenario projects to.

``python -m tests.support.projection_shapes`` rewrites the snapshots under
``tests/vibesys/run/projection_shapes``; ``./scripts/regenerate_goldens.sh`` runs it.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

from tests.support import journey_fixtures, shared_scenarios

from vs_proto.api import Record

if TYPE_CHECKING:
    from vs_proto.api import ClientReplay

SNAPSHOTS = Path(__file__).resolve().parents[1] / "vibesys/run/projection_shapes"
_UNLABELLED = "(no label)"


def names() -> list[str]:
    """Every scenario, as ``shared/<name>`` or ``journeys/<name>``."""
    shared = [f"shared/{path.stem}" for path in shared_scenarios.declarations()]
    journeys = [f"journeys/{path.stem}" for path in sorted(journey_fixtures.ROOT.glob("*.json"))]
    return [*shared, *journeys]


def project(name: str) -> ClientReplay:
    """The projection of the scenario ``name``, by the shipping projector."""
    group, stem = name.split("/")
    if group == "shared":
        return shared_scenarios.project_shared(shared_scenarios.SOURCES / f"{stem}.json")
    return journey_fixtures.project_scenario(stem)


def shape(name: str, replay: ClientReplay) -> str:
    """The shape of ``replay``: counts by record kind and by span label kind."""
    kinds: Counter[str] = Counter()
    labels: Counter[str] = Counter()
    for batch in replay.batches:
        for wire in batch.records:
            body = Record.from_binary(wire).body
            kinds[body.field if body is not None else "(empty)"] += 1
            if body is not None and body.field == "span":
                span = body.value.span
                label = span.label if span is not None and span.has_field("label") else None
                labels[label.kind if label is not None else _UNLABELLED] += 1
    lines = [name, f"records {sum(kinds.values())}", f"blobs {len(replay.blobs)}"]
    lines.append(f"streams {len(replay.streams)}")
    lines += ["record kinds", *(f"  {kind} {kinds[kind]}" for kind in sorted(kinds))]
    lines += ["span label kinds", *(f"  {kind} {labels[kind]}" for kind in sorted(labels))]
    return "\n".join(lines) + "\n"


def snapshot_path(name: str) -> Path:
    """Where the shape of ``name`` is committed."""
    return SNAPSHOTS / f"{name.replace('/', '.')}.txt"


def main() -> int:
    """Rewrite every snapshot, and remove one whose scenario is gone."""
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    wanted = {snapshot_path(name) for name in names()}
    for path in SNAPSHOTS.glob("*.txt"):
        if path not in wanted:
            path.unlink()
    for name in names():
        snapshot_path(name).write_text(shape(name, project(name)))
        sys.stdout.write(f"wrote {snapshot_path(name).name}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
