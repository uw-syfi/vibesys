"""The multi flow told as a story, by hand: no core, no clock, no event loop.

Each story plays the host of `multi`: it answers the waits the search opens in the order the story
chooses and reads what the search decided. Every story runs live and again with a restart after
every step: the decisions must not change. What core guarantees about these waits is core's own
tests; what is asserted here is only what the search decided.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Unpack

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.multi._host import (
    IMPLEMENTER,
    JUDGE,
    PLANNER,
    PROFILER,
    RECIPE,
    ROUTER,
    MultiHost,
    config,
    implemented,
    profiled,
    reviewed,
    routed,
)
from tests.vibesys.search_faults import assert_flow_closed
from tests.vibesys.serial_search._host import (
    CANDIDATE_THROUGHPUT,
    INPUT_THROUGHPUT,
    Baseline,
    World,
)
from tests.vibesys.serial_search._replies import plan_reply
from tests.vibesys.vibesys_flows import across_restarts

from vs_core.api import (
    CheckpointDecline,
    Declined,
    FailureReason,
    Finish,
    Operation,
    RequestTurn,
    Retained,
    RevisionRef,
    Settle,
    StartAttempt,
    ValidateLocalBase,
    Wait,
    Withdraw,
    WorkspaceRef,
)
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from vibesys.orchestration.multi.search.api import MultiConfig

MODES = across_restarts


def start(
    selected: MultiConfig | None = None,
    *,
    restarting: bool,
    validating: bool = False,
    **world: Unpack[World],
) -> MultiHost:
    """Run the input's measurement up to the first design turn."""
    host = MultiHost.start(
        config() if selected is None else selected,
        restarting=restarting,
        validating=validating,
        **world,
    )
    host.drain_until_turn(PLANNER)
    return host


def design(host: MultiHost, identifier: str = "h1") -> None:
    host.run.reply_raw(host.run.expect_turn(role=PLANNER), plan_reply(identifier))


def implement(host: MultiHost, outcome: str = "implemented", candidate: str = "def456") -> None:
    host.drain_until_turn(IMPLEMENTER)
    host.run.reply(
        host.run.expect_turn(role=IMPLEMENTER),
        implemented(outcome),
        checkpoint=Retained(revision=RevisionRef.of_git_commit(candidate)),
    )


def judge(host: MultiHost, *, passed: bool) -> None:
    host.drain_until_turn(JUDGE)
    host.run.reply(host.run.expect_turn(role=JUDGE), reviewed(passed=passed))


def selection_of(host: MultiHost) -> tuple[str, str | None]:
    finish = host.run.finished
    assert isinstance(finish, Finish)
    chosen = finish.result.selection
    assert chosen is not None, finish.result
    commit = chosen.revision.git_commit if chosen.kind == "retained_candidate" else None
    return chosen.kind, commit


@MODES
def test_a_candidate_the_judge_passes_and_the_gates_confirm_is_adopted(*, restarting: bool) -> None:
    host = start(restarting=restarting)
    design(host)
    implement(host)
    judge(host, passed=True)
    host.drain()

    assert selection_of(host) == ("retained_candidate", "def456")
    record = host.record
    assert record is not None
    assert [row.passed for row in record.hypotheses[0].rounds] == [True]
    assert_flow_closed(host)


@MODES
def test_a_judge_that_cannot_be_reached_leaves_the_candidate_ineligible(
    *, restarting: bool
) -> None:
    """Dynamic reads a missing review as a pass; multi needs a pass to adopt anything."""
    host = start(restarting=restarting)
    design(host)
    implement(host)
    host.drain_until_turn(JUDGE)
    host.run.fail(host.run.expect_turn(role=JUDGE), FailureReason.TIMED_OUT)
    host.drain()

    assert selection_of(host) == ("trusted_baseline", None)
    assert_flow_closed(host)


@MODES
def test_a_judge_rejection_gets_the_implementer_another_turn(*, restarting: bool) -> None:
    host = start(config(max_retries_per_round=2), restarting=restarting)
    design(host)
    implement(host)
    judge(host, passed=False)
    implement(host, candidate="c2")
    judge(host, passed=True)
    host.drain()

    assert len(host.opened) == 1
    assert selection_of(host) == ("retained_candidate", "c2")
    assert_flow_closed(host)


