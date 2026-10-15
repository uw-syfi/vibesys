"""A workstream's story, from the planner's plan to the settlement of its workspace.

Each story plays the host of the dynamic flow and scripts one slot's life: implement, review,
measure, settle; a continued hypothesis; a child of a parent that no longer reproduces; a profile;
a refused start; turns that time out; every way the input's own measurement can end; a stop at
every step. Every story runs live and again with a restart after every step: the decisions must
not change. What core itself guarantees about these waits (retry, recovery, release) is core's
own tests; what is asserted here is only what the search decided.
"""

from __future__ import annotations

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic._host import (
    INPUT_THROUGHPUT,
    PLANNER_LABEL,
    Host,
    begin,
    config,
)
from tests.vibesys.orchestration.dynamic._replies import (
    implement,
    implemented,
    plan_reply,
    profile,
    reviewed,
)
from tests.vibesys.search_faults import assert_flow_closed
from tests.vibesys.vibesys_flows import across_restarts

from vibesys.orchestration.dynamic.models import ImplementerResult, ProfilerResult, ReviewResult
from vibesys.orchestration.dynamic.search.api import (
    FailureKind,
    PlannerCorrection,
    PlannerPrompt,
    RunSnapshot,
    WorkKind,
)
from vs_core.api import (
    Capabilities,
    FailureReason,
    Finish,
    Operation,
    Retained,
    RevisionRef,
    StartAttempt,
)
from vs_core.api.testing import Asked, FakeView
from vs_sim.api.testing import examples

MODES = across_restarts


def revision(name: str) -> RevisionRef:
    return RevisionRef.of_git_commit(name)


def start(cfg: object, *, restarting: bool, **world: object) -> Host:
    """Run the input's measurement up to the first planner turn."""
    host = begin(cfg, restarting=restarting, **world)  # ty: ignore[invalid-argument-type]
    host.drain_until_turn(PLANNER_LABEL)
    return host


def plan(host: Host, *entries: dict[str, object]) -> None:
    host.run.reply_raw(host.run.expect_turn(role=PLANNER_LABEL), plan_reply(*entries))


def open_slot(host: Host, hypothesis: str) -> Asked:
    """Admit the workstream of ``hypothesis`` (core opens its workspace)."""
    for asked in host.run.open:
        request = asked.request
        if isinstance(request, StartAttempt) and f":{hypothesis}:" in request.attempt_id.root:
            host.answer(asked)
            return asked
    message = f"no start for {hypothesis}: {host.run.timeline()}"
    raise AssertionError(message)


def turn_of(host: Host, role: str, hypothesis: str) -> Asked:
    """The open turn of ``role`` that is about ``hypothesis``."""
    for asked in host.run.open:
        about = getattr(asked.context, "hypothesis_id", None)
        if asked.label == role and about == hypothesis:
            return asked
    message = f"no {role} turn for {hypothesis}: {host.run.timeline()}"
    raise AssertionError(message)


def implement_turn(
    host: Host,
    hypothesis: str,
    candidate: str | None,
    outcome: str = "supported",
    next_step: str | None = None,
) -> None:
    """The implementer of ``hypothesis`` answers, and core retains ``candidate`` for the turn."""
    result = ImplementerResult.model_validate_json(
        implemented(outcome, **({} if next_step is None else {"next_step": next_step}))
    )
    checkpoint = None if candidate is None else Retained(revision=revision(candidate))
    host.run.reply(turn_of(host, "implementer", hypothesis), result, checkpoint=checkpoint)


def judge_turn(host: Host, hypothesis: str, *, passed: bool) -> None:
    verdict = ReviewResult.model_validate_json(reviewed(passed=passed))
    host.run.reply(turn_of(host, "judge", hypothesis), verdict)


def record_of(host: Host) -> RunSnapshot:
    record = host.record
    assert record is not None
    return record


