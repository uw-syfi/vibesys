"""The evolve search labels its spans in the vocabulary every search shares.

A run's Overview and Work tabs read only labelled spans. Each child (a cold start, or one slot of
a generation) is a workstream named for its slot, on the round that runs it, linked to the
workstream that kept the revision it builds on and passed or failed by its settlement; every
provider turn is an agent turn naming its role; each trusted measurement is a gate with the value
it read and its verdict; and the adoption of the winner is a span naming the adopted candidate and
what it measured. Each story runs live and with a restart after every step.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from tests.vibesys.agent_spawns import spawns
from tests.vibesys.orchestration.evolve._host import FACTS, Host, begin, config
from tests.vibesys.vibesys_flows import across_restarts

if TYPE_CHECKING:
    from vs_core.api import Span


def labelled(host: Host, kind: str) -> list[Span]:
    return [s for s in host.run.trace().spans if s.label is not None and s.label.kind == kind]


def attrs(span: Span) -> dict[str, object]:
    assert span.label is not None
    assert span.label.attrs_json is not None, f"{span.id} has no attributes"
    return json.loads(span.label.attrs_json)


def roles_under(host: Host, owner: Span) -> list[str]:
    return [str(attrs(s)["role"]) for s in labelled(host, "agent_turn") if s.parent == owner.id]


@across_restarts
def test_a_cold_start_and_the_child_built_on_it_are_workstreams_with_turns_gates_and_adoption(
    *, restarting: bool
) -> None:
    host = begin(config(), restarting=restarting, throughputs={"c1": 100.0, "c2": 150.0})
    host.drain()

    first, second = labelled(host, "workstream")
    assert first.label is not None
    assert (first.label.title, first.label.verdict) == ("cold start 1", "passed")
    assert attrs(first) == {
        "hypothesis_id": "bootstrap.1",
        "parent_revision": FACTS.baseline.revision_id.root,
        "base_round": None,
        "round": 1,
        "idea": "change c1",
        "revision": "c1",
        "outcome": None,
        "summary": None,
        "probe": False,
    }
    assert second.label is not None
    assert (second.label.title, second.label.verdict) == ("generation 1 · child 1", "passed")
    assert attrs(second) == {
        "hypothesis_id": "g1.c1",
        "parent_revision": "c1",
        "base_round": None,
        "round": 2,
        "idea": "change c2",
        "revision": "c2",
        "outcome": None,
        "summary": None,
        "probe": False,
    }
    assert [(link.rel, link.to) for link in second.label.links] == [("builds_on", first.id)]
    assert first.label.links == ()
    assert roles_under(host, first) == roles_under(host, second) == ["mutator", "judge"]
    # the search starts each mutator; a mutator's offspring starts the judge that grades it
    assert spawns(host.run.trace().spans) == {"mutator": {()}, "judge": {("mutator",)}}

    gates = labelled(host, "gate")
    assert [g.parent for g in gates] == [first.id, second.id]
    assert [attrs(g) for g in gates] == [
        {
            "stage": "accuracy,benchmark",
            "metric_value": value,
            "vs_baseline": None,
            "input_metrics": [],
            "metrics": [
                {"name": "throughput", "value": value, "direction": "max", "unit": "tokens/s"}
            ],
            "failed": None,
        }
        for value in (100.0, 150.0)
    ]
    assert [g.label.verdict for g in gates if g.label is not None] == ["passed", "passed"]

    (adoption,) = labelled(host, "adoption")
    assert adoption.label is not None
    assert adoption.label.verdict == "adopted"
    assert attrs(adoption) == {"candidate": "c2", "metric_value": 150.0, "vs_baseline": None}


@across_restarts
def test_a_child_the_judge_rejects_is_a_rejected_workstream_and_adoption_keeps_the_parent(
    *, restarting: bool
) -> None:
    host = begin(config(), restarting=restarting)
    host.drain_until_turn("mutator")
    host.answer(host.run.expect_turn(role="mutator"))
    host.drain_until_turn("judge")
    host.answer(host.run.expect_turn(role="judge"))  # the cold start passes
    host.drain_until_turn("mutator")
    host.answer(host.run.expect_turn(role="mutator"))
    host.drain_until_turn("judge")
    host.answer(host.run.expect_turn(role="judge"), variant=1)  # the child does not
    host.drain()

    cold, child = labelled(host, "workstream")
    assert cold.label is not None
    assert cold.label.verdict == "passed"
    assert child.label is not None
    assert (child.label.verdict, child.label.reason) == ("failed", "not_passed")
    assert attrs(child)["revision"] is None, "a failed child kept nothing"
    assert attrs(child)["idea"], "the mutator said what it tried"
    assert [g.parent for g in labelled(host, "gate")] == [cold.id]
    (adoption,) = labelled(host, "adoption")
    assert attrs(adoption)["candidate"] == "c1"
    assert adoption.label is not None
    assert adoption.label.verdict == "adopted"


@across_restarts
def test_a_cold_start_left_unmeasured_keeps_its_work_in_progress_and_nothing_is_adopted(
    *, restarting: bool
) -> None:
    host = begin(config(bootstrap_max_attempts=1), restarting=restarting, unmeasurable=True)
    host.drain()

    (cold,) = labelled(host, "workstream")
    assert cold.label is not None
    assert cold.label.verdict == "passed", "the next cold start repairs what it left"
    assert labelled(host, "adoption") == []


@across_restarts
def test_a_child_whose_mutator_gave_no_reply_is_rejected_for_it(*, restarting: bool) -> None:
    host = begin(config(bootstrap_max_attempts=1), restarting=restarting)
    host.drain_until_turn("mutator")
    host.answer(host.run.expect_turn(role="mutator"), variant=2)  # the provider fails the turn
    host.drain()

    (cold,) = labelled(host, "workstream")
    assert cold.label is not None
    assert (cold.label.verdict, cold.label.reason) == ("failed", "no-reply")
    assert roles_under(host, cold) == ["mutator"]
    assert labelled(host, "gate") == []


@across_restarts
def test_regression_a_cold_start_whose_accuracy_failed_is_rejected_though_its_work_is_kept(
    *, restarting: bool
) -> None:
    # the next cold start repairs what it left, so its code is kept; but its own trusted check
    # turned it down, and it used to read as a passed workstream
    host = begin(config(bootstrap_max_attempts=1), restarting=restarting, failing={"c1"})
    host.drain()

    (cold,) = labelled(host, "workstream")
    assert cold.label is not None
    assert (cold.label.verdict, cold.label.reason) == ("failed", "turned_down")
    assert attrs(cold)["revision"] == "c1", "it names the code the next cold start repairs"
    (gate,) = labelled(host, "gate")
    assert gate.label is not None
    assert gate.label.verdict == "failed_check"
    assert labelled(host, "adoption") == []
