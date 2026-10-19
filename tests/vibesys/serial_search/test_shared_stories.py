"""What the two serial searches share, told as stories against each of them.

Single and multi design a hypothesis the same way and play a round in the same workspace, so what
the shared steps promise is stated once and run against both: the designer's correction, the
attribution that steers it, the base each round starts on, the paid turns a round may spend, and
what a workspace is closed with. Every story runs live and again with a restart after every step.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Unpack

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.multi import _host as multi_host
from tests.vibesys.orchestration.single import _host as single_host
from tests.vibesys.search_faults import assert_flow_closed
from tests.vibesys.serial_search._host import FACTS, SerialHost, World
from tests.vibesys.serial_search._replies import attribution, plan_reply
from tests.vibesys.vibesys_flows import across_restarts

from vs_core.api import (
    CaptureCommandBase,
    EndReason,
    Finish,
    Measure,
    Operation,
    RequestTurn,
    Retained,
    RevisionRef,
    RunResultProposal,
    Settle,
    StartAttempt,
    Wait,
    Withdraw,
    WorkspaceMode,
)
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from vs_core.api.testing import Asked

MODES = across_restarts
SEARCHES = pytest.mark.parametrize("search", ["single", "multi"])
PLANNER = "orchestrator"
IMPLEMENTER = "implementer"
JUDGE = "judge"
PROFILE_GUIDED = {"command": ("profile",), "timeout_seconds": 10}


def start(
    search: str,
    *,
    restarting: bool,
    config: dict[str, object] | None = None,
    **world: Unpack[World],
) -> SerialHost:
    """Run the input's measurement up to the first design turn."""
    settings_ = {"max_rounds": 1, **(config or {})}
    if search == "single":
        host: SerialHost = single_host.SingleHost.start(
            single_host.SingleConfig.model_validate(settings_), restarting=restarting, **world
        )
    else:
        host = multi_host.MultiHost.start(
            multi_host.MultiConfig.model_validate(settings_), restarting=restarting, **world
        )
    return host


def design(host: SerialHost, *plans: str) -> None:
    host.drain_until_turn(PLANNER)
    host.run.reply_raw(host.run.expect_turn(role=PLANNER), plan_reply(*plans))


def implement(host: SerialHost, search: str, candidate: str, *, passed: bool = True) -> None:
    """The implementer answers with a retained ``candidate``; in multi the judge then rules."""
    host.drain_until_turn(IMPLEMENTER)
    asked = host.run.expect_turn(role=IMPLEMENTER)
    checkpoint = Retained(revision=RevisionRef.of_git_commit(candidate))
    if search == "single":
        host.run.reply(asked, single_host.implemented(passed=passed), checkpoint=checkpoint)
        return
    host.run.reply(asked, multi_host.implemented(), checkpoint=checkpoint)
    host.drain_until_turn("judge")
    host.run.reply(host.run.expect_turn(role="judge"), multi_host.reviewed(passed=passed))


def paid_turns(host: SerialHost, role: str) -> int:
    return sum(
        isinstance(item, Wait)
        and isinstance(item.request, RequestTurn)
        and item.request.turn.charge_class == "paid"
        and role in item.request.turn.session.role_id.root
        for item in host.run.decisions
    )


def settlements(host: SerialHost) -> list[Settle]:
    return [
        item.request.disposition
        for item in host.run.decisions
        if isinstance(item, Wait)
        and isinstance(item.request, Withdraw)
        and isinstance(item.request.disposition, Settle)
    ]


def bases(host: SerialHost) -> list[RevisionRef]:
    return [
        item.request.workspace.base
        for item in host.run.decisions
        if isinstance(item, Wait) and isinstance(item.request, StartAttempt)
    ]


def input_measurements(host: SerialHost) -> int:
    """How many times the run asked core to measure the input."""
    return sum(
        isinstance(item, Wait)
        and isinstance(item.request, Measure)
        and item.request.plan.purpose == "baseline"
        for item in host.run.decisions
    )


def adopted(host: SerialHost) -> RunResultProposal:
    """The result of a run that adopted a retained candidate."""
    finish = host.run.finished
    assert isinstance(finish, Finish)
    assert finish.result.outcome == "success", finish.result.reason
    assert finish.result.selection is not None
    assert finish.result.selection.kind == "retained_candidate"
    return finish.result


