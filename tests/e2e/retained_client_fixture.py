"""Replay retained browser fixtures through the shipping typed-record projection.

The output is existing Record protobufs and content bytes. There is no second
record mapper here: the same pure owner used by RunLogTee interprets the events.
"""

from __future__ import annotations

import fcntl
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.support.retained_events import (
    replay_of,
)


def write_fixture(root: Path, fixture: dict[str, Any]) -> None:
    """Publish one immutable fixture under the caller's content-addressed directory."""
    root.parent.mkdir(parents=True, exist_ok=True)
    with root.with_suffix(".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (root / ".complete").exists():
            return
        replay = replay_of(fixture)
        root.mkdir(parents=True, exist_ok=True)
        (root / "replay.pb").write_bytes(replay.to_binary())
        (root / ".complete").touch()


if __name__ == "__main__":
    for item in json.load(sys.stdin):
        write_fixture(Path(sys.argv[1]) / item["key"], item["fixture"])
