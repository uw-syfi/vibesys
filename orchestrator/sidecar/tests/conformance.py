"""The shared conformance suite for ``SidecarService``.

``conformance/scenarios/*.json`` say what to send and which properties the resulting event log
must have; ``conformance/README.md`` defines each check by name. This module runs a scenario
against any ``Client``, so the Python sidecar and a Go Fake can be held to the same checks (the Go
side reads the same files and implements the same check names).
"""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from google.protobuf import json_format

from vs_sidecar.gen.vibesys.orchestrator.v1 import orchestrator_pb2 as orch
from vs_sidecar.gen.vibesys.orchestrator.v1 import sidecar_pb2 as sc

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

SCENARIOS = Path(__file__).resolve().parent.parent / "conformance" / "scenarios"


class Client(Protocol):
    """What a scenario needs from a sidecar."""

    def send(self, command: sc.SidecarCommand) -> None:
        """Deliver a command."""
        ...

    def follow(
        self, after_offset: int, until: Callable[[Sequence[sc.SidecarEvent]], bool]
    ) -> list[sc.SidecarEvent]:
        """The events after an offset, read until *until* accepts what has been read (or the
        stream ends), within a bound that only guards against a hang."""
        ...


@dataclass(frozen=True)
class Scenario:
    """One scenario file."""

    name: str
    provider: Sequence[Sequence[Mapping[str, Any]]]
    steps: Sequence[Mapping[str, Any]]
    checks: Sequence[str]
    expect: Mapping[str, Any]


def load(name: str) -> Scenario:
    """The scenario called *name*."""
    raw = json.loads((SCENARIOS / f"{name}.json").read_text())
    return Scenario(
        raw["name"], raw["provider"], raw["steps"], raw["checks"], raw.get("expect", {})
    )


def names() -> list[str]:
    """Every scenario name, sorted."""
    return sorted(path.stem for path in SCENARIOS.glob("*.json"))


def command(raw: Mapping[str, Any]) -> sc.SidecarCommand:
    """A ``SidecarCommand`` from its protobuf JSON."""
    return json_format.ParseDict(dict(raw), sc.SidecarCommand())


def _reached(condition: Mapping[str, Any]) -> Callable[[Sequence[sc.SidecarEvent]], bool]:
    def done(events: Sequence[sc.SidecarEvent]) -> bool:
        for event in events:
            if "turn_ended" in condition and event.turn_ended.turn_id == condition["turn_ended"]:
                return True
            if (
                "steer" in condition
                and event.HasField("steer")
                and event.steer.message_id == condition["steer"]
            ):
                return True
            if "closed" in condition and event.HasField("closed"):
                return True
        return False

    return done


def run(scenario: Scenario, client: Client) -> list[sc.SidecarEvent]:
    """Play *scenario*'s steps and return the whole event log."""
    for step in scenario.steps:
        if "send" in step:
            client.send(command(step["send"]))
        else:
            client.follow(0, _reached(step["await"]))
    return client.follow(0, lambda events: _settled(events, scenario))


def _settled(events: Sequence[sc.SidecarEvent], scenario: Scenario) -> bool:
    waits = [step["await"] for step in scenario.steps if "await" in step]
    return bool(waits) and _reached(waits[-1])(events)


# -- checks: each takes the log, the scenario and the client, and raises AssertionError


def check(name: str, events: Sequence[sc.SidecarEvent], scenario: Scenario, client: Client) -> None:
    """Run the check called *name*."""
    CHECKS[name](events, scenario, client)


def _turn_ids(scenario: Scenario) -> set[str]:
    return {
        step["send"]["startTurn"]["turnId"]
        for step in scenario.steps
        if "send" in step and "startTurn" in step["send"]
    }


def _opened_first(events: Sequence[sc.SidecarEvent], *_: object) -> None:
    assert events[0].HasField("opened"), "the first event must be ConversationOpened"
    assert sum(e.HasField("opened") for e in events) == 1