@MODES
def test_a_blocked_implementer_ends_the_round_and_nothing_is_adopted(*, restarting: bool) -> None:
    host = start(restarting=restarting)
    design(host)
    host.drain_until_turn(IMPLEMENTER)
    host.run.reply(host.run.expect_turn(role=IMPLEMENTER), implemented("blocked"))
    host.drain()

    assert selection_of(host) == ("trusted_baseline", None)
    assert_flow_closed(host)


@MODES
def test_a_validation_recipe_that_fails_is_told_to_the_implementer_not_adopted(
    *, restarting: bool
) -> None:
    host = start(
        config(max_retries_per_round=2), restarting=restarting, validating=True, validation="failed"
    )
    design(host)
    host.drain_until_turn(IMPLEMENTER)
    host.run.reply(
        host.run.expect_turn(role=IMPLEMENTER),
        implemented(validation_recipe_artifact=RECIPE),
        checkpoint=Retained(revision=RevisionRef.of_git_commit("c1")),
    )
    judge(host, passed=True)
    host.drain_until_turn(IMPLEMENTER)
    again = host.run.expect_turn(role=IMPLEMENTER)
    assert getattr(again.context, "validation_failure", None) is not None


@MODES
def test_the_router_may_ask_for_a_profile_and_the_profiler_is_asked_before_the_design(
    *, restarting: bool
) -> None:
    host = MultiHost.start(config(profiling=True), restarting=restarting)
    host.drain_until_turn(ROUTER)
    host.run.reply(host.run.expect_turn(role=ROUTER), routed(need_profile=True, focus="gemm"))
    host.drain_until_turn(PROFILER)
    host.run.reply(host.run.expect_turn(role=PROFILER), profiled())
    host.drain_until_turn(PLANNER)
    design(host)
    implement(host)
    judge(host, passed=True)
    host.drain()

    assert selection_of(host) == ("retained_candidate", "def456")
    assert_flow_closed(host)


@MODES
def test_a_later_rounds_router_and_profiler_see_the_retained_candidate_not_the_input(
    *, restarting: bool
) -> None:
    """Regression: the run root holds the input until adoption, so advisors there saw no work.

    A live run's router read the root, found only the input's README, and declined to profile a
    server that had already passed the gates; its profiler, run in the root, could not have
    captured the candidate either.
    """
    host = MultiHost.start(config(max_rounds=2, profiling=True), restarting=restarting)
    host.drain_until_turn(ROUTER)
    host.run.reply(host.run.expect_turn(role=ROUTER), routed())
    host.drain_until_turn(PLANNER)
    design(host, "h1")
    implement(host, candidate="def456")
    judge(host, passed=True)
    host.drain_until_turn(ROUTER)

    retained = RevisionRef.of_git_commit("def456")
    router = host.run.expect_turn(role=ROUTER)
    assert getattr(router.context, "base_revision", None) == retained
    host.run.reply(router, routed(need_profile=True, focus="decode step"))
    host.drain_until_turn(PROFILER)

    profiler = host.run.expect_turn(role=PROFILER)
    assert isinstance(profiler.request, RequestTurn)
    workspace = profiler.request.turn.workspace
    assert isinstance(workspace, WorkspaceRef)
    assert workspace.revision == retained
    assert retained in [
        item.request.workspace.base
        for item in host.run.decisions
        if isinstance(item, Wait) and isinstance(item.request, StartAttempt)
    ]
    host.run.reply(profiler, profiled())
    host.drain()
    assert_flow_closed(host)


