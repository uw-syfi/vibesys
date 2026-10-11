"""Properties of the multi search that its conformance subjects do not state.

Each test runs whole searches on the production shell with scripted executors and reads only
the decisions the strategy proposed: the roles asked in each round, the settlements and the
operations.
"""

from __future__ import annotations

import json
from collections import deque
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.multi.strategy._executors import Executors
from tests.vibesys.orchestration.multi.strategy._replies import (
    implemented,
    plan_reply,
    pre_round_reply,
    profiler_reply,
    reviewed,
)
from tests.vibesys.orchestration.multi.strategy._run import FACTS, run_shell

from vibesys.metrics import MetricSpace, Objective
from vs_core.api import (
    AssessmentKind,
    LocalValidationReport,
    Operation,
    RequestTurn,
    Settle,
    StartAttempt,
    Stop,
    Withdraw,
)

if TYPE_CHECKING:
    from vs_runtime.api.testing import Run

PURE = settings(max_examples=5, deadline=None, derandomize=True, database=None)
_RECIPE = "validation/recipes.json"


def _roles(run: Run) -> list[str]:
    """The role of every turn the strategy requested, in order."""
    return [
        item.turn.session.role_id.root.removeprefix("multi-")
        for item in run.decisions
        if isinstance(item, RequestTurn)
    ]


def _settles(run: Run) -> list[Settle]:
    return [
        item.disposition
        for item in run.decisions
        if isinstance(item, Withdraw) and isinstance(item.disposition, Settle)
    ]


def _starts(run: Run) -> list[StartAttempt]:
    return [item for item in run.decisions if isinstance(item, StartAttempt)]


def _verdicts(settle: Settle) -> list[str]:
    return [
        item.verdict for item in settle.assessments if item.kind is AssessmentKind.LOCAL_VALIDATION
    ]


def _validations(run: Run) -> list[Operation]:
    return [
        item
        for item in run.decisions
        if isinstance(item, Operation) and type(item.request).__name__ == "ValidateLocal"
    ]


# -- turn-taking ----------------------------------------------------------------------------


@PURE
@given(
    judge_every=st.integers(1, 3),
    outcomes=st.lists(
        st.sampled_from(["nominated", "disproven", "inconclusive"]), min_size=3, max_size=3
    ),
)
def test_m1_the_judge_is_asked_exactly_when_the_review_cadence_says(
    judge_every: int, outcomes: list[str]
) -> None:
    """A round is reviewed at the cadence, in the final round, or on a nominated candidate."""
    executors = Executors(
        planner=deque(plan_reply(f"h{n}") for n in range(3)),
        implementer=deque(implemented(outcome) for outcome in outcomes),
        judge=deque(reviewed() for _ in range(3)),
    )
    run = run_shell(executors, max_rounds=3, judge_every=judge_every, official_eval_every=9)
    reviews = _roles(run).count("judge")
    expected = sum(
        number == 3 or number % judge_every == 0 or outcome == "nominated"
        for number, outcome in enumerate(outcomes, start=1)
    )
    assert reviews == expected


def test_a_round_whose_review_was_deferred_is_never_a_winner() -> None:
    executors = Executors(
        planner=deque([plan_reply("h1"), plan_reply("h2")]),
        implementer=deque([implemented("disproven"), implemented("disproven")]),
        judge=deque([reviewed()]),
    )
    run = run_shell(executors, max_rounds=2, judge_every=2, official_eval_every=9)
    first, second = _settles(run)
    assert not first.eligible
    assert _verdicts(first) == []
    assert _verdicts(second) == ["satisfied"]


@PURE
@given(retries=st.integers(1, 3), verdicts=st.lists(st.booleans(), min_size=1, max_size=4))
def test_paid_turns_of_a_round_never_exceed_the_retry_budget(
    retries: int, verdicts: list[bool]
) -> None:
    """Whatever the judge says, a round spends at most `max_retries_per_round` paid turns."""
    executors = Executors(
        planner=deque(plan_reply(f"h{n}") for n in range(3)),
        implementer=deque(implemented() for _ in range(12)),
        judge=deque(reviewed(passed=verdicts[n % len(verdicts)]) for n in range(12)),
    )
    run = run_shell(executors, max_rounds=2, max_retries_per_round=retries)
    implementer_turns = [
        item
        for item in run.decisions
        if isinstance(item, RequestTurn) and item.turn.charge_class == "paid"
    ]
    assert len(implementer_turns) <= 2 * retries


def test_each_review_is_a_fresh_conversation() -> None:
    executors = Executors(
        planner=deque([plan_reply("h1")]),
        implementer=deque(implemented() for _ in range(3)),
        judge=deque([reviewed(passed=False), reviewed(passed=False), reviewed()]),
    )
    run = run_shell(executors, max_rounds=1, max_retries_per_round=3)
    sessions = [
        item.turn.session.session_id.root
        for item in run.decisions
        if isinstance(item, RequestTurn) and item.turn.session.role_id.root.endswith("judge")
    ]
    assert len(sessions) == 3
    assert len(set(sessions)) == 3