def uncompared(result: RunResultProposal) -> bool:
    """Whether the run says its adopted candidate was never compared with the input."""
    return result.uncompared


# -- the input's measurement -----------------------------------------------------------------


@SEARCHES
@MODES
def test_an_input_measurement_lost_twice_ends_adopted_without_an_input_comparison(
    *, search: str, restarting: bool
) -> None:
    """A lost measurement shows nothing about the input, so it is taken once more, then stands.

    Both attempts are lost, so there is no bar: the candidate that passes its gates is adopted,
    and the run says it was never compared with the input.
    """
    host = start(search, restarting=restarting, baseline="lost")
    design(host, "h1")
    implement(host, search, "c1")
    host.drain()

    assert input_measurements(host) == host.input_measurements == 2
    assert uncompared(adopted(host))
    assert_flow_closed(host)


@SEARCHES
@MODES
def test_an_input_measurement_lost_once_is_taken_again_and_the_winner_is_compared_with_it(
    *, search: str, restarting: bool
) -> None:
    """The second measurement succeeds, so the candidate is compared with the input as usual."""
    host = start(search, restarting=restarting, baseline="lost", input_recovers=True)
    design(host, "h1")
    implement(host, search, "c1")
    host.drain()

    assert input_measurements(host) == host.input_measurements == 2
    assert not uncompared(adopted(host))
    assert_flow_closed(host)


@SEARCHES
@MODES
def test_an_uncompared_run_that_ends_on_its_deadline_still_says_it_was_never_compared(
    *, search: str, restarting: bool
) -> None:
    """H1: the deadline ends the run after an uncompared candidate was chosen; the mark stays."""
    host = start(search, restarting=restarting, config={"max_rounds": 3}, baseline="lost")
    design(host, "h1")
    implement(host, search, "c1")
    host.drain_until_turn(PLANNER)
    host.run.stop("deadline")
    host.drain()

    finish = host.run.finished
    assert isinstance(finish, Finish)
    assert finish.result.outcome == "cancelled"
    assert finish.result.reason.startswith(EndReason.DEADLINE.value)
    assert finish.result.selection is not None
    assert finish.result.selection.kind == "retained_candidate"
    assert finish.result.uncompared
    assert_flow_closed(host)


# -- the designer ----------------------------------------------------------------------------


@SEARCHES
@MODES
def test_a_designer_that_never_gives_a_valid_plan_ends_the_run_without_a_round(
    *, search: str, restarting: bool
) -> None:
    host = start(search, restarting=restarting)
    for _ in range(2):  # the correction the ledger allows, after the first answer
        host.drain_until_turn(PLANNER)
        host.run.reply_raw(host.run.expect_turn(role=PLANNER), "not json")
    host.drain(limit=100)

    assert not host.opened
    finish = host.run.finished
    assert isinstance(finish, Finish)
    assert finish.result.outcome == "failure"
    assert "designer" in finish.result.reason


@SEARCHES
@MODES
def test_a_plan_the_ledger_refuses_is_corrected_once_by_a_turn_that_names_the_problem(
    *, search: str, restarting: bool
) -> None:
    host = start(search, restarting=restarting, config={"max_rounds": 2})
    design(host, "h1")
    implement(host, search, "c1")
    host.drain_until_turn(PLANNER)
    host.run.reply_raw(host.run.expect_turn(role=PLANNER), plan_reply("h1"))  # a reused id
    correction = host.run.expect_turn(role=PLANNER)

    assert getattr(correction.context, "problem", None) is not None
    host.run.reply_raw(correction, plan_reply("h2"))
    host.drain_until_turn(IMPLEMENTER)


# -- the attribution -------------------------------------------------------------------------


@SEARCHES
@MODES
def test_the_attribution_is_captured_before_each_new_hypothesis_and_reaches_the_designer(
    *, search: str, restarting: bool
) -> None:
    host = start(
        search,
        restarting=restarting,
        config={"max_rounds": 2, "attribution": PROFILE_GUIDED},
        attribution_output=attribution(),
    )
    design(host, "h1")
    implement(host, search, "c1")
    host.drain_until_turn(PLANNER)
    prompt = host.run.expect_turn(role=PLANNER).context
    captures = [
        item
        for item in host.run.decisions
        if isinstance(item, Wait)
        and isinstance(item.request, Operation)
        and isinstance(item.request.request, CaptureCommandBase)
    ]

    assert len(captures) == 2
    assert [item.component for item in prompt.ranked_bottlenecks] == ["gemm", "io"]  # ty: ignore[unresolved-attribute]