@MODES
def test_the_router_is_told_what_the_trusted_gates_measured_of_the_base(
    *, restarting: bool
) -> None:
    """Regression: told round 1 holds no measurement, a live router launched a 27B server to
    see whether it runs, gave up after 15 s of model loading and declined to profile, although
    the trusted gates had just measured that same input end to end."""
    host = MultiHost.start(config(max_rounds=2, profiling=True), restarting=restarting)
    host.drain_until_turn(ROUTER)
    first = host.run.expect_turn(role=ROUTER)
    assert getattr(first.context, "base_value", None) == INPUT_THROUGHPUT
    host.run.reply(first, routed())
    host.drain_until_turn(PLANNER)
    design(host, "h1")
    implement(host, candidate="def456")
    judge(host, passed=True)
    host.drain_until_turn(ROUTER)

    second = host.run.expect_turn(role=ROUTER)
    assert getattr(second.context, "base_value", None) == CANDIDATE_THROUGHPUT


@MODES
def test_a_router_over_an_unmeasured_input_is_given_no_value(*, restarting: bool) -> None:
    host = MultiHost.start(
        config(profiling=True), restarting=restarting, baseline="failed-workload"
    )
    host.drain_until_turn(ROUTER)

    assert getattr(host.run.expect_turn(role=ROUTER).context, "base_value", 0.0) is None


@MODES
def test_a_router_that_fails_does_not_stop_the_round(*, restarting: bool) -> None:
    host = MultiHost.start(config(profiling=True), restarting=restarting)
    host.drain_until_turn(ROUTER)
    host.run.fail(host.run.expect_turn(role=ROUTER), FailureReason.TIMED_OUT)
    host.drain_until_turn(PLANNER)
    design(host)
    implement(host)
    judge(host, passed=True)
    host.drain()

    assert selection_of(host) == ("retained_candidate", "def456")
    assert_flow_closed(host)


@MODES
@pytest.mark.parametrize("baseline", ["failed-workload", "no-evidence", "lost"])
def test_an_input_that_cannot_be_measured_is_not_hidden(
    *, restarting: bool, baseline: Baseline
) -> None:
    host = MultiHost.start(config(), restarting=restarting, baseline=baseline)
    host.drain(limit=60)
    assert host.run.finished is not None or host.run.open


@MODES
@pytest.mark.parametrize("reason", list(CheckpointDecline))
def test_a_checkpoint_core_declines_leaves_the_round_without_a_candidate(
    *, restarting: bool, reason: CheckpointDecline
) -> None:
    host = start(restarting=restarting)
    design(host)
    host.drain_until_turn(IMPLEMENTER)
    host.run.reply(
        host.run.expect_turn(role=IMPLEMENTER), implemented(), checkpoint=Declined(reason=reason)
    )
    host.drain()

    assert selection_of(host) == ("trusted_baseline", None)
    assert_flow_closed(host)


@MODES
def test_an_implementer_that_asks_to_continue_is_given_its_own_next_step_without_a_new_design(
    *, restarting: bool
) -> None:
    host = start(config(max_rounds=2), restarting=restarting)
    design(host)
    host.drain_until_turn(IMPLEMENTER)
    host.run.reply(
        host.run.expect_turn(role=IMPLEMENTER),
        implemented("continue", next_step="try the fused kernel"),
        checkpoint=Retained(revision=RevisionRef.of_git_commit("c1")),
    )
    judge(host, passed=True)
    host.drain_until_turn(IMPLEMENTER)

    prompt = host.run.expect_turn(role=IMPLEMENTER).context
    assert getattr(prompt, "continuation_step", None) == "try the fused kernel"
    assert not [item for item in host.run.open if item.label == PLANNER]
    record = host.record
    assert record is not None
    assert [item.hypothesis_id for item in record.hypotheses] == ["h1"]