# -- the review gates the round -----------------------------------------------------------


def test_a_failing_review_repairs_with_another_paid_turn_then_a_passing_one_is_measured() -> None:
    executors = Executors(
        planner=deque([plan_reply("h1")]),
        implementer=deque(implemented() for _ in range(2)),
        judge=deque([reviewed(passed=False), reviewed()]),
    )
    run = run_shell(executors, max_rounds=1, max_retries_per_round=2)
    assert _roles(run).count("implementer") == 2
    (settle,) = _settles(run)
    assert settle.eligible
    assert _verdicts(settle) == ["satisfied"]


def test_a_judge_that_never_answers_validly_ends_the_round_unreviewed_without_a_paid_retry() -> (
    None
):
    executors = Executors(
        planner=deque([plan_reply("h1")]),
        implementer=deque(implemented() for _ in range(3)),
        judge=deque(["nope"] * 8),
    )
    run = run_shell(executors, max_rounds=1, max_retries_per_round=3)
    (settle,) = _settles(run)
    assert _roles(run).count("implementer") == 1
    assert not settle.eligible
    assert settle.retention == "wip"


@pytest.mark.parametrize(("claimed", "dominated"), [(10.0, True), (500.0, False)])
def test_a_pareto_claim_dominated_by_the_archive_never_passes_review(
    claimed: float, *, dominated: bool
) -> None:
    """The live archive overrides a passing verdict on a frontier row it dominates."""
    space = MetricSpace(objectives=(Objective(name="throughput", direction="max"),))
    executors = Executors(
        planner=deque([plan_reply("h1"), plan_reply("h2")]),
        implementer=deque(
            [
                implemented(),
                implemented(
                    candidate_disposition="pareto_frontier",
                    candidate_metrics={"throughput": claimed},
                ),
            ]
        ),
        judge=deque([reviewed(), reviewed()]),
        benchmark=lambda _commit: 100.0,
    )
    run = run_shell(executors, max_rounds=2, metric_space=space)
    second = _settles(run)[1]
    assert _verdicts(second) == (["rejected"] if dominated else ["satisfied"])
    assert second.eligible is not dominated


# -- validation of the implementer's recipes --------------------------------------------------


def test_m3_validation_runs_once_per_paid_turn_and_a_failure_repairs_the_attempt() -> None:
    outcomes = deque(
        [
            LocalValidationReport(status="succeeded", passed=False, recipe="unit", error="boom"),
            LocalValidationReport(status="succeeded", passed=True),
        ]
    )
    executors = Executors(
        planner=deque([plan_reply("h1")]),
        implementer=deque(implemented(validation_recipe_artifact=_RECIPE) for _ in range(2)),
        judge=deque([reviewed(), reviewed()]),
        validation=lambda _request: outcomes.popleft(),
    )
    run = run_shell(executors, max_rounds=1, max_retries_per_round=2)
    operations = _validations(run)
    assert len(operations) == 2
    assert len({item.decision_id.root for item in operations}) == 2
    assert [item.recipe_artifact for item in executors.validations] == [_RECIPE, _RECIPE]
    (settle,) = _settles(run)
    assert settle.eligible


def test_a_run_that_cannot_validate_approves_on_the_judges_verdict() -> None:
    executors = Executors(
        planner=deque([plan_reply("h1")]),
        implementer=deque([implemented(validation_recipe_artifact=_RECIPE)]),
        judge=deque([reviewed()]),
        validation=lambda _request: LocalValidationReport(status="rejected", detail="no sandbox"),
    )
    run = run_shell(executors, max_rounds=1)
    (settle,) = _settles(run)
    assert settle.eligible


# -- the advisory profiler -----------------------------------------------------------------


@PURE
@given(
    routed=st.lists(st.sampled_from(["x", "profile", "skip"]), min_size=2, max_size=2),
    profiled=st.lists(st.sampled_from(["x", "ok"]), min_size=3, max_size=3),
)
def test_m2_an_advisory_failure_never_fails_the_round(
    routed: list[str], profiled: list[str]
) -> None:
    """A routing or profiling turn that gives garbage costs a note; the plan and the round go on."""
    executors = Executors(
        pre_round=deque(
            "x" if item == "x" else pre_round_reply(need_profile=item == "profile")
            for item in routed * 4
        ),
        profiler=deque(profiler_reply() if item == "ok" else "x" for item in profiled * 4),
        planner=deque(plan_reply(f"h{n}") for n in range(4)),
        implementer=deque(implemented() for _ in range(4)),
        judge=deque(reviewed() for _ in range(4)),
    )
    run = run_shell(
        executors,
        max_rounds=2,
        profiler_id="linux_cpu",
        profiler_support_name="linux_cpu_profiler",
        profiler_mcp_name="vibesys-linux-cpu-profiler",
    )
    assert len(_starts(run)) == 2
    final = run.decisions[-1]
    assert isinstance(final, Stop)
    assert final.result.outcome == "success"


