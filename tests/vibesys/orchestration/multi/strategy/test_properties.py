"""Properties of the multi search that its conformance subjects do not state.

Each test simulates whole searches on the production shell against a scripted world and reads
only the decisions the strategy proposed: the roles asked in each round, the settlements and the
operations.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.multi.strategy.world import (
    FACTS,
    PROFILER,
    RECIPE,
    MultiWorld,
    implemented,
    plan_reply,
    pre_round_reply,
    profiler_reply,
    reviewed,
)

from vibesys.hypothesis import rounds
from vibesys.metrics import MetricSpace, Objective
from vibesys.orchestration.multi.strategy.api import MultiStrategyState, ValidateLocal
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
from vs_dst.api import simulate

if TYPE_CHECKING:
    from vs_dst.api import SimulatedRun

PURE = settings(max_examples=5, deadline=None, derandomize=True, database=None)


def _run(world: MultiWorld) -> SimulatedRun:
    """The world's run, which must have ended in its terminal state without a halt."""
    run = simulate(world)
    assert run.finished
    assert run.halted is None
    assert not run.liveness_violations
    return run


def _roles(run: SimulatedRun) -> list[str]:
    """The role of every turn the strategy requested, in order."""
    return [
        item.turn.session.role_id.root.removeprefix("multi-")
        for item in run.decisions
        if isinstance(item, RequestTurn)
    ]


def _settles(run: SimulatedRun) -> list[Settle]:
    return [
        item.disposition
        for item in run.decisions
        if isinstance(item, Withdraw) and isinstance(item.disposition, Settle)
    ]


def _starts(run: SimulatedRun) -> list[StartAttempt]:
    return [item for item in run.decisions if isinstance(item, StartAttempt)]


def _verdicts(settle: Settle) -> list[str]:
    return [
        item.verdict for item in settle.assessments if item.kind is AssessmentKind.LOCAL_VALIDATION
    ]


def _validations(run: SimulatedRun) -> list[tuple[Operation, ValidateLocal]]:
    return [
        (item, item.request)
        for item in run.decisions
        if isinstance(item, Operation) and isinstance(item.request, ValidateLocal)
    ]


def test_unmeasured_frontier_claim_is_evidence_but_not_framework_retention() -> None:
    claim = implemented(
        "disproven",
        candidate_disposition="pareto_frontier",
        candidate_metrics={"throughput": 75.0},
        candidate_evaluation_artifact="agent-profile.json",
        candidate_retention_reason="agent reports a tradeoff",
    )
    world = MultiWorld(
        plans=[plan_reply("h1"), plan_reply("h2"), plan_reply("h3")],
        implementations=[claim, implemented(), implemented()],
        max_rounds=3,
        official_eval_every=2,
    )

    run = _run(world)
    first = _settles(run)[0]
    state = MultiStrategyState.model_validate(run.state)
    ledger = rounds.decode(rounds.EncodedLedger(state.search_json, state.carry_json))
    first_record = ledger.records[0]

    assert (first.eligible, first.retention) == (False, "candidate")
    assert first.candidate is not None
    assert _starts(run)[1].workspace.base == first.candidate
    assert first_record.hypothesis_outcome == "disproven"
    assert first_record.candidate_disposition == "pareto_frontier"
    assert first_record.candidate_retention_reason == "agent reports a tradeoff"
    assert first_record.candidate_retained is False


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
    world = MultiWorld(
        implementations=[implemented(outcome) for outcome in outcomes],
        max_rounds=3,
        judge_every=judge_every,
        official_eval_every=9,
    )

    reviews = _roles(_run(world)).count("judge")

    expected = sum(
        number == 3 or number % judge_every == 0 or outcome == "nominated"
        for number, outcome in enumerate(outcomes, start=1)
    )
    assert reviews == expected


def test_a_round_whose_review_was_deferred_is_never_a_winner() -> None:
    world = MultiWorld(
        implementations=[implemented("disproven"), implemented("disproven")],
        reviews=[reviewed()],
        max_rounds=2,
        judge_every=2,
        official_eval_every=9,
    )

    first, second = _settles(_run(world))

    assert not first.eligible
    assert _verdicts(first) == []
    assert _verdicts(second) == ["satisfied"]