@SEARCHES
@MODES
@settings(max_examples=6, deadline=None, derandomize=True, database=None)
@given(exit_code=st.integers(1, 255), output=st.text(max_size=20))
def test_a_failed_attribution_command_ends_the_run_naming_what_ran_and_why(
    *, search: str, restarting: bool, exit_code: int, output: str
) -> None:
    host = start(
        search,
        restarting=restarting,
        config={
            "max_rounds": 2,
            "attribution": {"command": ("bash", "attribute.sh"), "timeout_seconds": 10},
        },
        attribution_output=output,
        attribution_exit_code=exit_code,
    )
    host.drain(limit=100)

    assert not host.opened
    finish = host.run.finished
    assert isinstance(finish, Finish)
    reason = finish.result.reason
    assert "`bash attribute.sh`" in reason
    assert f"exited with code {exit_code}" in reason


# -- the base ---------------------------------------------------------------------------------------

INPUT = FACTS.baseline
INPUT_SPEED = 50.0
SLOWER_THAN_INPUT = 30.0
"""What a candidate measures (the input measures 50): it passes review but is slower."""


def commit(name: str) -> RevisionRef:
    return RevisionRef.of_git_commit(name)


def next_plan(host: SerialHost, *plans: str) -> None:
    """The designer answers its next turn; returns once the round's implementer is asked."""
    design(host, *plans)
    host.drain_until_turn(IMPLEMENTER)


@SEARCHES
@MODES
def test_a_plan_that_names_the_input_as_its_base_starts_its_round_on_the_input(
    *, search: str, restarting: bool
) -> None:
    host = start(search, restarting=restarting, config={"max_rounds": 2})
    design(host, "h1")
    implement(host, search, "c1")
    host.drain_until_turn(PLANNER)
    host.run.reply_raw(host.run.expect_turn(role=PLANNER), plan_reply("h2", base_round=0))
    host.drain_until_turn(IMPLEMENTER)

    assert bases(host)[1].revision_id == FACTS.baseline.revision_id


@SEARCHES
@MODES
def test_an_idea_starts_from_the_best_not_from_the_slower_idea_retained_after_it(
    *, search: str, restarting: bool
) -> None:
    """Regression: the base followed the newest retained checkpoint, however slow."""
    host = start(
        search,
        restarting=restarting,
        config={"max_rounds": 3},
        throughputs={"c1": 100.0, "c2": SLOWER_THAN_INPUT},
    )
    design(host, "h1")
    implement(host, search, "c1")
    host.drain_until_turn(PLANNER)
    host.run.reply_raw(host.run.expect_turn(role=PLANNER), plan_reply("h2"))
    implement(host, search, "c2")
    host.drain_until_turn(PLANNER)
    prompt = host.run.expect_turn(role=PLANNER).context
    host.run.reply_raw(host.run.expect_turn(role=PLANNER), plan_reply("h3"))
    host.drain_until_turn(IMPLEMENTER)

    assert bases(host) == [INPUT, commit("c1"), commit("c1")]
    # the designer is shown the passed ideas and which is best
    assert prompt.base_revision == commit("c1")  # ty: ignore[unresolved-attribute]
    assert [(item.round_number, item.best) for item in prompt.passed] == [(1, True), (2, False)]  # ty: ignore[unresolved-attribute]


@SEARCHES
@MODES
def test_a_plan_may_start_from_a_passed_idea_that_is_not_the_best(
    *, search: str, restarting: bool
) -> None:
    host = start(
        search,
        restarting=restarting,
        config={"max_rounds": 3},
        throughputs={"c1": 100.0, "c2": 150.0},
    )
    design(host, "h1")
    implement(host, search, "c1")
    host.drain_until_turn(PLANNER)
    host.run.reply_raw(host.run.expect_turn(role=PLANNER), plan_reply("h2"))
    implement(host, search, "c2")
    host.drain_until_turn(PLANNER)
    host.run.reply_raw(host.run.expect_turn(role=PLANNER), plan_reply("h3", base_round=1))
    host.drain_until_turn(IMPLEMENTER)

    assert bases(host)[2] == commit("c1")


