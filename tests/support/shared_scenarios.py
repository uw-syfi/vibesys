"""Canonical shared declarations and their native replay goldens; no client span factories."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from tests.support.retained_events import replay_of

if TYPE_CHECKING:
    from vs_proto.api import ClientReplay

REPOSITORY = Path(__file__).resolve().parents[2]
SOURCES = REPOSITORY / "clients/libs/run-fixtures/src/data"
GOLDENS = REPOSITORY / "clients/libs/run-fixtures/src/native"
TRACE = REPOSITORY / "clients/apps/desktop-shell/e2e/scenarios/trace.json"


def declarations() -> list[Path]:
    """The same canonical RunFixture declarations the client catalog loads."""
    return [
        path for path in sorted(SOURCES.glob("*.json")) if "events" in json.loads(path.read_text())
    ]


def project_shared(path: Path) -> ClientReplay:
    """The shipping projector consumes the canonical core events, including streams and blobs."""
    return replay_of(json.loads(path.read_text()))


def fingerprint() -> str:
    """Large golden checks hash every input instead of repeating quadratic projection in CI."""
    paths = declarations()
    paths += sorted((REPOSITORY / "src/vibesys/records").glob("*.py"))
    paths += sorted((REPOSITORY / "libs/vs-runlog/src/vs_runlog").glob("*.py"))
    paths += sorted((REPOSITORY / "proto/vibesys").rglob("*.proto"))
    paths += [Path(__file__), Path(__file__).with_name("retained_events.py")]
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.read_bytes())
    return digest.hexdigest()


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


def regenerate() -> None:
    """Publish native replay artifacts and a manifest that fails on source or byte drift."""
    publish_live_trace()
    GOLDENS.mkdir(exist_ok=True)
    for path in declarations():
        (GOLDENS / f"{path.stem}.pb").write_bytes(project_shared(path).to_binary())
        sys.stdout.write(f"wrote shared {path.stem}.pb\n")
        sys.stdout.flush()
    (GOLDENS / "source.sha256").write_text(fingerprint() + "\n")
    (GOLDENS / "goldens.sha256").write_text(
        "".join(
            f"{path.name} {hashlib.sha256(path.read_bytes()).hexdigest()}\n"
            for path in sorted(GOLDENS.glob("*.pb"))
        )
    )


if __name__ == "__main__":
    regenerate()
