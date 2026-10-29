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
    search: str,
    *,
    rounds: int,
    restarting: bool,
    **world: object,
) -> SerialHost:
    if search == "single":
        return single_host.SingleHost.start(
            single_host.config(max_rounds=rounds),
            restarting=restarting,
            **world,  # ty: ignore[invalid-argument-type]
        )
    return multi_host.MultiHost.start(
        multi_host.config(max_rounds=rounds),
        restarting=restarting,
        **world,  # ty: ignore[invalid-argument-type]
    )


def design(host: SerialHost, hypothesis: str) -> None:
    host.drain_until_turn(PLANNER)
    host.run.reply_raw(host.run.expect_turn(role=PLANNER), plan_reply(hypothesis))


type Evaluation = tuple[str, dict[str, bool]]
"""An evaluation the implementer took itself: of which candidate, and each stage's pass."""
BOTH_PASS = {"accuracy": True, "benchmark": True}
BENCHMARK_ONLY = {"benchmark": True}
ACCURACY_FAILS = {"accuracy": False}


def measure_itself(host: SerialHost, evaluations: list[Evaluation]) -> None:
    """The implementer evaluates each ``(candidate, stages)`` itself.

    Core records each evaluation as one trusted ``local-validation`` record per stage it ran, of
    the open attempt, after the evaluations taken before; a stage that did not pass failed.
    """
    attempt = host.opened[-1]
    scope = Scope(owner=attempt.attempt_id, generation=attempt.generation)
    taken = list(host.run.view.current().measurements)
    for candidate, stages in evaluations:
        request = RequestId(root=f"agent-evaluation-{len(taken)}")
        for stage, passed in stages.items():
            taken.append(
                EvidenceRef(
                    evidence_id=EvidenceId(root=f"{stage}:{len(taken)}"),
                    kind=EvidenceKind.CORRECTNESS
                    if stage == "accuracy"
                    else EvidenceKind.BENCHMARK,
                    purpose="local-validation",
                    scope=scope,
                    source_request=request,
                    candidate=RevisionRef.of_git_commit(candidate),
                    observation_sequence=len(taken),
                    evaluator_digest="evaluator",
                    workload_digest="workload",
                    environment_digest="environment",
                    provenance="trusted",
                    status=ObservationStatus.SUCCEEDED if passed else ObservationStatus.FAILED,
                )
            )
    host.run.view.changes["measurements"] = tuple(taken)


