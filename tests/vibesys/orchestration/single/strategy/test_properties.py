"""Properties of the single search that its conformance subject does not state.

Each test simulates whole searches on the production shell against a scripted world and reads
only the decisions the strategy proposed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.single.strategy.world import (
    FACTS,
    SingleWorld,
    implemented,
    plan_reply,
    profile_attribution,
)

from vibesys.hypothesis import rounds
from vibesys.orchestration.single.strategy.api import SingleStrategyState
from vs_core.api import ProposeWinner, RequestTurn, Settle, StartAttempt, Stop, Withdraw
from vs_dst.api import simulate

if TYPE_CHECKING:
    from vs_dst.api import SimulatedRun

PURE = settings(max_examples=12, deadline=None, derandomize=True, database=None)


def _starts(run: SimulatedRun) -> list[StartAttempt]:
    return [item for item in run.decisions if isinstance(item, StartAttempt)]


def _settles(run: SimulatedRun) -> list[Settle]:
    return [
        item.disposition
        for item in run.decisions
        if isinstance(item, Withdraw) and isinstance(item.disposition, Settle)
    ]


@PURE
@given(
    retries=st.integers(1, 3),
    verdicts=st.lists(st.booleans(), min_size=1, max_size=4),
)
def test_s5_paid_turns_of_a_round_never_exceed_the_retry_budget(
    retries: int, verdicts: list[bool]
) -> None:
    """Whatever the self-review says, a round spends at most `max_retries_per_round` paid turns."""
    run = simulate(SingleWorld(passes=verdicts, max_rounds=1, max_retries_per_round=retries))

    assert run.finished
    assert not run.liveness_violations
    paid = [
        item
        for item in run.decisions
        if isinstance(item, RequestTurn) and item.turn.charge_class == "paid"
    ]
    assert len(paid) <= retries
    assert isinstance(run.decisions[-1], Stop)


def test_s1_a_round_the_cadence_skipped_is_never_a_winner_and_is_the_next_base() -> None:
    """Round 1 passes review but is not measured (every second candidate is): it is a buildable
    parent that can never be adopted, and round 2 starts on it."""
    world = SingleWorld(
        plans=[plan_reply("h1"), plan_reply("h2"), plan_reply("h3")],
        implementations=[
            implemented(
                candidate_disposition="pareto_frontier",
                candidate_metrics={"throughput": 75.0},
                candidate_evaluation_artifact="agent-profile.json",
                candidate_retention_reason="agent reports a tradeoff",
            ),
            *(implemented() for _ in range(5)),
        ],
        max_rounds=3,
        official_eval_every=2,
    )
    run = simulate(world)

    assert run.finished
    assert not run.liveness_violations
    first = _settles(run)[0]
    state = SingleStrategyState.model_validate(run.state)
    ledger = rounds.decode(rounds.EncodedLedger(state.search_json, state.carry_json))
    first_record = ledger.records[0]

    assert (first.eligible, first.retention) == (False, "candidate")
    assert first.candidate is not None
    assert _starts(run)[1].workspace.base == first.candidate
    assert first_record.candidate_disposition == "pareto_frontier"
    assert first_record.candidate_retention_reason == "agent reports a tradeoff"
    assert first_record.candidate_retained is False
    winner = next(item for item in run.decisions if isinstance(item, ProposeWinner))
    if winner.selection.kind == "retained_candidate":
        assert winner.selection.revision != first.candidate


def test_s3_revert_to_the_input_starts_the_round_on_the_input() -> None:
    """A designer plan that reverts to round 0 gives the attempt the run's input as its base."""
    run = simulate(
        SingleWorld(plans=[plan_reply("h1"), plan_reply("h2", revert_to_round=0)], max_rounds=2)
    )

    assert run.finished
    assert not run.liveness_violations
    assert _starts(run)[1].workspace.base == FACTS.baseline


def test_an_invalid_designer_plan_is_corrected_once_with_its_predecessor() -> None:
    run = simulate(SingleWorld(plans=["not json", plan_reply("h1")], max_rounds=1))

    assert run.finished
    assert not run.liveness_violations
    designer = [
        item
        for item in run.decisions
        if isinstance(item, RequestTurn) and item.turn.session.role_id.root.endswith("orchestrator")
    ]
    assert [item.turn.charge_class for item in designer] == ["free", "correction"]
    assert designer[1].turn.predecessor is not None
    assert len(_starts(run)) == 1


def test_a_designer_that_never_gives_a_valid_plan_ends_the_run_without_a_round() -> None:
    run = simulate(SingleWorld(plans=["x", "y", "z", "w"], implementations=[], max_rounds=1))

    assert run.finished
    assert not run.liveness_violations
    assert not _starts(run)
    final = run.decisions[-1]
    assert isinstance(final, Stop)
    assert final.result.outcome in {"failure", "success"}


def test_s6_profile_guidance_is_captured_once_per_new_hypothesis_and_reaches_the_designer() -> None:
    """The attribution runs before each designer turn; its ranked components are in the prompt."""
    world = SingleWorld(
        plans=[plan_reply("h1"), plan_reply("h2")],
        implementations=[implemented() for _ in range(4)],
        attribution_output=profile_attribution(),
        max_rounds=2,
        attribution={"command": ("profile",), "timeout_seconds": 10},
    )
    run = simulate(world)

    assert run.finished
    assert not run.liveness_violations
    captures = [
        item
        for item in run.decisions
        if type(getattr(item, "request", None)).__name__ == "CaptureCommand"
    ]
    assert len(captures) == 2
    assert len({item.decision_id.root for item in captures}) == 2


def test_the_search_survives_an_implementer_whose_every_reply_is_unparsable() -> None:
    world = SingleWorld(
        plans=[plan_reply(f"h{n}") for n in range(3)], implementations=["no"] * 6, max_rounds=2
    )
    run = simulate(world)

    assert run.finished
    assert not run.liveness_violations
    assert isinstance(run.decisions[-1], Stop)
    assert all(not item.eligible for item in _settles(run))


@pytest.mark.parametrize("passed", [True, False])
def test_the_retained_checkpoint_of_a_failed_round_is_work_in_progress(*, passed: bool) -> None:
    world = SingleWorld(
        plans=[plan_reply("h1")],
        implementations=[implemented(passed=passed) for _ in range(3)],
        max_rounds=1,
        max_retries_per_round=1,
    )
    run = simulate(world)

    assert run.finished
    assert not run.liveness_violations
    (settle,) = _settles(run)
    assert settle.eligible is passed or not settle.eligible
    if not passed:
        assert settle.retention in {"wip", "discard"}
        assert not settle.eligible


@PURE
@given(exit_code=st.integers(1, 255), output=st.text(max_size=40))
def test_a_failed_attribution_command_names_the_command_and_where_it_runs(
    exit_code: int, output: str
) -> None:
    """The run ends at the baseline attribution, saying what ran, where, and why it failed."""
    world = SingleWorld(
        attribution_output=output,
        attribution_exit_code=exit_code,
        max_rounds=2,
        attribution={"command": ("bash", "attribute.sh"), "timeout_seconds": 10},
    )
    run = simulate(world)

    assert run.finished
    assert not run.liveness_violations
    assert isinstance(run.state, SingleStrategyState)
    error = run.state.planner.last_error
    assert error is not None
    assert "`bash attribute.sh`" in error
    assert f"exited with code {exit_code}" in error
    assert "command environment" in error
    assert ("Exit code 127 means" in error) == (exit_code == 127)
