"""The serial searches label their spans in the vocabulary every hypothesis search shares.

A run's Overview and Work tabs read only labelled spans: workstreams, agent turns and gates. Each
round of a serial search is a workstream named for its hypothesis and its round, linked to the
workstream that kept the revision it builds on and accepted or rejected by its settlement; every
provider turn is an agent turn naming its role; and each trusted measurement is a gate under its
round, with the value it read, the ratio to the input's and its verdict. A round whose candidate
the gates did not measure (they run on a cadence) has as its gate what its implementer measured of
it through the evaluation tool. The stories pin one run of each kind; the properties play
generated schedules, live and with a restart after every step, and hold the labels to the same
rules after every step.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, note, settings
from hypothesis import strategies as st
from tests.vibesys.agent_spawns import spawns
from tests.vibesys.orchestration.multi import _host as multi_host
from tests.vibesys.orchestration.single import _host as single_host
from tests.vibesys.search_faults import Step, Stop, play, steps, stops
from tests.vibesys.serial_search._host import CANDIDATE_THROUGHPUT, FACTS, INPUT_THROUGHPUT
from tests.vibesys.serial_search._replies import plan_reply
from tests.vibesys.vibesys_flows import across_restarts

from vs_core.api import (
    EvidenceId,
    EvidenceKind,
    EvidenceRef,
    ObservationStatus,
    RequestId,
    Retained,
    RevisionRef,
    Scope,
    Span,
    SpanType,
)
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from tests.vibesys.serial_search._host import SerialHost

SEARCHES = pytest.mark.parametrize("search", ["single", "multi"])
PLANNER = "orchestrator"
IMPLEMENTER = "implementer"
JUDGE = "judge"


def begin(
    search: str, *, rounds: int, restarting: bool, official_every: int = 1, **world: object
) -> SerialHost:
    if search == "single":
        return single_host.SingleHost.start(
            single_host.config(max_rounds=rounds, official_eval_every=official_every),
            restarting=restarting,
            **world,  # ty: ignore[invalid-argument-type]
        )
    return multi_host.MultiHost.start(
        multi_host.config(max_rounds=rounds, official_eval_every=official_every),
        restarting=restarting,
        **world,  # ty: ignore[invalid-argument-type]
    )


def design(host: SerialHost, hypothesis: str) -> None:
    host.drain_until_turn(PLANNER)
    host.run.reply_raw(host.run.expect_turn(role=PLANNER), plan_reply(hypothesis))


def measure_itself(host: SerialHost, evaluations: list[tuple[str, bool]]) -> None:
    """The implementer evaluates each ``(candidate, with the accuracy stage)`` itself.

    Core records each evaluation as one trusted ``local-validation`` record per stage, of the
    open attempt, after the evaluations taken before.
    """
    attempt = host.opened[-1]
    scope = Scope(owner=attempt.attempt_id, generation=attempt.generation)
    taken = list(host.run.view.current().measurements)
    for candidate, accuracy in evaluations:
        request = RequestId(root=f"agent-evaluation-{len(taken)}")
        for stage in ("accuracy", "benchmark") if accuracy else ("benchmark",):
            taken.append(
                EvidenceRef(
                    evidence_id=EvidenceId(root=f"{stage}:{len(taken)}"),
                    kind=EvidenceKind.LOCAL_VALIDATION,
                    purpose="local-validation",
                    scope=scope,
                    source_request=request,
                    candidate=RevisionRef.of_git_commit(candidate),
                    observation_sequence=len(taken),
                    evaluator_digest="evaluator",
                    workload_digest="workload",
                    environment_digest="environment",
                    provenance="trusted",
                    status=ObservationStatus.SUCCEEDED,
                )
            )
    host.run.view.changes["measurements"] = tuple(taken)


def implement(
    host: SerialHost,
    search: str,
    candidate: str,
    *,
    evaluations: list[tuple[str, bool]] | None = None,
) -> None:
    """The implementer retains ``candidate``; in multi the judge then passes it.

    ``evaluations`` are what the implementer evaluated itself first (`measure_itself`).
    """
    host.drain_until_turn(IMPLEMENTER)
    measure_itself(host, evaluations or [])
    asked = host.run.expect_turn(role=IMPLEMENTER)
    checkpoint = Retained(revision=RevisionRef.of_git_commit(candidate))
    if search == "single":
        host.run.reply(asked, single_host.implemented(passed=True), checkpoint=checkpoint)
        return
    host.run.reply(asked, multi_host.implemented(), checkpoint=checkpoint)
    host.drain_until_turn(JUDGE)
    host.run.reply(host.run.expect_turn(role=JUDGE), multi_host.reviewed(passed=True))


def spans(host: SerialHost) -> list[Span]:
    return list(host.run.trace().spans)


def labelled(host: SerialHost, kind: str) -> list[Span]:
    return [s for s in spans(host) if s.label is not None and s.label.kind == kind]


def attrs(span: Span) -> dict[str, object]:
    assert span.label is not None
    assert span.label.attrs_json is not None, f"{span.id} has no attributes"
    return json.loads(span.label.attrs_json)


def roles_under(host: SerialHost, owner: Span) -> list[str]:
    return [
        str(attrs(s)["role"])
        for s in labelled(host, "agent_turn")
        if s.parent == owner.id  # a round's turns are issued by the round itself
    ]


@SEARCHES
@across_restarts
def test_a_round_that_adopts_a_candidate_is_an_accepted_workstream_with_its_turns_and_gate(
    *, search: str, restarting: bool
) -> None:
    host = begin(search, rounds=1, restarting=restarting)
    design(host, "h1")
    implement(host, search, "c1")
    host.drain()

    (workstream,) = labelled(host, "workstream")
    assert workstream.label is not None
    assert workstream.label.title == "title h1"
    assert workstream.label.verdict == "accepted"
    assert attrs(workstream) == {
        "hypothesis_id": "h1",
        "parent_revision": FACTS.baseline.revision_id.root,
        "round": 1,
        "idea": "title h1",
        "revision": "c1",
        "probe": False,
    }
    expected_roles = [PLANNER, IMPLEMENTER] if search == "single" else [PLANNER, IMPLEMENTER, JUDGE]
    assert roles_under(host, workstream) == expected_roles
    # the orchestrator's plan starts the implementer, whose candidate starts the judge
    started = {PLANNER: {()}, IMPLEMENTER: {(PLANNER,)}}
    if search != "single":
        started[JUDGE] = {(IMPLEMENTER,)}
    assert spawns(host.run.trace().spans) == started
    (gate,) = labelled(host, "gate")
    assert gate.parent == workstream.id
    assert gate.label is not None
    assert gate.label.verdict == "passed"
    assert attrs(gate) == {
        "stage": "accuracy,benchmark",
        "metric_value": CANDIDATE_THROUGHPUT,
        "vs_baseline": CANDIDATE_THROUGHPUT / INPUT_THROUGHPUT,
        "failed": None,
    }


@SEARCHES
@across_restarts
def test_a_round_builds_on_the_workstream_that_kept_its_base_and_a_slow_one_is_slower(
    *, search: str, restarting: bool
) -> None:
    host = begin(search, rounds=2, restarting=restarting, throughputs={"c2": 10.0})
    design(host, "h1")
    implement(host, search, "c1")
    design(host, "h2")
    implement(host, search, "c2")
    host.drain()

    first, second = labelled(host, "workstream")
    assert second.label is not None
    assert [(link.rel, link.to) for link in second.label.links] == [("builds_on", first.id)]
    assert attrs(second)["parent_revision"] == "c1"
    assert attrs(second)["round"] == 2
    verdicts = [g.label.verdict for g in labelled(host, "gate") if g.label is not None]
    assert verdicts == ["passed", "slower"]


def gates_under(host: SerialHost, owner: Span) -> list[Span]:
    return [s for s in labelled(host, "gate") if s.parent == owner.id]


@SEARCHES
@across_restarts
def test_regression_a_round_the_gates_skip_has_what_its_implementer_measured_as_its_gate(
    *, search: str, restarting: bool
) -> None:
    # a live multi run showed "nothing measured yet" through three accepted rounds: the gates
    # run on a cadence, and the rounds between kept code only the implementer had measured
    host = begin(
        search, rounds=2, restarting=restarting, official_every=3, throughputs={"c1": 80.0}
    )
    design(host, "h1")
    implement(host, search, "c1", evaluations=[("c0", True), ("c1", True)])
    design(host, "h2")
    implement(host, search, "c2")  # the final round: the trusted gates measure it
    host.drain()

    assert host.measured == ["c2"]
    first, second = labelled(host, "workstream")
    assert [attrs(w)["revision"] for w in (first, second)] == ["c1", "c2"]
    (taken,) = gates_under(host, first)
    assert taken.label is not None
    assert taken.label.verdict == "passed"
    assert attrs(taken) == {
        "stage": "accuracy,benchmark",
        "metric_value": 80.0,
        "vs_baseline": 80.0 / INPUT_THROUGHPUT,
        "failed": None,
    }
    (official,) = gates_under(host, second)
    assert attrs(official)["metric_value"] == CANDIDATE_THROUGHPUT


@SEARCHES
@across_restarts
@settings(max_examples=examples(pr=8, nightly=40), deadline=None, derandomize=True, database=None)
@given(
    rounds=st.lists(
        st.tuples(
            # what the implementer evaluated itself: (the candidate it keeps?, with accuracy?)
            st.lists(st.tuples(st.booleans(), st.booleans()), max_size=3),
            st.sampled_from([10.0, 50.0, 80.0]),  # the kept candidate's throughput
        ),
        min_size=1,
        max_size=3,
    ),
    official_every=st.integers(1, 4),
)
def test_every_measured_round_has_a_gate_with_what_its_latest_measurement_read(
    *,
    search: str,
    restarting: bool,
    rounds: list[tuple[list[tuple[bool, bool]], float]],
    official_every: int,
) -> None:
    throughputs = {f"c{n}": value for n, (_, value) in enumerate(rounds, start=1)}
    host = begin(
        search,
        rounds=len(rounds),
        restarting=restarting,
        official_every=official_every,
        throughputs=throughputs,
    )
    for n, (evaluations, _) in enumerate(rounds, start=1):
        design(host, f"h{n}")
        measured = [(f"c{n}" if kept else "other", accuracy) for kept, accuracy in evaluations]
        implement(host, search, f"c{n}", evaluations=measured)
    host.drain()

    note(f"officially measured: {host.measured}")
    for n, workstream in enumerate(labelled(host, "workstream"), start=1):
        candidate, value = f"c{n}", throughputs[f"c{n}"]
        own = [accuracy for kept, accuracy in rounds[n - 1][0] if kept]
        gates = gates_under(host, workstream)
        if candidate not in host.measured and not own:
            assert gates == [], f"round {n} measured nothing, yet has a gate"
            continue
        assert len(gates) == 1, f"round {n} has {len(gates)} gates"
        (gate,) = gates
        assert gate.label is not None
        assert attrs(gate)["metric_value"] == value
        assert attrs(gate)["vs_baseline"] == value / INPUT_THROUGHPUT
        checked = candidate in host.measured or own[-1]
        beats = value > INPUT_THROUGHPUT
        assert gate.label.verdict == (None if not checked else "passed" if beats else "slower")


# who starts whom: the orchestrator that designs a round's hypothesis starts its implementer (a
# round that continues an earlier round's hypothesis names none); the implementer starts the judge
# that reviews it, and the router the profiler it asks for; the search starts the rest
_STARTERS: dict[str, set[tuple[str, ...]]] = {
    "orchestrator": {()},
    "router": {()},
    "implementer": {("orchestrator",), ()},
    "judge": {("implementer",)},
    "profiler": {("router",)},
}


def _check(host: SerialHost) -> None:
    """What the labels promise after every step."""
    trace = host.run.trace()
    for role, starters in spawns(trace.spans).items():
        assert starters <= _STARTERS[role], f"a {role} was started by {starters}"
    by_id = {s.id: s for s in trace.spans}
    rounds = [s for s in trace.spans if s.name == "play_round"]
    workstreams: list[str] = []
    for span in rounds:
        issued = any(s.parent == span.id for s in trace.spans)
        if span.label is None:
            # only a round the ledger finished before it began does no work and stays unlabelled
            assert not issued, f"{span.id} worked without a label"
            continue
        assert span.label.kind == "workstream"
        assert span.label.title, f"{span.id} has no title"
        found = attrs(span)
        assert isinstance(found["round"], int)
        # a round names its hypothesis once it has one; one the designer gave none is rejected
        chosen = found.get("hypothesis_id") is not None
        assert chosen == (found.get("parent_revision") is not None)
        if span.label.verdict == "accepted":
            assert chosen, f"{span.id} was accepted without a hypothesis"
        if not chosen and span.ended_at is not None and span.label.verdict is not None:
            assert span.label.reason == "no_design"
        # an accepted round names the revision it kept: what the next round builds on
        kept = found.get("revision")
        assert (kept is not None) == (span.label.verdict == "accepted"), f"{span.id} kept {kept}"
        assert found.get("probe") is False
        links = [link.to for link in span.label.links]
        assert len(links) <= 1
        assert set(links) <= set(workstreams), f"{span.id} builds on a later span: {links}"
        for link in links:
            # one path: a round links to the round that kept the revision it builds on
            assert attrs(by_id[link])["revision"] == found["parent_revision"], span.id
        workstreams.append(span.id)
    turns = [s for s in trace.spans if s.invocation is not None]
    assert all(s.label is not None and s.label.kind == "agent_turn" for s in turns)
    assert all(attrs(s)["role"] for s in turns)
    for gate in labelled(host, "gate"):
        assert gate.type is SpanType.OPERATION
        assert attrs(gate)["stage"]
        assert gate.parent in workstreams, f"{gate.id} is not under a workstream"
        assert gate.label is not None
        if gate.label.verdict in {"passed", "slower"}:
            assert attrs(gate)["metric_value"] is not None, f"{gate.id} measured nothing"
        if gate.label.verdict == "passed":
            owner = by_id[str(gate.parent)]
            assert owner.label is not None
            assert owner.label.verdict != "rejected", f"{gate.id} passed in a rejected round"


@SEARCHES
@across_restarts
@settings(max_examples=examples(pr=6, nightly=40), deadline=None, derandomize=True, database=None)
@given(story=steps(), stop=stops(), world=st.tuples(st.integers(1, 3), st.booleans()))
def test_the_serial_searches_label_their_spans_whatever_core_answers(
    *,
    search: str,
    restarting: bool,
    story: list[Step],
    stop: Stop | None,
    world: tuple[int, bool],
) -> None:
    rounds, slow = world
    # every candidate slower than the input, so gates reach "slower" too
    throughputs = {f"c{n}": 10.0 for n in range(1, 40)} if slow else {}
    host = begin(search, rounds=rounds, restarting=restarting, throughputs=throughputs)
    try:
        play(host, story, stop, check=lambda: _check(host))
        _check(host)
    finally:
        note(host.run.timeline())