def selection_of(host: Host) -> tuple[str, str | None]:
    """The kind of what the run adopted and the commit of a retained candidate."""
    finish = host.run.finished
    assert isinstance(finish, Finish)
    chosen = finish.result.selection
    assert chosen is not None, finish.result
    commit = chosen.revision.git_commit if chosen.kind == "retained_candidate" else None
    return chosen.kind, commit


def planner_context(host: Host) -> PlannerPrompt | PlannerCorrection:
    context = host.run.expect_turn(role=PLANNER_LABEL).context
    assert isinstance(context, PlannerPrompt | PlannerCorrection)
    return context


# -- implement, review, measure, settle ------------------------------------------------------------


@MODES
@settings(max_examples=examples(pr=10, nightly=25), deadline=None, derandomize=True, database=None)
@example(throughputs=[30], judged=[True, True, True], finishing=[0, 1, 2])
@example(throughputs=[90, 60, 120], judged=[True, True, False], finishing=[2, 0, 1])
@example(throughputs=[40, 30], judged=[True, True, True], finishing=[0, 1, 2])
@given(
    throughputs=st.lists(st.integers(20, 100), min_size=1, max_size=3, unique=True),
    judged=st.lists(st.booleans(), min_size=3, max_size=3),
    finishing=st.lists(st.integers(0, 2), min_size=3, max_size=3),
)
def test_the_adopted_candidate_is_the_best_eligible_one_or_the_input_stands(
    *, restarting: bool, throughputs: list[int], judged: list[bool], finishing: list[int]
) -> None:
    """Whatever the candidates measure, the judge rules and the order the workstreams end in.

    A candidate is eligible when the judge passed it and it beats the input. A rejected
    workstream spends both its turns, each on a candidate as fast as the generated throughput,
    so a search that ignored the verdict would adopt it.
    """
    names = [f"h{n}" for n in range(1, len(throughputs) + 1)]
    order = sorted(names, key=lambda name: finishing[int(name[1:]) - 1])
    speeds = {f"c{n}": float(speed) for n, speed in enumerate(throughputs, start=1)}
    speeds |= {f"{key}-again": speed for key, speed in speeds.items()}
    host = start(
        config(max_rounds=1, max_in_flight=len(names)),
        restarting=restarting,
        throughputs=speeds,
    )
    plan(host, *(implement(name) for name in names))
    for name in names:
        open_slot(host, name)
    for name in order:
        candidate = f"c{name[1:]}"
        passed = judged[int(name[1:]) - 1]
        implement_turn(host, name, candidate)
        judge_turn(host, name, passed=passed)
        if not passed:  # the default bound is two turns without an accepted candidate
            implement_turn(host, name, f"{candidate}-again")
            judge_turn(host, name, passed=False)
    host.drain()

    eligible = {
        f"c{n}": speed
        for n, speed in enumerate(throughputs, start=1)
        if judged[n - 1] and speed > INPUT_THROUGHPUT
    }
    if eligible:
        best = max(eligible, key=eligible.__getitem__)
        assert selection_of(host) == ("retained_candidate", best)
    else:
        assert selection_of(host) == ("trusted_baseline", None)
    assert_flow_closed(host)


@MODES
def test_a_candidate_that_does_not_beat_the_input_is_not_adopted(*, restarting: bool) -> None:
    host = start(config(), restarting=restarting, throughputs={"c1": 10.0})
    plan(host, implement("h1"))
    open_slot(host, "h1")
    implement_turn(host, "h1", "c1")
    judge_turn(host, "h1", passed=True)
    host.drain()

    assert selection_of(host) == ("trusted_baseline", None)
    assert [row.eligible for row in record_of(host).hypotheses[0].rounds] == [False]
    assert_flow_closed(host)