@PURE
@given(retries=st.integers(1, 3), verdicts=st.lists(st.booleans(), min_size=1, max_size=4))
def test_paid_turns_of_a_round_never_exceed_the_retry_budget(
    retries: int, verdicts: list[bool]
) -> None:
    """Whatever the judge says, a round spends at most `max_retries_per_round` paid turns."""
    run = _run(MultiWorld(passes=verdicts, max_rounds=2, max_retries_per_round=retries))

    paid = [
        item
        for item in run.decisions
        if isinstance(item, RequestTurn) and item.turn.charge_class == "paid"
    ]
    assert len(paid) <= 2 * retries


def test_each_review_is_a_fresh_conversation() -> None:
    world = MultiWorld(
        reviews=[reviewed(passed=False), reviewed(passed=False), reviewed()],
        max_rounds=1,
        max_retries_per_round=3,
    )

    run = _run(world)

    sessions = [
        item.turn.session.session_id.root
        for item in run.decisions
        if isinstance(item, RequestTurn) and item.turn.session.role_id.root.endswith("judge")
    ]
    assert len(sessions) == 3
    assert len(set(sessions)) == 3


# -- the review gates the round -----------------------------------------------------------


def test_a_failing_review_repairs_with_another_paid_turn_then_a_passing_one_is_measured() -> None:
    world = MultiWorld(
        reviews=[reviewed(passed=False), reviewed()], max_rounds=1, max_retries_per_round=2
    )

    run = _run(world)

    assert _roles(run).count("implementer") == 2
    (settle,) = _settles(run)
    assert settle.eligible
    assert _verdicts(settle) == ["satisfied"]


def test_a_judge_that_never_answers_validly_ends_the_round_unreviewed_without_a_paid_retry() -> (
    None
):
    world = MultiWorld(reviews=["nope"] * 8, max_rounds=1, max_retries_per_round=3)

    run = _run(world)

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
    claim = implemented(
        candidate_disposition="pareto_frontier", candidate_metrics={"throughput": claimed}
    )
    world = MultiWorld(
        plans=[plan_reply("h1"), plan_reply("h2")],
        implementations=[implemented(), claim],
        max_rounds=2,
        metric_space=space,
    )

    second = _settles(_run(world))[1]

    assert _verdicts(second) == (["rejected"] if dominated else ["satisfied"])
    assert second.eligible is not dominated


# -- validation of the implementer's recipes --------------------------------------------------


def test_m3_validation_runs_once_per_paid_turn_and_a_failure_repairs_the_attempt() -> None:
    world = MultiWorld(
        implementations=[implemented(validation_recipe_artifact=RECIPE) for _ in range(2)],
        validations=[
            LocalValidationReport(status="succeeded", passed=False, recipe="unit", error="boom"),
            LocalValidationReport(status="succeeded", passed=True),
        ],
        max_rounds=1,
        max_retries_per_round=2,
    )

    run = _run(world)

    operations = _validations(run)
    assert len(operations) == 2
    assert len({decision.decision_id.root for decision, _ in operations}) == 2
    assert [request.recipe_artifact for _, request in operations] == [RECIPE, RECIPE]
    (settle,) = _settles(run)
    assert settle.eligible


def test_a_run_that_cannot_validate_approves_on_the_judges_verdict() -> None:
    world = MultiWorld(
        implementations=[implemented(validation_recipe_artifact=RECIPE)],
        validations=[LocalValidationReport(status="rejected", detail="no sandbox")],
        max_rounds=1,
    )

    (settle,) = _settles(_run(world))

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
    world = MultiWorld(
        pre_rounds=[
            "x" if item == "x" else pre_round_reply(need_profile=item == "profile")
            for item in routed * 4
        ],
        profiles=[profiler_reply() if item == "ok" else "x" for item in profiled * 4],
        max_rounds=2,
        **PROFILER,
    )

    run = _run(world)

    assert len(_starts(run)) == 2
    final = run.decisions[-1]
    assert isinstance(final, Stop)
    assert final.result.outcome == "success"


def test_a_run_without_a_profiler_never_asks_the_routing_turn() -> None:
    run = _run(MultiWorld(max_rounds=1))

    assert _roles(run) == ["orchestrator", "implementer", "judge"]