@MODES
@settings(max_examples=examples(pr=10, nightly=12), deadline=None, derandomize=True, database=None)
@given(
    judge_every=st.integers(1, 3),
    outcomes=st.lists(
        st.sampled_from(["implemented", "implementation_failed"]), min_size=3, max_size=3
    ),
)
def test_the_judge_is_asked_exactly_when_the_review_cadence_says(
    *, restarting: bool, judge_every: int, outcomes: list[str]
) -> None:
    """A round is reviewed at the cadence, in the final round, or on an implemented candidate."""
    host = start(config(max_rounds=3, judge_every=judge_every), restarting=restarting)
    for number, outcome in enumerate(outcomes, start=1):
        design(host, f"h{number}")
        host.drain_until_turn(IMPLEMENTER)
        host.run.reply(
            host.run.expect_turn(role=IMPLEMENTER),
            implemented(outcome),
            checkpoint=Retained(revision=RevisionRef.of_git_commit(f"c{number}")),
        )
        for _ in range(50):
            if any(item.label == PLANNER for item in host.run.open) or host.run.finished:
                break
            host.step(0)
    host.drain()

    expected = sum(
        number == 3 or number % judge_every == 0 or outcome == "implemented"
        for number, outcome in enumerate(outcomes, start=1)
    )
    assert len([item for item in host.run.decisions if _is_turn(item, "judge")]) == expected


@MODES
def test_a_round_whose_review_was_deferred_is_never_a_winner(*, restarting: bool) -> None:
    host = start(config(max_rounds=2, judge_every=2), restarting=restarting)
    design(host, "h1")
    host.drain_until_turn(IMPLEMENTER)
    host.run.reply(
        host.run.expect_turn(role=IMPLEMENTER),
        implemented("implementation_failed"),
        checkpoint=Retained(revision=RevisionRef.of_git_commit("c1")),
    )
    host.drain_until_turn(PLANNER)

    assert not [item for item in host.run.decisions if _is_turn(item, "judge")]
    assert _settled_ineligible(host)


@MODES
def test_each_review_is_a_fresh_conversation(*, restarting: bool) -> None:
    host = start(config(max_retries_per_round=2), restarting=restarting)
    design(host)
    implement(host)
    judge(host, passed=False)
    implement(host, candidate="c2")
    judge(host, passed=True)
    sessions = {
        item.request.turn.session.session_id
        for item in host.run.decisions
        if isinstance(item, Wait)
        and isinstance(item.request, RequestTurn)
        and _is_turn(item, "judge")
    }

    assert len(sessions) == 2


@MODES
def test_validation_runs_once_per_paid_turn_and_a_failure_repairs_the_attempt(
    *, restarting: bool
) -> None:
    host = start(
        config(max_retries_per_round=2), restarting=restarting, validating=True, validation="failed"
    )
    design(host)
    for candidate in ("c1", "c2"):
        host.drain_until_turn(IMPLEMENTER)
        host.run.reply(
            host.run.expect_turn(role=IMPLEMENTER),
            implemented(validation_recipe_artifact=RECIPE),
            checkpoint=Retained(revision=RevisionRef.of_git_commit(candidate)),
        )
        judge(host, passed=True)
    host.drain()

    validations = [
        item
        for item in host.run.decisions
        if isinstance(item, Wait)
        and isinstance(item.request, Operation)
        and isinstance(item.request.request, ValidateLocalBase)
    ]
    assert len(validations) == 2
    assert selection_of(host) == ("trusted_baseline", None)


@MODES
def test_a_run_that_cannot_validate_approves_on_the_judges_verdict(*, restarting: bool) -> None:
    host = start(restarting=restarting, validating=True, validation="errored")
    design(host)
    host.drain_until_turn(IMPLEMENTER)
    host.run.reply(
        host.run.expect_turn(role=IMPLEMENTER),
        implemented(validation_recipe_artifact=RECIPE),
        checkpoint=Retained(revision=RevisionRef.of_git_commit("def456")),
    )
    judge(host, passed=True)
    host.drain()

    assert selection_of(host) == ("retained_candidate", "def456")


@MODES
def test_a_run_without_a_profiler_never_asks_the_routing_turn(*, restarting: bool) -> None:
    host = start(config(profiling=False), restarting=restarting)
    design(host)
    implement(host)
    judge(host, passed=True)
    host.drain()

    assert not [item for item in host.run.decisions if _is_turn(item, "router")]
    assert not [item for item in host.run.decisions if _is_turn(item, "profiler")]