@SEARCHES
@MODES
@pytest.mark.parametrize(
    ("named", "why"),
    [(1, "names an idea that did not pass"), (7, "names no idea of this run")],
    ids=["failed", "unknown"],
)
def test_a_base_naming_an_unknown_or_failed_idea_is_refused_by_its_number(
    *, search: str, restarting: bool, named: int, why: str
) -> None:
    host = start(
        search,
        restarting=restarting,
        config={"max_rounds": 3, "max_retries_per_round": 1, "max_continuation_rounds": 0},
    )
    design(host, "h1")
    implement(host, search, "c1", passed=False)
    host.drain_until_turn(PLANNER)
    host.run.reply_raw(host.run.expect_turn(role=PLANNER), plan_reply("h2", base_round=named))
    problem = getattr(host.run.expect_turn(role=PLANNER).context, "problem", None)

    assert problem is not None
    assert f"base_round {named} {why}" in problem


@SEARCHES
@MODES
def test_an_idea_that_continues_its_hypothesis_builds_on_its_own_work_in_progress(
    *, search: str, restarting: bool
) -> None:
    host = start(
        search,
        restarting=restarting,
        config={"max_rounds": 3},
        throughputs={"c1": 100.0},
    )
    design(host, "h1")
    implement(host, search, "c1")
    host.drain_until_turn(PLANNER)
    host.run.reply_raw(host.run.expect_turn(role=PLANNER), plan_reply("h2"))
    # h2 retains work in progress that did not pass; the best is still c1
    implement(host, search, "c2", passed=False)
    host.drain()

    # round 3 continues h2 (no designer turn), on the work in progress round 2 retained
    assert bases(host)[:3] == [INPUT, commit("c1"), commit("c2")]


_DRAWN_ROUNDS = st.lists(
    st.tuples(
        st.sampled_from([30.0, 70.0, 90.0, 120.0, 160.0]),
        st.booleans(),
        st.none() | st.integers(0, 5),
    ),
    min_size=1,
    max_size=5,
)


@dataclass
class _Played:
    """What a driven run recorded: the base each round started on, and what it should be."""

    bases: list[RevisionRef] = field(default_factory=list)
    expected: list[RevisionRef] = field(default_factory=list)
    passed: dict[int, RevisionRef] = field(default_factory=dict)
    produced: dict[str, int] = field(default_factory=dict)
    """The round that made each commit: the first to retain it as new work."""
    keeps_base: frozenset[int] = frozenset()
    """The failed rounds that retain the base they started on unchanged, as an implementation
    that failed before changing anything does."""
    best: tuple[float, int] | None = None
    designed: RevisionRef | None = None
    """The base the latest accepted plan gave, until the round it designed starts."""

    def base_of_plan(self, named: int | None) -> RevisionRef | None:
        """The base a plan gets: the one it names (the input or a passed idea), else the best;
        None when it names neither, so the ledger refuses it."""
        if named is None:
            return INPUT if self.best is None else commit(f"c{self.best[1]}")
        return INPUT if named == 0 else self.passed.get(named)

    def note_starts(self, starts: list[RevisionRef]) -> None:
        """Record each round that started since last looked: a round with no design before it
        continues the one before it, on the work in progress that one retained."""
        while len(self.bases) < len(starts):
            number = len(self.bases) + 1
            self.bases.append(starts[number - 1])
            self.expected.append(self.designed or commit(f"c{number - 1}"))
            self.designed = None


def _answer(
    host: SerialHost,
    search: str,
    asked: Asked,
    played: _Played,
    draw: tuple[float, bool, int | None],
) -> None:
    """Answer one turn as round ``len(played.bases)``'s draw says."""
    speed, ok, named = draw
    number = max(len(played.bases), 1)
    if asked.label == PLANNER:
        if getattr(asked.context, "problem", None) is not None:
            named = None  # asked again after a refusal: no base this time
        extra = {} if named is None else {"base_round": named}
        host.run.reply_raw(asked, plan_reply(f"h{len(played.bases) + 1}", **extra))
        played.designed = played.base_of_plan(named) or played.designed
        return
    if asked.label == JUDGE:
        host.run.reply(asked, multi_host.reviewed(passed=ok))
        return
    unchanged = not ok and number in played.keeps_base
    retained = played.bases[number - 1] if unchanged else commit(f"c{number}")
    if not unchanged:
        played.produced.setdefault(retained.revision_id.root, number)
    checkpoint = Retained(revision=retained)
    if search == "single":
        host.run.reply(asked, single_host.implemented(passed=ok), checkpoint=checkpoint)
    else:
        host.run.reply(asked, multi_host.implemented(), checkpoint=checkpoint)
    if ok:
        played.passed[number] = commit(f"c{number}")
        if speed > INPUT_SPEED and (played.best is None or speed > played.best[0]):
            played.best = (speed, number)


