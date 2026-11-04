"""The single flow told as a story, by hand: no core, no clock, no event loop.

Each story plays the host of `single`: it answers the waits the search opens in the order the story
chooses and reads what the search decided. Every story runs live and again with a restart after
every step: the decisions must not change. What core guarantees about these waits is core's own
tests; what is asserted here is only what the search decided.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Unpack

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.single._host import (
    IMPLEMENTER,
    PLANNER,
    SingleHost,
    config,
    implemented,
)
from tests.vibesys.search_faults import assert_flow_closed
from tests.vibesys.serial_search._host import (
    INPUT_THROUGHPUT,
    Baseline,
    World,
)
from tests.vibesys.serial_search._replies import plan_reply
from tests.vibesys.vibesys_flows import across_restarts

from vibesys.serial_search.api import PlanPrompt
from vs_core.api import (
    CheckpointDecline,
    Declined,
    FailureReason,
    Finish,
    Retained,
    RevisionRef,
    StartAttempt,
)
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from vibesys.orchestration.single.search.api import SingleConfig

MODES = across_restarts
CANDIDATE = RevisionRef.of_git_commit("def456")


def start(
    selected: SingleConfig | None = None, *, restarting: bool, **world: Unpack[World]
) -> SingleHost:
    """Run the input's measurement up to the first design turn."""
    host = SingleHost.start(
        config() if selected is None else selected, restarting=restarting, **world
    )
    host.drain_until_turn(PLANNER)
    return host


def design(host: SingleHost, identifier: str = "h1") -> None:
    host.run.reply_raw(host.run.expect_turn(role=PLANNER), plan_reply(identifier))


def implement(host: SingleHost, *, passed: bool = True, candidate: str = "def456") -> None:
    host.run.reply(
        host.run.expect_turn(role=IMPLEMENTER),
        implemented(passed=passed),
        checkpoint=Retained(revision=RevisionRef.of_git_commit(candidate)),
    )


def selection_of(host: SingleHost) -> tuple[str, str | None]:
    finish = host.run.finished
    assert isinstance(finish, Finish)
    chosen = finish.result.selection
    assert chosen is not None, finish.result
    commit = chosen.revision.git_commit if chosen.kind == "retained_candidate" else None
    return chosen.kind, commit


@MODES
def test_one_hypothesis_from_design_to_the_adoption_of_its_candidate(*, restarting: bool) -> None:
    host = start(restarting=restarting)
    design(host)
    host.drain_until_turn(IMPLEMENTER)
    implement(host)
    host.drain()

    assert selection_of(host) == ("retained_candidate", "def456")
    record = host.record
    assert record is not None
    assert [item.hypothesis_id for item in record.hypotheses] == ["h1"]
    assert [row.passed for row in record.hypotheses[0].rounds] == [True]
    assert_flow_closed(host)


@MODES
def test_a_self_review_that_fails_gets_the_implementer_another_turn_in_the_same_workspace(
    *, restarting: bool
) -> None:
    host = start(config(max_retries_per_round=2), restarting=restarting)
    design(host)
    host.drain_until_turn(IMPLEMENTER)
    implement(host, passed=False)
    host.drain_until_turn(IMPLEMENTER)
    implement(host, candidate="c2")
    host.drain()

    assert len(host.opened) == 1
    assert selection_of(host) == ("retained_candidate", "c2")
    assert_flow_closed(host)


@MODES
def test_an_invalid_design_is_corrected_once_by_a_turn_that_names_the_problem(
    *, restarting: bool
) -> None:
    host = start(restarting=restarting)
    host.run.reply_raw(host.run.expect_turn(role=PLANNER), "not json")
    correction = host.run.expect_turn(role=PLANNER)
    assert getattr(correction.context, "errors", ())
    host.run.reply_raw(correction, plan_reply("h1"))
    host.drain_until_turn(IMPLEMENTER)


@MODES
def test_an_implementer_that_times_out_ends_the_round_and_nothing_is_adopted(
    *, restarting: bool
) -> None:
    host = start(config(max_retries_per_round=1), restarting=restarting)
    design(host)
    host.drain_until_turn(IMPLEMENTER)
    host.run.fail(host.run.expect_turn(role=IMPLEMENTER), FailureReason.TIMED_OUT)
    host.drain()

    assert selection_of(host) == ("trusted_baseline", None)
    assert_flow_closed(host)