@MODES
def test_a_profiler_that_fails_does_not_stop_the_round(*, restarting: bool) -> None:
    host = MultiHost.start(config(profiling=True), restarting=restarting)
    host.drain_until_turn(ROUTER)
    host.run.reply(host.run.expect_turn(role=ROUTER), routed(need_profile=True, focus="gemm"))
    host.drain_until_turn(PROFILER)
    host.run.fail(host.run.expect_turn(role=PROFILER), FailureReason.TIMED_OUT)
    host.drain_until_turn(PLANNER)
    design(host)
    implement(host)
    judge(host, passed=True)
    host.drain()

    assert selection_of(host) == ("retained_candidate", "def456")


@MODES
def test_a_requested_profile_is_collected_before_the_plan_and_reaches_the_designer(
    *, restarting: bool
) -> None:
    host = MultiHost.start(config(profiling=True), restarting=restarting)
    host.drain_until_turn(ROUTER)
    host.run.reply(host.run.expect_turn(role=ROUTER), routed(need_profile=True, focus="gemm"))
    host.drain_until_turn(PROFILER)
    host.run.reply(host.run.expect_turn(role=PROFILER), profiled())
    host.drain_until_turn(PLANNER)

    prompt = host.run.expect_turn(role=PLANNER).context
    assert getattr(prompt, "last_profile", None) is not None


def _is_turn(item: object, role: str) -> bool:
    return (
        isinstance(item, Wait)
        and isinstance(item.request, RequestTurn)
        and item.request.turn.session.role_id.root.endswith(role)
    )


def _settled_ineligible(host: MultiHost) -> bool:
    settles = [
        item.request.disposition
        for item in host.run.decisions
        if isinstance(item, Wait)
        and isinstance(item.request, Withdraw)
        and isinstance(item.request.disposition, Settle)
    ]
    return bool(settles) and not settles[0].eligible


@MODES
def test_a_round_whose_implementer_waited_adopts_the_candidate_it_then_checkpointed(
    *, restarting: bool
) -> None:
    host = MultiHost.start(config(), restarting=restarting, suspending=True)
    host.drain_until_turn(PLANNER)
    design(host)
    host.drain_until_turn(IMPLEMENTER)
    host.run.resume_ready(host.run.suspend(host.run.expect_turn(role=IMPLEMENTER)))
    implement(host)
    judge(host, passed=True)
    host.drain()

    assert selection_of(host) == ("retained_candidate", "def456")
    assert_flow_closed(host)


@MODES
@settings(max_examples=examples(pr=20, nightly=80), deadline=None, database=None)
@example(throughputs=(30, 40), judged=(True, True))
@example(throughputs=(90, 60), judged=(True, True))
@example(throughputs=(90, 60), judged=(False, True))
@example(throughputs=(90, 60), judged=(False, False))
@given(
    throughputs=st.tuples(st.integers(20, 100), st.integers(20, 100)).filter(
        lambda pair: pair[0] != pair[1]
    ),
    judged=st.tuples(st.booleans(), st.booleans()),
)
def test_the_best_eligible_candidate_of_the_rounds_is_adopted_not_the_last(
    *, restarting: bool, throughputs: tuple[int, int], judged: tuple[bool, bool]
) -> None:
    """Over two rounds, the winner is the fastest candidate that the judge passed and beat the input."""
    host = start(
        config(max_rounds=2),
        restarting=restarting,
        throughputs={"c1": float(throughputs[0]), "c2": float(throughputs[1])},
    )
    design(host, "h1")
    implement(host, candidate="c1")
    judge(host, passed=judged[0])
    if judged[0]:  # a round the judge rejected goes on with the same hypothesis
        host.drain_until_turn(PLANNER)
        design(host, "h2")
    implement(host, candidate="c2")
    judge(host, passed=judged[1])
    host.drain()

    eligible = {
        name: speed
        for name, speed, passed in zip(("c1", "c2"), throughputs, judged, strict=True)
        if passed and speed > INPUT_THROUGHPUT
    }
    if eligible:
        assert selection_of(host) == ("retained_candidate", max(eligible, key=eligible.__getitem__))
    else:
        assert selection_of(host) == ("trusted_baseline", None)
    assert_flow_closed(host)