def _drive(
    host: SerialHost,
    search: str,
    rounds: list[tuple[float, bool, int | None]],
    keeps_base: frozenset[int] = frozenset(),
) -> _Played:
    """Answer every agent turn of a run from the drawn rounds, and say what each base must be.

    Round ``n`` retains ``c<n>`` and reads as the ``n``-th draw says. A designer turn names the
    drawn base; a refused name is asked again without one.
    The oracle: an idea passes when it passes review (the host's gates always read); it is best
    when it also beats the input (50) by more than every earlier best.
    """
    played = _Played(keeps_base=keeps_base)
    for _ in range(1000):
        starts = bases(host)
        played.note_starts(starts)
        asked = next(
            (
                item
                for item in host.run.open
                if item.until is not None and item.label in {PLANNER, IMPLEMENTER, JUDGE}
            ),
            None,
        )
        if asked is None:
            if not host.step(0):
                return played
            continue
        draw = rounds[min(max(len(starts), 1), len(rounds)) - 1]
        _answer(host, search, asked, played, draw)
    message = f"the run did not settle: {host.run.timeline()}"
    raise AssertionError(message)


@SEARCHES
@MODES
@settings(max_examples=examples(pr=8, nightly=60), deadline=None, derandomize=True, database=None)
@given(rounds=_DRAWN_ROUNDS, continuations=st.integers(0, 2))
def test_every_base_is_the_best_a_named_passed_idea_the_input_or_the_rounds_own_work(
    *,
    search: str,
    restarting: bool,
    rounds: list[tuple[float, bool, int | None]],
    continuations: int,
) -> None:
    """Whatever each idea measures, passes and names as its base, and whether a failed idea is
    continued, every round starts on the best, a named passed idea, the input, or (a
    continuation) its own work in progress; never a failed or unknown idea. A named base is
    honored only for the input or a passed idea, else the plan is refused and asked again
    without one. The best and the adopted result rest on a trusted measurement of that commit.
    """
    speeds = {f"c{number}": speed for number, (speed, _, _) in enumerate(rounds, start=1)}
    host = start(
        search,
        restarting=restarting,
        config={"max_rounds": len(rounds), "max_continuation_rounds": continuations},
        throughputs=speeds,
    )
    played = _drive(host, search, rounds)

    assert played.bases == played.expected
    passed = set(played.passed.values())
    for base in played.bases:
        if base in passed:
            assert base.revision_id.root in host.measured, f"{base} is a base but never measured"
    finish = host.run.finished
    assert isinstance(finish, Finish)
    selection = finish.result.selection
    assert selection is not None
    if played.best is None:
        assert selection.kind != "retained_candidate"
    else:
        assert selection.kind == "retained_candidate"
        assert selection.revision == commit(f"c{played.best[1]}")
        # the adopted result rests on a trusted measurement of that exact commit
        assert selection.revision.revision_id.root in host.measured
    assert_flow_closed(host)


@SEARCHES
@MODES
@settings(max_examples=examples(pr=8, nightly=60), deadline=None, derandomize=True, database=None)
@example(rounds=[(30.0, False, None), (30.0, False, None)], keeps_base=frozenset({1}))
@example(
    rounds=[(90.0, True, None), (30.0, False, None), (30.0, False, None)], keeps_base=frozenset({2})
)
@given(rounds=_DRAWN_ROUNDS, keeps_base=st.sets(st.integers(1, 5)).map(frozenset))
def test_regression_a_failed_round_that_kept_its_base_unchanged_is_never_the_base_round(
    *,
    search: str,
    restarting: bool,
    rounds: list[tuple[float, bool, int | None]],
    keeps_base: frozenset[int],
) -> None:
    """A round that retained its base commit made nothing, so a later round on that commit
    names the round that made it (none for the input), never the failed round that held it.
    """
    speeds = {f"c{number}": speed for number, (speed, _, _) in enumerate(rounds, start=1)}
    host = start(
        search,
        restarting=restarting,
        config={"max_rounds": len(rounds), "max_continuation_rounds": 0},
        throughputs=speeds,
    )
    played = _drive(host, search, rounds, keeps_base)

    workstreams = [
        (json.loads(span.label.attrs_json), span.label.verdict)
        for span in host.run.trace().spans
        if span.name == "play_round" and span.label is not None and span.label.attrs_json
    ]
    assert workstreams
    for found, _ in workstreams:
        parent = found.get("parent_revision")
        if parent is not None:
            assert found["base_round"] == played.produced.get(parent), found
    failed = {found["round"] for found, verdict in workstreams if verdict == "failed"}
    assert all(found.get("base_round") not in failed for found, _ in workstreams)


