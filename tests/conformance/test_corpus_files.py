"""The corpus files are well formed and cover the protocol's event kinds."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from server.events import EventType

CORPUS = Path(__file__).parent
EVENT_FILES = sorted((CORPUS / "events").glob("*.json"))
SCENARIO_FILES = sorted((CORPUS / "scenarios").glob("*.json"))
WIRE_PROTOCOL = CORPUS.parents[1] / "docs" / "contributing" / "wire-protocol.md"


def test_every_event_kind_has_exactly_one_fixture_and_no_fixture_is_unknown() -> None:
    assert {path.stem for path in EVENT_FILES} == {kind.value for kind in EventType}


@pytest.mark.parametrize("path", EVENT_FILES, ids=lambda path: path.stem)
def test_an_event_fixture_declares_its_own_kind(path: Path) -> None:
    assert json.loads(path.read_text())["type"] == path.stem


@pytest.mark.parametrize("path", SCENARIO_FILES, ids=lambda path: path.stem)
def test_a_scenario_declares_its_own_id(path: Path) -> None:
    assert json.loads(path.read_text())["id"] == path.stem


def test_every_wire_decision_is_exercised_by_a_scenario_and_no_scenario_tags_an_unknown_one() -> (
    None
):
    declared = set(re.findall(r"^###\s+(WP-[A-Z-]+):", WIRE_PROTOCOL.read_text(), re.MULTILINE))
    tagged = {
        decision
        for path in SCENARIO_FILES
        for decision in json.loads(path.read_text()).get("decisions", [])
    }
    assert declared
    assert tagged == declared