@MODES
def test_a_start_core_refuses_is_a_failed_round_not_a_crash(*, restarting: bool) -> None:
    host = start(restarting=restarting)
    host.refusals["start"] = 1
    design(host)
    for asked in host.open(StartAttempt):
        host.answer(asked)
    host.drain()

    assert host.refused_starts == 1
    assert selection_of(host) == ("trusted_baseline", None)
    assert_flow_closed(host)


@MODES
@pytest.mark.parametrize("baseline", ["failed-workload", "no-evidence", "lost"])
def test_an_input_that_cannot_be_measured_is_not_hidden(
    *, restarting: bool, baseline: Baseline
) -> None:
    host = SingleHost.start(config(), restarting=restarting, baseline=baseline)
    host.drain(limit=60)
    assert host.run.finished is not None or host.run.open


@MODES
@pytest.mark.parametrize("reason", list(CheckpointDecline))
def test_a_checkpoint_core_declines_leaves_the_round_without_a_candidate(
    *, restarting: bool, reason: CheckpointDecline
) -> None:
    host = start(config(max_retries_per_round=1), restarting=restarting)
    design(host)
    host.drain_until_turn(IMPLEMENTER)
    host.run.reply(
        host.run.expect_turn(role=IMPLEMENTER), implemented(), checkpoint=Declined(reason=reason)
    )
    host.drain()

    assert selection_of(host) == ("trusted_baseline", None)
    assert_flow_closed(host)


@MODES
@settings(max_examples=examples(pr=20, nightly=80), deadline=None, database=None)
@example(throughputs=(30, 40), reviewed=(True, True))
@example(throughputs=(90, 60), reviewed=(True, True))
@example(throughputs=(90, 60), reviewed=(False, True))
@example(throughputs=(90, 60), reviewed=(False, False))
@given(
    throughputs=st.tuples(st.integers(20, 100), st.integers(20, 100)).filter(
        lambda pair: pair[0] != pair[1]
    ),
    reviewed=st.tuples(st.booleans(), st.booleans()),
)
def test_the_best_eligible_candidate_of_the_rounds_is_adopted_not_the_last(
    *, restarting: bool, throughputs: tuple[int, int], reviewed: tuple[bool, bool]
) -> None:
    """Over two rounds, the winner is the fastest candidate that passed review and beat the input."""
    host = start(
        config(max_rounds=2),
        restarting=restarting,
        throughputs={"c1": float(throughputs[0]), "c2": float(throughputs[1])},
    )
    design(host, "h1")
    host.drain_until_turn(IMPLEMENTER)
    implement(host, passed=reviewed[0], candidate="c1")
    host.drain_until_turn(PLANNER if reviewed[0] else IMPLEMENTER)
    if reviewed[0]:  # a round whose review failed goes on with the same hypothesis
        design(host, "h2")
        host.drain_until_turn(IMPLEMENTER)
    implement(host, passed=reviewed[1], candidate="c2")
    host.drain()

    eligible = {
        name: speed
        for name, speed, passed in zip(("c1", "c2"), throughputs, reviewed, strict=True)
        if passed and speed > INPUT_THROUGHPUT
    }
    if eligible:
        assert selection_of(host) == ("retained_candidate", max(eligible, key=eligible.__getitem__))
    else:
        assert selection_of(host) == ("trusted_baseline", None)
    assert_flow_closed(host)


@MODES
@settings(max_examples=examples(pr=4, nightly=20), deadline=None, derandomize=True, database=None)
@given(noise=st.floats(0.001, 0.02, allow_nan=False))
def test_a_designer_after_a_measured_round_is_shown_that_comparisons_noise(
    *, restarting: bool, noise: float
) -> None:
    host = start(config(max_rounds=2), restarting=restarting, comparisons={"def456": (1.2, noise)})
    design(host)
    host.drain_until_turn(IMPLEMENTER)
    implement(host)
    host.drain_until_turn(PLANNER)
    context = host.run.expect_turn(role=PLANNER).context

    assert isinstance(context, PlanPrompt)
    assert context.measurement_noise is not None
    assert context.measurement_noise.noise == noise