# -- the paid turns and the workspace --------------------------------------------------------


@SEARCHES
@MODES
@settings(max_examples=8, deadline=None, derandomize=True, database=None)
@given(retries=st.integers(1, 3))
def test_a_round_never_spends_more_paid_turns_than_its_retry_budget(
    *, search: str, restarting: bool, retries: int
) -> None:
    host = start(search, restarting=restarting, config={"max_retries_per_round": retries})
    design(host, "h1")
    for _ in range(retries + 1):  # more rejections than the budget allows
        if not any(item.label == IMPLEMENTER for item in host.run.open):
            host.step(0)
        if any(item.label == IMPLEMENTER for item in host.run.open):
            implement(host, search, "c1", passed=False)
    host.drain()

    assert paid_turns(host, IMPLEMENTER) <= retries
    assert_flow_closed(host)


@SEARCHES
@MODES
def test_the_retained_checkpoint_of_a_round_that_failed_review_is_work_in_progress(
    *, search: str, restarting: bool
) -> None:
    host = start(search, restarting=restarting, config={"max_retries_per_round": 1})
    design(host, "h1")
    implement(host, search, "c1", passed=False)
    host.drain()

    (settled,) = settlements(host)
    assert not settled.eligible
    assert settled.retention in {"wip", "discard"}
    assert_flow_closed(host)


@SEARCHES
@MODES
def test_a_settlement_core_refuses_ends_the_round_instead_of_stranding_it(
    *, search: str, restarting: bool
) -> None:
    host = start(search, restarting=restarting)
    design(host, "h1")
    implement(host, search, "c1")
    # Every close of the workspace is refused once with its assertion; core takes the plain close.
    for _ in range(100):
        pending = host.open(Withdraw)
        if pending:
            host.answer(pending[0], 3)
        elif not host.step(0):
            break
    assert host.refused_closes >= 1
    assert_flow_closed(host)


@SEARCHES
@MODES
def test_a_start_core_refuses_is_a_failed_round_not_a_stall(
    *, search: str, restarting: bool
) -> None:
    host = start(search, restarting=restarting)
    host.refusals["start"] = 1
    design(host, "h1")
    for asked in host.open(StartAttempt):
        host.answer(asked)
    host.drain()

    assert host.refused_starts == 1
    assert_flow_closed(host)


@SEARCHES
@MODES
@pytest.mark.parametrize("isolated", [True, False], ids=["isolated", "exclusive-root"])
def test_the_round_works_where_the_environment_lets_a_candidate_work(
    *, search: str, restarting: bool, isolated: bool
) -> None:
    host = start(search, restarting=restarting, config={"exclusive_root": not isolated})
    design(host, "h1")
    implement(host, search, "c1")
    host.drain()

    expected = WorkspaceMode.ISOLATED_CHILD if isolated else WorkspaceMode.EXCLUSIVE_ROOT
    plans = [
        item.request.workspace.mode
        for item in host.run.decisions
        if isinstance(item, Wait) and isinstance(item.request, StartAttempt)
    ]
    turns = [
        item.request.turn.workspace.mode
        for item in host.run.decisions
        if isinstance(item, Wait)
        and isinstance(item.request, RequestTurn)
        and item.request.turn.session.role_id.root.endswith(IMPLEMENTER)
        and hasattr(item.request.turn.workspace, "mode")
    ]
    assert plans == [expected]
    assert turns
    assert set(turns) == {expected}
    finish = host.run.finished
    assert isinstance(finish, Finish)
    assert finish.result.selection is not None
    assert_flow_closed(host)
