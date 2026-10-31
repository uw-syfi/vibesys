"""Canonical shared declarations and their projection; no client span factories.

The native replays are generated from the declarations by ``tests.support.materialize_replays``
and are not committed. ``python -m tests.support.shared_scenarios`` refreshes the one committed
copy, ``queue-rs-live.json``, from the desktop journey trace it is a rename of.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from tests.support.retained_events import replay_of

if TYPE_CHECKING:
    from vs_proto.api import ClientReplay

REPOSITORY = Path(__file__).resolve().parents[2]
SOURCES = REPOSITORY / "clients/libs/run-fixtures/src/data"
TRACE = REPOSITORY / "clients/apps/desktop-shell/e2e/scenarios/trace.json"


def declarations() -> list[Path]:
    """The same canonical RunFixture declarations the client catalog loads."""
    return [
        path for path in sorted(SOURCES.glob("*.json")) if "events" in json.loads(path.read_text())
    ]


def project_shared(path: Path) -> ClientReplay:
    """The shipping projector consumes the canonical core events, including streams and blobs."""
    return replay_of(json.loads(path.read_text()))


def publish_live_trace() -> None:
    """The ``queue-rs-live`` declaration is the desktop journey trace under a new name."""
    trace = json.loads(TRACE.read_text())
    trace.update(
        name="queue-rs-live",
        description="A dynamic search over two planning calls, seven ideas, W5 with a turn and a judge.",
    )
    (SOURCES / "queue-rs-live.json").write_text(
        json.dumps(trace, indent=2, ensure_ascii=False) + "\n"
    )


if __name__ == "__main__":
    publish_live_trace()
