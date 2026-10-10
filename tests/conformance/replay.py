"""Load the shared corpus and replay a scenario's steps over any connection.

Every runner that executes scenarios against a live transport (``runners/<id>.json``)
reads its inventory and replays steps through these helpers, so the meaning of a step is
defined once.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from tests.conformance.frame_matching import assert_frame_matches

if TYPE_CHECKING:
    from collections.abc import Mapping

_CORPUS = Path(__file__).parent


class ScenarioConnection(Protocol):
    """One client connection a runner replays scenario steps over."""

    def send(self, frame: Mapping[str, Any]) -> None:
        """Send one protocol message."""
        ...

    def receive(self) -> dict[str, Any]:
        """The next protocol message from the server."""
        ...

    def close(self) -> None:
        """Close the connection; closing twice is fine."""
        ...


def load_scenario(name: str) -> dict[str, Any]:
    """The scenario ``scenarios/<name>.json``."""
    return cast("dict[str, Any]", json.loads((_CORPUS / "scenarios" / f"{name}.json").read_text()))


def runner_group(runner: str, group: str) -> tuple[str, ...]:
    """The scenario ids ``runners/<runner>.json`` executes through one setup path."""
    inventory = cast(
        "dict[str, Any]", json.loads((_CORPUS / "runners" / f"{runner}.json").read_text())
    )
    return tuple(cast("list[str]", inventory["groups"][group]))


def run_steps(connection: ScenarioConnection, scenario: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Send each ``c2s`` frame and match each ``s2c`` expectation; return what arrived."""
    received: list[dict[str, Any]] = []
    for step in scenario["steps"]:
        if step["dir"] == "c2s":
            connection.send(step["frame"])
        else:
            message = connection.receive()
            assert_frame_matches(message, step["expect"])
            received.append(message)
    return received