def test_a_requested_profile_is_collected_before_the_plan() -> None:
    world = MultiWorld(
        pre_rounds=[pre_round_reply(need_profile=True, focus="decode loop")],
        max_rounds=1,
        **PROFILER,
    )

    run = _run(world)

    assert _roles(run) == ["orchestrator", "profiler", "orchestrator", "implementer", "judge"]


# -- failed gates ---------------------------------------------------------------------------


def test_m4_a_failed_official_gate_forces_the_next_review_even_for_a_continuation() -> None:
    """After a gate failure the repaired attempt is reviewed, though `continue` would defer it."""
    world = MultiWorld(
        implementations=[
            implemented(),
            implemented("continue", next_step="repair the gate path"),
        ],
        benchmark_failures=1,
        max_rounds=2,
        max_retries_per_round=2,
        judge_every=1,
    )

    run = _run(world)

    assert _roles(run)[:5] == ["orchestrator", "implementer", "judge", "implementer", "judge"]


def test_a_continuation_after_a_failed_review_is_not_reviewed_again() -> None:
    world = MultiWorld(
        implementations=[implemented(), implemented("continue", next_step="smaller repair")],
        reviews=[reviewed(passed=False), reviewed()],
        max_rounds=2,
        max_retries_per_round=2,
        judge_every=1,
    )

    run = _run(world)

    assert _roles(run)[:4] == ["orchestrator", "implementer", "judge", "implementer"]


# -- the designer ------------------------------------------------------------------------------


def test_an_invalid_plan_is_corrected_once_with_its_predecessor() -> None:
    run = _run(MultiWorld(plans=["not json", plan_reply("h1")], max_rounds=1))

    designer = [
        item
        for item in run.decisions
        if isinstance(item, RequestTurn) and item.turn.session.role_id.root.endswith("orchestrator")
    ]
    assert [item.turn.charge_class for item in designer] == ["free", "correction"]
    assert designer[1].turn.predecessor is not None
    assert len(_starts(run)) == 1


def test_a_revert_to_the_input_starts_the_round_on_the_input() -> None:
    world = MultiWorld(plans=[plan_reply("h1"), plan_reply("h2", revert_to_round=0)], max_rounds=2)

    run = _run(world)

    assert _starts(run)[1].workspace.base == FACTS.baseline


def test_a_continued_hypothesis_asks_no_designer() -> None:
    world = MultiWorld(
        plans=[plan_reply("h1")],
        implementations=[implemented("continue", next_step="finish it"), implemented("nominated")],
        max_rounds=2,
        judge_every=1,
    )

    run = _run(world)

    assert _roles(run).count("orchestrator") == 1
    assert len(_starts(run)) == 2


def test_the_search_survives_an_implementer_whose_every_reply_is_unparsable() -> None:
    run = _run(MultiWorld(implementations=["no"] * 12, max_rounds=2))

    assert isinstance(run.decisions[-1], Stop)
    assert all(not item.eligible for item in _settles(run))


def test_the_profile_json_is_untouched_by_the_scripted_replies() -> None:
    assert json.loads(profiler_reply())["bottlenecks"] == "hot loop"


@pytest.mark.parametrize("passed", [True, False])
def test_the_retained_checkpoint_of_a_failed_round_is_work_in_progress(*, passed: bool) -> None:
    world = MultiWorld(passes=[passed], max_rounds=1, max_retries_per_round=1)

    (settle,) = _settles(_run(world))

    assert settle.eligible is passed
    if not passed:
        assert settle.retention == "wip"


@PURE
@given(exit_code=st.integers(1, 255), output=st.text(max_size=40))
def test_a_failed_attribution_command_names_the_command_and_where_it_runs(
    exit_code: int, output: str
) -> None:
    """The run ends at the baseline attribution, saying what ran, where, and why it failed."""
    world = MultiWorld(
        attribution_output=output,
        attribution_exit_code=exit_code,
        max_rounds=2,
        attribution={"command": ("bash", "attribute.sh"), "timeout_seconds": 10},
    )

    run = _run(world)

    assert isinstance(run.state, MultiStrategyState)
    error = run.state.design.last_error
    assert error is not None
    assert "`bash attribute.sh`" in error
    assert f"exited with code {exit_code}" in error
    assert "command environment" in error
    assert ("Exit code 127 means" in error) == (exit_code == 127)