@MODES
def test_two_turns_the_judge_rejects_end_the_workstream_and_nothing_is_adopted(
    *, restarting: bool
) -> None:
    host = start(config(), restarting=restarting)
    plan(host, implement("h1"))
    open_slot(host, "h1")
    for _ in range(2):  # the default bound is two turns without a measurable candidate
        implement_turn(host, "h1", "c1")
        judge_turn(host, "h1", passed=False)
    host.drain()

    (done,) = record_of(host).finished
    assert done.failure is not None
    assert done.failure.kind is FailureKind.TURN_LIMIT
    assert selection_of(host) == ("trusted_baseline", None)
    assert_flow_closed(host)


@MODES
def test_a_judge_rejection_gets_the_implementer_one_more_turn_that_is_told_why(
    *, restarting: bool
) -> None:
    host = start(config(), restarting=restarting)
    plan(host, implement("h1"))
    open_slot(host, "h1")
    implement_turn(host, "h1", "c1")
    judge_turn(host, "h1", passed=False)

    again = turn_of(host, "implementer", "h1")
    assert getattr(again.context, "rejection", None) == "fix"
    assert getattr(again.context, "turns_without_candidate", None) == 1


@MODES
def test_two_timed_out_implementer_turns_end_the_workstream_and_return_its_slot_to_the_planner(
    *, restarting: bool
) -> None:
    host = start(config(max_rounds=2), restarting=restarting)
    plan(host, implement("h1"))
    open_slot(host, "h1")
    host.run.fail(turn_of(host, "implementer", "h1"), FailureReason.TIMED_OUT)
    host.run.fail(turn_of(host, "implementer", "h1"), FailureReason.TIMED_OUT)
    host.drain_until_turn(PLANNER_LABEL)

    (done,) = record_of(host).finished
    assert done.failure is not None
    assert (done.failure.kind, done.failure.turns, done.failure.limit) == (
        FailureKind.TURN_LIMIT,
        2,
        2,
    )
    assert planner_context(host).capacity == 1  # the slot is free for the next plan


@MODES
def test_an_invalid_plan_is_corrected_once_by_a_turn_that_names_the_problem(
    *, restarting: bool
) -> None:
    host = start(config(), restarting=restarting)
    host.run.reply_raw(host.run.expect_turn(role=PLANNER_LABEL), "not json")

    correction = planner_context(host)
    assert isinstance(correction, PlannerCorrection)
    assert correction.problem is not None
    assert correction.problem.kind.value == "unparseable"
    plan(host, implement("h1"))
    assert open_slot(host, "h1")


# -- continuation and parents ----------------------------------------------------------------------


def finish_first_round(host: Host, hypothesis: str, candidate: str) -> None:
    open_slot(host, hypothesis)
    implement_turn(host, hypothesis, candidate, "inconclusive", next_step="try the next kernel")
    judge_turn(host, hypothesis, passed=True)


def verify_parents(host: Host) -> None:
    """Core checks that each parent revision still reproduces; the world answers."""
    for asked in host.open(Operation):
        host.answer(asked)


@MODES
def test_a_continued_hypothesis_builds_on_its_own_last_candidate(*, restarting: bool) -> None:
    host = start(config(max_rounds=2), restarting=restarting)
    plan(host, implement("h1"))
    finish_first_round(host, "h1", "c1")
    host.drain_until_turn(PLANNER_LABEL)
    plan(host, implement("h1", continue_hypothesis=True, task="next kernel"))
    verify_parents(host)

    (asked,) = host.open(StartAttempt)
    assert isinstance(asked.request, StartAttempt)
    assert asked.request.workspace.base == revision("c1")
    host.answer(asked)
    implement_turn(host, "h1", "c2", "supported")
    judge_turn(host, "h1", passed=True)
    host.drain()

    (hypothesis,) = record_of(host).hypotheses
    assert [row.sequence for row in hypothesis.rounds] == [1, 2]
    assert hypothesis.continuation_rounds == 1
    assert_flow_closed(host)