def _offsets_increase(events: Sequence[sc.SidecarEvent], *_: object) -> None:
    offsets = [e.offset for e in events]
    assert offsets[0] >= 1
    assert all(a < b for a, b in itertools.pairwise(offsets)), offsets


def _one_end_per_turn(events: Sequence[sc.SidecarEvent], scenario: Scenario, *_: object) -> None:
    ends = [e.turn_ended.turn_id for e in events if e.HasField("turn_ended")]
    for turn in _turn_ids(scenario):
        assert ends.count(turn) == 1, f"turn {turn} ended {ends.count(turn)} times"


def _items_precede_end(events: Sequence[sc.SidecarEvent], *_: object) -> None:
    ended: set[str] = set()
    for event in events:
        if event.HasField("turn_ended"):
            ended.add(event.turn_ended.turn_id)
        elif event.HasField("item"):
            assert event.item.turn_id not in ended, f"item {event.item.id} after its turn ended"


def _no_running_item_after_end(events: Sequence[sc.SidecarEvent], *_: object) -> None:
    status: dict[str, int] = {}
    for event in events:
        if event.HasField("item"):
            status[event.item.id] = event.item.status
    assert orch.ITEM_STATUS_RUNNING not in status.values(), status


def _usage_ends_the_turn(events: Sequence[sc.SidecarEvent], *_: object) -> None:
    last: dict[str, sc.Usage] = {}
    for event in events:
        if event.HasField("usage"):
            last[event.usage.turn_id] = event.usage.usage
        elif event.HasField("turn_ended") and event.turn_ended.turn_id in last:
            assert event.turn_ended.usage == last[event.turn_ended.turn_id]


def _effect_repeat_dropped(
    events: Sequence[sc.SidecarEvent], scenario: Scenario, *_: object
) -> None:
    sends = [step["send"]["effectId"] for step in scenario.steps if "send" in step]
    assert len(sends) > len(set(sends)), "the scenario must repeat an effect"
    _one_end_per_turn(events, scenario)


def _at_least(count: int) -> Callable[[Sequence[sc.SidecarEvent]], bool]:
    return lambda seen: len(seen) >= count


def _follow_resumes(events: Sequence[sc.SidecarEvent], _scenario: Scenario, client: Client) -> None:
    last = events[-1].offset
    for after in sorted({0, events[len(events) // 2].offset, last}):
        wanted = [e for e in events if e.offset > after]
        if not wanted:
            continue  # an empty answer cannot be told from a slow one without waiting
        got = client.follow(after, _at_least(len(wanted)))
        assert got == wanted, f"follow after {after}"


def _late_steer_refused(events: Sequence[sc.SidecarEvent], scenario: Scenario, *_: object) -> None:
    for message_id, accepted in scenario.expect["steer"].items():
        at = next(
            i
            for i, e in enumerate(events)
            if e.HasField("steer") and e.steer.message_id == message_id
        )
        assert events[at].steer.accepted is accepted
        assert any(e.HasField("turn_ended") for e in events[:at]), (
            "the answer must follow a TurnEnded"
        )


def _close_is_last(events: Sequence[sc.SidecarEvent], *_: object) -> None:
    assert events[-1].HasField("closed")
    assert sum(e.HasField("closed") for e in events) == 1


CHECKS: dict[str, Callable[..., None]] = {
    "opened_first": _opened_first,
    "offsets_increase": _offsets_increase,
    "one_end_per_turn": _one_end_per_turn,
    "items_precede_end": _items_precede_end,
    "no_running_item_after_end": _no_running_item_after_end,
    "usage_ends_the_turn": _usage_ends_the_turn,
    "effect_repeat_dropped": _effect_repeat_dropped,
    "follow_resumes": _follow_resumes,
    "late_steer_refused": _late_steer_refused,
    "close_is_last": _close_is_last,
}