def test_a_run_without_a_profiler_never_asks_the_routing_turn() -> None:
    executors = Executors(
        planner=deque([plan_reply("h1")]),
        implementer=deque([implemented()]),
        judge=deque([reviewed()]),
    )
    run = run_shell(executors, max_rounds=1)
    assert _roles(run) == ["orchestrator", "implementer", "judge"]


def test_a_requested_profile_is_collected_before_the_plan() -> None:
    executors = Executors(
        pre_round=deque([pre_round_reply(need_profile=True, focus="decode loop")]),
        profiler=deque([profiler_reply()]),
        planner=deque([plan_reply("h1")]),
        implementer=deque([implemented()]),
        judge=deque([reviewed()]),
    )
    run = run_shell(
        executors,
        max_rounds=1,
        profiler_id="linux_cpu",
        profiler_support_name="linux_cpu_profiler",
        profiler_mcp_name="vibesys-linux-cpu-profiler",
    )
    assert _roles(run) == ["orchestrator", "profiler", "orchestrator", "implementer", "judge"]


# -- failed gates ---------------------------------------------------------------------------


def test_m4_a_failed_official_gate_forces_the_next_review_even_for_a_continuation() -> None:
    """After a gate failure the repaired attempt is reviewed, though `continue` would defer it."""
    fail_first = deque([None, 100.0])
    executors = Executors(
        planner=deque([plan_reply("h1")]),
        implementer=deque(
            [implemented(), implemented("continue", next_step="repair the gate path")]
        ),
        judge=deque([reviewed(), reviewed()]),
        benchmark=lambda _commit: fail_first.popleft() if fail_first else 100.0,
    )
    run = run_shell(executors, max_rounds=2, max_retries_per_round=2, judge_every=1)
    assert _roles(run)[:5] == ["orchestrator", "implementer", "judge", "implementer", "judge"]


def test_a_continuation_after_a_failed_review_is_not_reviewed_again() -> None:
    executors = Executors(
        planner=deque([plan_reply("h1")]),
        implementer=deque([implemented(), implemented("continue", next_step="smaller repair")]),
        judge=deque([reviewed(passed=False), reviewed()]),
    )
    run = run_shell(executors, max_rounds=2, max_retries_per_round=2, judge_every=1)
    assert _roles(run)[:4] == ["orchestrator", "implementer", "judge", "implementer"]


# -- the designer ------------------------------------------------------------------------------


def test_an_invalid_plan_is_corrected_once_with_its_predecessor() -> None:
    executors = Executors(
        planner=deque(["not json", plan_reply("h1")]),
        implementer=deque([implemented()]),
        judge=deque([reviewed()]),
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


def test_a_revert_to_the_input_starts_the_round_on_the_input() -> None:
    executors = Executors(
        planner=deque([plan_reply("h1"), plan_reply("h2", revert_to_round=0)]),
        implementer=deque(implemented() for _ in range(4)),
        judge=deque(reviewed() for _ in range(4)),
    )
    run = run_shell(executors, max_rounds=2)
    assert _starts(run)[1].workspace.base == FACTS.baseline


def test_a_continued_hypothesis_asks_no_designer() -> None:
    executors = Executors(
        planner=deque([plan_reply("h1")]),
        implementer=deque(
            [implemented("continue", next_step="finish it"), implemented("nominated")]
        ),
        judge=deque([reviewed(), reviewed()]),
    )
    run = run_shell(executors, max_rounds=2, judge_every=1)
    assert _roles(run).count("orchestrator") == 1
    assert len(_starts(run)) == 2


def test_the_search_survives_an_implementer_whose_every_reply_is_unparsable() -> None:
    executors = Executors(
        planner=deque(plan_reply(f"h{n}") for n in range(3)),
        implementer=deque(["no"] * 12),
    )
    run = run_shell(executors, max_rounds=2)
    assert isinstance(run.decisions[-1], Stop)
    assert all(not item.eligible for item in _settles(run))


def test_the_profile_json_is_untouched_by_the_scripted_replies() -> None:
    assert json.loads(profiler_reply())["bottlenecks"] == "hot loop"


@pytest.mark.parametrize("passed", [True, False])
def test_the_retained_checkpoint_of_a_failed_round_is_work_in_progress(*, passed: bool) -> None:
    executors = Executors(
        planner=deque([plan_reply("h1")]),
        implementer=deque(implemented() for _ in range(3)),
        judge=deque(reviewed(passed=passed) for _ in range(3)),
    )
    run = run_shell(executors, max_rounds=1, max_retries_per_round=1)
    (settle,) = _settles(run)
    assert settle.eligible is passed
    if not passed:
        assert settle.retention == "wip"
