"""The serial round machine keeps a ledger that survives encoding and follows its parents."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from vibesys.hypothesis import (
    Continue,
    Finished,
    HypothesisConfig,
    NewHypothesis,
    OrchestratorPlan,
    rounds,
)
from vibesys.metrics import FrameworkBenchmarkOutcome, MetricSpace
from vibesys.orchestration.review import Verdict
from vibesys.orchestration.single.models import SingleAgentRoundResponse
from vs_sim.api.testing import examples

SPACE = MetricSpace()


def _plan(number: int) -> OrchestratorPlan:
    return OrchestratorPlan(
        hypothesis_id=f"h{number}",
        hypothesis="claim",
        task="do it",
        pass_criteria="faster",  # noqa: S106  # LW-641201 [S106]; a fixture literal, not a credential.
        reasoning="because",
    )


def _reply(*, passed: bool) -> SingleAgentRoundResponse:
    return SingleAgentRoundResponse(
        summary="did it",
        expected_behavior="faster",
        self_review="ok",
        feedback="" if passed else "fix",
        verdict=Verdict.PASS if passed else Verdict.FAIL,
        bottlenecks="",
        suggestions="",
        profile_analysis="",
    )


def _facts(
    number: int, plan: OrchestratorPlan, *, passed: bool, commit: str | None
) -> rounds.ClosingFacts:
    return rounds.ClosingFacts(
        round_number=number,
        plan=plan,
        reply=_reply(passed=passed),
        passed=passed,
        feedback=None if passed else "fix",
        official_reason="cadence" if passed else None,
        benchmark=FrameworkBenchmarkOutcome(
            metric_name="throughput", metric_value=100.0 + number, metric_direction="max"
        ),
        benchmark_metric=100.0 + number if passed else None,
        candidate_commit=commit,
        attempts=1,
        accuracy_configured=True,
        benchmark_configured=True,
        implementer=None,
        verdict_pass=passed,
    )


@settings(max_examples=examples(pr=10, nightly=25), deadline=None, derandomize=True, database=None)
@given(passes=st.lists(st.booleans(), min_size=1, max_size=4))
def test_rounds_close_in_order_and_the_ledger_round_trips(passes: list[bool]) -> None:
    config = HypothesisConfig(max_rounds=len(passes))
    ledger = rounds.decode(rounds.EncodedLedger(), SPACE)
    commits: list[str | None] = []
    for number, passed in enumerate(passes, start=1):
        decision = rounds.next_round(config, ledger, number)
        current = next((c for c in reversed(commits) if c), "base")
        if isinstance(decision, NewHypothesis):
            plan = _plan(number)
            ledger, started = rounds.start(
                config,
                ledger,
                plan,
                round_number=number,
                parent=rounds.Parent(None, current),
            )
            assert started.hypothesis.parent_commit == current
        else:
            # A reviewed failure keeps its hypothesis active for the next round.
            assert isinstance(decision, Continue)
            plan = decision.hypothesis.plan
        commit = f"c{number}" if passed else None
        commits.append(commit)
        ledger = rounds.close_round(
            config, ledger, _facts(number, plan, passed=passed, commit=commit)
        )
        again = rounds.decode(rounds.encode(ledger), SPACE)
        assert again.search == ledger.search
        assert again.carry == ledger.carry
        assert [r.round_number for r in ledger.records] == list(range(1, number + 1))
    assert isinstance(rounds.next_round(config, ledger, len(passes) + 1), Finished)


def test_a_failed_attempt_keeps_its_feedback_and_a_gate_failure_marks_revalidation() -> None:
    config = HypothesisConfig(max_rounds=2)
    ledger = rounds.decode(rounds.EncodedLedger(), SPACE)
    ledger, _ = rounds.start(
        config, ledger, _plan(1), round_number=1, parent=rounds.Parent(None, "base")
    )
    ledger = rounds.note_attempt_failure(
        config, ledger, "benchmark failed", rounds.GateFailure("c1", accuracy_passed=True)
    )
    active = ledger.search.active_hypothesis
    assert active is not None
    assert active.feedback == "benchmark failed"
    assert active.gate_revalidation_pending
    assert active.gate_candidate_commit == "c1"
