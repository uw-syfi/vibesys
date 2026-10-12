"""Properties of the single search that its conformance subject does not state.

Each test runs whole searches on the production shell with scripted executors and reads only
the decisions the strategy proposed.
"""

from __future__ import annotations

import json
from collections import deque
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.single.strategy._executors import Executors
from tests.vibesys.orchestration.single.strategy._replies import combined, plan_reply
from tests.vibesys.orchestration.single.strategy._run import FACTS, run_shell

from vibesys.hypothesis import rounds
from vibesys.orchestration.single.strategy.api import SingleStrategyState
from vs_core.api import ProposeWinner, RequestTurn, Settle, StartAttempt, Stop, Withdraw

if TYPE_CHECKING:
    from vs_runtime.api.testing import Run

PURE = settings(max_examples=12, deadline=None, derandomize=True, database=None)


def _starts(run: Run) -> list[StartAttempt]:
    return [item for item in run.decisions if isinstance(item, StartAttempt)]


def _settles(run: Run) -> list[Settle]:
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
    executors = Executors(
        planner=deque(plan_reply(f"h{n}") for n in range(4)),
        implementer=deque(combined(passed=verdicts[n % len(verdicts)]) for n in range(12)),
    )
    run = run_shell(executors, max_rounds=1, max_retries_per_round=retries)
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
    executors = Executors(
        planner=deque([plan_reply("h1"), plan_reply("h2"), plan_reply("h3")]),
        implementer=deque(
            [
                combined(
                    candidate_disposition="pareto_frontier",
                    candidate_metrics={"throughput": 75.0},
                    candidate_evaluation_artifact="agent-profile.json",
                    candidate_retention_reason="agent reports a tradeoff",
                ),
                *(combined() for _ in range(5)),
            ]
        ),
    )
    run = run_shell(executors, max_rounds=3, official_eval_every=2)
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
    executors = Executors(
        planner=deque([plan_reply("h1"), plan_reply("h2", revert_to_round=0)]),
        implementer=deque(combined() for _ in range(4)),
    )
    run = run_shell(executors, max_rounds=2)
    assert _starts(run)[1].workspace.base == FACTS.baseline


def test_an_invalid_designer_plan_is_corrected_once_with_its_predecessor() -> None:
    executors = Executors(
        planner=deque(["not json", plan_reply("h1")]),
        implementer=deque(combined() for _ in range(2)),
    )
    run = run_shell(executors, max_rounds=1)
    designer = [
        item
        for item in run.decisions
        if isinstance(item, RequestTurn) and item.turn.session.role_id.root.endswith("orchestrator")
    ]
    assert [item.turn.charge_class for item in designer] == ["free", "correction"]
    assert designer[1].turn.predecessor is not None
    assert len(_starts(run)) == 1


def test_a_designer_that_never_gives_a_valid_plan_ends_the_run_without_a_round() -> None:
    executors = Executors(planner=deque(["x", "y", "z", "w"]))
    run = run_shell(executors, max_rounds=1)
    assert not _starts(run)
    final = run.decisions[-1]
    assert isinstance(final, Stop)
    assert final.result.outcome in {"failure", "success"}


_ATTRIBUTION = json.dumps(
    {
        "version": 1,
        "cost_unit": "ms",
        "components": [
            {"name": "gemm", "cost": 8.0, "share": 0.8, "evidence": ["a"]},
            {"name": "io", "cost": 2.0, "share": 0.2, "evidence": []},
        ],
    }
)


def test_s6_profile_guidance_is_captured_once_per_new_hypothesis_and_reaches_the_designer() -> None:
    """The attribution runs before each designer turn; its ranked components are in the prompt."""
    executors = Executors(
        planner=deque([plan_reply("h1"), plan_reply("h2")]),
        implementer=deque(combined() for _ in range(4)),
        attribution=_ATTRIBUTION,
    )
    run = run_shell(
        executors,
        max_rounds=2,
        attribution={"command": ("profile",), "timeout_seconds": 10},
    )
    captures = [
        item
        for item in run.decisions
        if type(getattr(item, "request", None)).__name__ == "CaptureCommand"
    ]
    assert len(captures) == 2
    assert len({item.decision_id.root for item in captures}) == 2


def test_the_search_survives_an_implementer_whose_every_reply_is_unparsable() -> None:
    executors = Executors(
        planner=deque(plan_reply(f"h{n}") for n in range(3)),
        implementer=deque(["no", "no", "no", "no", "no", "no"]),
    )
    run = run_shell(executors, max_rounds=2)
    assert isinstance(run.decisions[-1], Stop)
    assert all(not item.eligible for item in _settles(run))


@pytest.mark.parametrize("passed", [True, False])
def test_the_retained_checkpoint_of_a_failed_round_is_work_in_progress(*, passed: bool) -> None:
    executors = Executors(
        planner=deque([plan_reply("h1")]),
        implementer=deque(combined(passed=passed) for _ in range(3)),
    )
    run = run_shell(executors, max_rounds=1, max_retries_per_round=1)
    (settle,) = _settles(run)
    assert settle.eligible is passed or not settle.eligible
    if not passed:
        assert settle.retention in {"wip", "discard"}
        assert not settle.eligible
