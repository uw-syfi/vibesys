"""The issue-queue search labels its spans in the vocabulary every search shares.

A run's Overview and Work tabs read only labelled spans. Each job is a workstream of the round it
belongs to: an issue job is named for its issue, passed when it closed the issue with a change
and linked to the job that kept the tip it builds on; an evaluation only looks, so it reaches no
verdict. Every provider turn is an agent turn naming its role, the accuracy proof of the final
candidate is a gate, and the adoption of the tip is a span naming the adopted candidate. Each
story runs live and with a restart after every step.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from tests.vibesys.agent_spawns import spawns
from tests.vibesys.orchestration.issue_queue._host import (
    FACTS,
    IMPLEMENTER,
    JUDGE,
    PERF,
    UNGATED,
    Host,
    begin,
    config,
)
from tests.vibesys.orchestration.issue_queue._replies import evaluated, judged
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
def test_each_job_is_a_workstream_and_the_issue_that_builds_on_the_tip_links_to_its_job(
    *, restarting: bool
) -> None:
    host = begin(config(), restarting=restarting, perf=[evaluated("faster")])
    host.drain()

    first, evaluation, second, last = labelled(host, "workstream")
    base = FACTS.baseline.revision_id.root
    assert first.label is not None
    assert first.label.verdict == "passed"
    assert attrs(first) == {
        "hypothesis_id": "issue-1",
        "parent_revision": base,
        "base_round": None,
        "round": 1,
        "idea": "two opaque items",
        "revision": "c1",
        "outcome": None,
        "summary": None,
        "probe": False,
    }
    assert first.label.links == ()
    assert roles_under(host, first) == [IMPLEMENTER, JUDGE]

    assert evaluation.label is not None
    assert evaluation.label.title == "performance evaluation 1"
    assert evaluation.label.verdict is None
    assert attrs(evaluation)["hypothesis_id"] == "evaluation-1"
    assert attrs(evaluation)["probe"] is True, "an evaluation only looks: it is no candidate"
    assert roles_under(host, evaluation) == [PERF]

    assert second.label is not None
    assert (second.label.title, second.label.verdict) == ("#2 faster", "passed")
    assert attrs(second)["round"] == 2
    assert attrs(second)["parent_revision"] == attrs(first)["revision"], (
        "it builds on the revision the first issue's job kept"
    )
    assert [(link.rel, link.to) for link in second.label.links] == [("builds_on", first.id)]
    assert last.label is not None
    assert attrs(last)["hypothesis_id"] == "evaluation-2"
    # the queue starts each job's implementer and each evaluator; the implementer the judge
    started = spawns(host.run.trace().spans)
    assert started == {IMPLEMENTER: {()}, JUDGE: {(IMPLEMENTER,)}, PERF: {()}}


@across_restarts
def test_the_accuracy_proof_is_a_gate_and_the_adopted_tip_is_the_adoption(
    *, restarting: bool
) -> None:
    host = begin(config(), restarting=restarting)
    host.drain()

    (gate,) = labelled(host, "gate")
    assert attrs(gate) == {
        "stage": "accuracy",
        "metric_value": None,
        "vs_baseline": None,
        "input_metrics": [],
        "metrics": [],
        "failed": None,
    }
    (adoption,) = labelled(host, "adoption")
    assert adoption.label is not None
    assert adoption.label.verdict == "adopted"
    assert attrs(adoption) == {"candidate": "c1", "metric_value": None, "vs_baseline": None}
    assert host.run.finished is not None
    assert host.run.finished.result.selection is not None
    assert host.run.finished.result.selection.revision.revision_id.root == "c1"


@across_restarts
def test_an_issue_that_runs_out_of_attempts_is_rejected_and_the_input_is_adopted_as_it_stands(
    *, restarting: bool
) -> None:
    host = begin(
        config(),
        restarting=restarting,
        facts=UNGATED,
        judge=[judged(passed=False), judged(passed=False)],
    )
    host.drain()

    (issue,) = labelled(host, "workstream")
    assert issue.label is not None
    assert (issue.label.verdict, issue.label.reason) == ("failed", "not_closed")
    assert labelled(host, "gate") == []
    assert labelled(host, "adoption") == [], "a failed run with nothing to keep adopts nothing"


@across_restarts
def test_an_issue_closed_without_a_change_is_rejected_for_it(*, restarting: bool) -> None:
    host = begin(config(), restarting=restarting, unchanged=True, facts=UNGATED)
    host.drain()

    issue, *_ = labelled(host, "workstream")
    assert issue.label is not None
    assert (issue.label.verdict, issue.label.reason) == ("failed", "no_change")
    (adoption,) = labelled(host, "adoption")
    assert attrs(adoption)["candidate"] is None, "nothing changed: the input stands"
    assert adoption.label is not None
    assert adoption.label.verdict == "adopted"