def implement(
    host: SerialHost,
    search: str,
    candidate: str,
    *,
    evaluations: list[Evaluation] | None = None,
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
def test_a_round_that_adopts_a_candidate_is_a_passed_workstream_with_its_turns_and_gate(
    *, search: str, restarting: bool
) -> None:
    host = begin(search, rounds=1, restarting=restarting)
    design(host, "h1")
    implement(host, search, "c1")
    host.drain()

    (workstream,) = labelled(host, "workstream")
    assert workstream.label is not None
    assert workstream.label.title == "title h1"
    assert workstream.label.verdict == "passed"
    assert attrs(workstream) == {
        "hypothesis_id": "h1",
        "parent_revision": FACTS.baseline.revision_id.root,
        "base_round": None,
        "round": 1,
        "idea": "title h1",
        "revision": "c1",
        "outcome": None if search == "single" else "nominated",
        "summary": "did it",
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
        "input_metrics": [
            {
                "name": "throughput",
                "value": INPUT_THROUGHPUT,
                "direction": "max",
                "unit": "tokens/s",
            }
        ],
        "metrics": [
            {
                "name": "throughput",
                "value": CANDIDATE_THROUGHPUT,
                "direction": "max",
                "unit": "tokens/s",
            }
        ],
        "failed": None,
    }


@SEARCHES
@across_restarts
def test_a_round_builds_on_the_workstream_that_made_its_base_and_a_slow_one_is_slower(
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
    assert attrs(second)["base_round"] == 1
    assert attrs(second)["round"] == 2
    verdicts = [g.label.verdict for g in labelled(host, "gate") if g.label is not None]
    assert verdicts == ["passed", "slower"]
    # a slower idea still passed its own checks: its gates read and none turned it down
    assert [w.label.verdict for w in (first, second) if w.label] == ["passed", "passed"]


def gates_under(host: SerialHost, owner: Span) -> list[Span]:
    return [s for s in labelled(host, "gate") if s.parent == owner.id]


EVALUATIONS = {"both": BOTH_PASS, "benchmark": BENCHMARK_ONLY, "accuracy_fails": ACCURACY_FAILS}
"""The evaluations an implementer may take of its own work, which no gate ever reads."""


@SEARCHES
@across_restarts
@settings(max_examples=examples(pr=8, nightly=40), deadline=None, derandomize=True, database=None)
@given(
    rounds=st.lists(
        st.tuples(
            # what the implementer evaluated itself: (of the candidate it keeps?, which stages)
            st.lists(st.tuples(st.booleans(), st.sampled_from(sorted(EVALUATIONS))), max_size=3),
            st.sampled_from([10.0, 50.0, 80.0]),  # the kept candidate's throughput
        ),
        min_size=1,
        max_size=3,
    ),
)
def test_every_gate_is_the_framework_s_measurement_of_the_revision_its_round_retains(
    *,
    search: str,
    restarting: bool,
    rounds: list[tuple[list[tuple[bool, str]], float]],
) -> None:
    throughputs = {f"c{n}": value for n, (_, value) in enumerate(rounds, start=1)}
    host = begin(search, rounds=len(rounds), restarting=restarting, throughputs=throughputs)
    for n, (evaluations, _) in enumerate(rounds, start=1):
        design(host, f"h{n}")
        measured = [(f"c{n}" if kept else "other", EVALUATIONS[kind]) for kept, kind in evaluations]
        implement(host, search, f"c{n}", evaluations=measured)
    host.drain()

    note(f"officially measured: {host.measured}")
    # every idea that passes review is measured, in order
    assert host.measured == [f"c{n}" for n in range(1, len(rounds) + 1)]
    for n, workstream in enumerate(labelled(host, "workstream"), start=1):
        candidate, value = f"c{n}", throughputs[f"c{n}"]
        gates = gates_under(host, workstream)
        if candidate not in host.measured:
            assert gates == [], f"round {n} was not measured, yet has a gate"
            continue
        (gate,) = gates
        assert gate.label is not None
        beats = value > INPUT_THROUGHPUT
        assert (attrs(gate)["metric_value"], gate.label.verdict) == (
            value,
            "passed" if beats else "slower",
        ), f"round {n}"


RUN_3 = [
    # (the implementer's outcome, its advice for the checkpoint, what it evaluated itself)
    ("implemented", "unassessed", []),
    ("implemented", "unassessed", []),
    ("implemented", "unassessed", []),
    ("implemented", "discard", [ACCURACY_FAILS]),
    ("implemented", "discard", [ACCURACY_FAILS]),
    ("continue", "unassessed", []),
    ("implemented", "discard", [ACCURACY_FAILS]),
]
"""The rounds of a live multi run whose every workstream was accepted and none had a gate."""


def play_multi_round(
    host: SerialHost, n: int, outcome: str, disposition: str, evaluations: list[dict[str, bool]]
) -> None:
    """One multi round: its design, the implementer's reply, and the judge when it is due."""
    design(host, f"h{n}")
    host.drain_until_turn(IMPLEMENTER)
    measure_itself(host, [(f"c{n}", stages) for stages in evaluations])
    host.run.reply(
        host.run.expect_turn(role=IMPLEMENTER),
        multi_host.implemented(outcome, candidate_disposition=disposition),
        checkpoint=Retained(revision=RevisionRef.of_git_commit(f"c{n}")),
    )
    if outcome == "implemented":  # the only reply the sparse review cadence reviews here
        host.drain_until_turn(JUDGE)
        host.run.reply(host.run.expect_turn(role=JUDGE), multi_host.reviewed(passed=True))


def next_turn(host: SerialHost) -> str | None:
    """Answer everything else the ordinary way until a planner, implementer or judge turn opens."""
    for _ in range(100):
        open_roles = [item.label for item in host.run.open]
        for role in (JUDGE, IMPLEMENTER, PLANNER):
            if role in open_roles:
                return role
        if not host.step(0):
            return None
    message = f"no turn opened: {host.run.timeline()}"
    raise AssertionError(message)


def settled_workstreams(host: SerialHost) -> list[Span]:
    return [w for w in labelled(host, "workstream") if w.label and w.label.verdict is not None]


@across_restarts
def test_regression_an_implemented_idea_is_measured_whatever_the_implementer_thinks_of_it(
    *, restarting: bool
) -> None:
    # the live run showed all seven rounds as kept and none measured: implementers declared their
    # ideas inconclusive or disproven, which kept them out of the review and so out of the gates,
    # and the sparse review cadence reviewed only the one that declared itself ready. An
    # implementer now reports implementation status only: every candidate it implemented is
    # reviewed and measured by the gates, and what the measurement says is the framework's to
    # derive. Only a round that asked to continue has no candidate yet, so it is not measured.
    config = multi_host.config(max_rounds=10, judge_every=100)
    host = multi_host.MultiHost.start(config, restarting=restarting)
    for n, (outcome, disposition, evaluations) in enumerate(RUN_3, start=1):
        play_multi_round(host, n, outcome, disposition, evaluations)
    host.drain_until_turn(PLANNER)

    assert host.measured == ["c1", "c2", "c3", "c4", "c5", "c7"]
    rounds = settled_workstreams(host)
    assert [(w.label.verdict, w.label.reason) for w in rounds if w.label] == [
        ("passed", None),
        ("passed", None),
        ("passed", None),
        ("passed", None),
        ("passed", None),
        ("failed", "not_measured"),
        ("passed", None),
    ]
    # its code is still kept, and its own hypothesis may continue from it
    assert [attrs(w)["revision"] for w in rounds] == [f"c{n}" for n in range(1, 8)]
    for n, workstream in enumerate(rounds, start=1):
        gates = gates_under(host, workstream)
        # only a round the gates measured has one: what its implementer measured is no gate
        assert [g.label.verdict for g in gates if g.label] == ([] if n == 6 else ["passed"])


@across_restarts
@settings(max_examples=examples(pr=10, nightly=50), deadline=None, derandomize=True, database=None)
@given(
    rounds=st.lists(
        st.tuples(
            st.sampled_from(["implemented", "implementation_failed"]),
            st.sampled_from(["unassessed", "discard"]),
            st.booleans(),  # the judge passes it, when it reviews it
        ),
        min_size=1,
        max_size=4,
    ),
)
def test_a_multi_round_passed_when_the_gates_measured_it_and_nothing_turned_it_down(
    *, restarting: bool, rounds: list[tuple[str, str, bool]]
) -> None:
    config = multi_host.config(max_rounds=len(rounds) + 1, judge_every=100)
    host = multi_host.MultiHost.start(config, restarting=restarting)
    designed = 0
    reviewed: set[int] = set()
    implemented = 0  # one paid turn a round: the nth implementer reply is the nth round's
    while (role := next_turn(host)) is not None:
        if role == PLANNER:
            if implemented == len(rounds):
                break
            designed += 1
            design(host, f"h{designed}")
        elif role == IMPLEMENTER:
            if implemented == len(rounds):
                break
            outcome, disposition, _ = rounds[implemented]
            implemented += 1
            host.run.reply(
                host.run.expect_turn(role=IMPLEMENTER),
                multi_host.implemented(outcome, candidate_disposition=disposition),
                checkpoint=Retained(revision=RevisionRef.of_git_commit(f"c{implemented}")),
            )
        else:
            passes = rounds[implemented - 1][2]
            reviewed.add(implemented)
            host.run.reply(host.run.expect_turn(role=JUDGE), multi_host.reviewed(passed=passes))
    assert implemented == len(rounds)

    settled = settled_workstreams(host)
    assert len(settled) == len(rounds)
    note(f"reviewed rounds: {sorted(reviewed)}; officially measured: {host.measured}")
    for n, workstream in enumerate(settled, start=1):
        outcome, disposition, passes = rounds[n - 1]
        assert workstream.label is not None
        rejected_review = n in reviewed and not passes
        turned_down = (
            disposition == "discard" or outcome == "implementation_failed" or rejected_review
        )
        measured = f"c{n}" in host.measured
        eligible = measured and outcome == "implemented" and passes
        passed = measured and (not turned_down or eligible)
        assert workstream.label.verdict == ("passed" if passed else "failed"), n
        assert attrs(workstream)["revision"] == f"c{n}", "its code is kept either way"


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
        # a round names its hypothesis once it has one; one the designer gave none failed
        chosen = found.get("hypothesis_id") is not None
        assert chosen == (found.get("parent_revision") is not None)
        if span.label.verdict == "passed":
            assert chosen, f"{span.id} passed without a hypothesis"
        if not chosen and span.ended_at is not None and span.label.verdict is not None:
            assert span.label.reason == "no_design"
        # a passed round names the revision it kept; so does one its own checks turned down and
        # one no gate measured, whose code is kept all the same
        kept = found.get("revision")
        keeps = span.label.verdict == "passed" or span.label.reason in {
            "turned_down",
            "not_measured",
        }
        assert (kept is not None) == keeps, f"{span.id} kept {kept}"
        assert found.get("probe") is False
        links = [link.to for link in span.label.links]
        assert len(links) <= 1
        assert set(links) <= set(workstreams), f"{span.id} builds on a later span: {links}"
        for link in links:
            # one path: a round links to the round that made the revision it is based on
            assert attrs(by_id[link])["revision"] == found["parent_revision"], span.id
            assert attrs(by_id[link])["round"] == found["base_round"], span.id
        # the base is the input (no link, no base round) or an earlier round's retained code
        assert (found["base_round"] is None) == (not links) or not chosen, span.id
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
            assert owner.label.verdict != "failed", f"{gate.id} passed in a failed round"


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