@MODES
def test_a_child_is_started_from_the_parent_revision_when_it_still_reproduces(
    *, restarting: bool
) -> None:
    host = start(config(max_rounds=3), restarting=restarting, verified=True)
    plan(host, implement("h1"))
    finish_first_round(host, "h1", "c1")
    host.drain_until_turn(PLANNER_LABEL)
    plan(host, implement("h2", parent_hypothesis_id="h1"))
    verify_parents(host)

    (asked,) = host.open(StartAttempt)
    assert isinstance(asked.request, StartAttempt)
    assert asked.request.workspace.base == revision("c1")


@MODES
def test_a_child_of_a_parent_that_no_longer_reproduces_is_never_started(
    *, restarting: bool
) -> None:
    host = start(config(max_rounds=3), restarting=restarting, verified=False)
    plan(host, implement("h1"))
    finish_first_round(host, "h1", "c1")
    host.drain_until_turn(PLANNER_LABEL)
    plan(host, implement("h2", parent_hypothesis_id="h1"))
    verify_parents(host)

    assert not host.open(StartAttempt)
    failed = [item for item in record_of(host).finished if item.plan.work_id == "h2"]
    assert [item.failure.kind for item in failed if item.failure] == [FailureKind.PARENT_UNVERIFIED]


# -- refused starts, profiles, the input's own measurement -----------------------------------------


@MODES
def test_a_start_core_refuses_fails_that_workstream_and_the_planner_is_asked_again(
    *, restarting: bool
) -> None:
    host = start(config(max_rounds=2), restarting=restarting)
    host.refusals["start"] = 1
    plan(host, implement("h1"))
    (asked,) = host.open(StartAttempt)
    host.answer(asked)
    host.drain_until_turn(PLANNER_LABEL)

    assert host.refused_starts == 1
    (done,) = record_of(host).finished
    assert done.failure is not None
    assert done.failure.kind is FailureKind.REFUSED
    assert done.failure.cause == host.refusal_code
    assert not host.unsettled


@MODES
def test_a_profile_workstream_asks_the_profiler_and_records_a_row_for_the_planner(
    *, restarting: bool
) -> None:
    host = start(
        config(profiling=True),
        restarting=restarting,
        view=FakeView(
            changes={"capabilities": Capabilities(lifecycle=frozenset({"profile-capture"}))}
        ),
    )
    plan(host, profile("p1"))
    open_slot(host, "p1")
    result = ProfilerResult.model_validate_json(implemented("supported"))
    host.run.reply(host.run.expect_turn(role="profiler"), result, checkpoint=None)
    host.drain()

    (done,) = record_of(host).finished
    assert done.plan.work_id == "p1"
    assert done.plan.kind is WorkKind.PROFILE
    assert done.failure is None
    assert_flow_closed(host)


@MODES
@pytest.mark.parametrize("baseline", ["failed-workload", "no-evidence", "lost"])
def test_an_input_that_cannot_be_measured_is_told_to_the_planner_not_hidden(
    *, restarting: bool, baseline: str
) -> None:
    host = begin(config(), restarting=restarting, baseline=baseline)  # ty: ignore[invalid-argument-type]
    host.drain(limit=50)

    assert host.run.finished is not None or host.run.open
    prompts = [a for a in host.run.open if a.label == PLANNER_LABEL]
    if prompts:
        context = prompts[0].context
        assert isinstance(context, PlannerPrompt)
        assert context.input_failure is not None
        assert context.baseline == ()


@MODES
def test_a_measured_input_is_shown_to_the_planner_as_its_baseline(*, restarting: bool) -> None:
    host = begin(config(), restarting=restarting, baseline="measured")
    # The first ask went out before the measurement landed; the planner's next turn shows it.
    host.run.fail(host.run.expect_turn(role=PLANNER_LABEL), FailureReason.TIMED_OUT)

    context = host.run.expect_turn(role=PLANNER_LABEL).context
    assert isinstance(context, PlannerPrompt)
    assert not context.baseline_pending
    assert context.input_failure is None
    assert context.baseline != ()
