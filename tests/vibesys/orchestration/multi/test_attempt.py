"""What each reply, verdict, validation and gate result means for one multi attempt, as plain data.

`MultiAttempt` is the pure bookkeeping of a round: it decides, from what the turns established, whether
to ask the implementer again, to review, to validate, to run the trusted gates or to settle. The
rules are stated over generated replies and results; the flow that awaits is told by the stories.
One rule is multi's own and is held here: a candidate is eligible only when an independent
review passed it, where dynamic reads a missing review as a pass.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.multi._host import config, implemented, reviewed
from tests.vibesys.serial_search._host import TERMS
from tests.vibesys.serial_search._replies import plan_reply

from vibesys.candidates.api import NotConfigured
from vibesys.hypothesis import JudgeReviewed, OrchestratorPlan, rounds
from vibesys.metrics import AcceptedReading
from vibesys.orchestration.multi.search.api import (
    ArchiveConflictView,
    ConflictMetric,
    Dominator,
    MultiAttempt,
    MultiConfig,
    Next,
)
from vibesys.serial_search.api import Gated, NotMeasured, Unreadable
from vs_core.api import (
    EvidenceId,
    EvidenceKind,
    InvocationId,
    InvocationRef,
    LocalValidationReport,
    RequestId,
    RevisionRef,
    SessionId,
)
from vs_sim.api.testing import examples

BASE = RevisionRef.of_git_commit("base")
CANDIDATE = RevisionRef.of_git_commit("candidate")
JUDGE_TURN = InvocationRef(
    session_id=SessionId(root="judge"), invocation_id=InvocationId(root="turn"), generation=0
)


def attempt(*, validation_offered: bool = False, **changes: object) -> MultiAttempt:
    given = changes.pop("config", None)
    assert given is None or isinstance(given, MultiConfig)
    selected = config() if given is None else given
    plan = OrchestratorPlan.model_validate_json(plan_reply("h1"))
    ledger, _ = rounds.start(
        selected.hypotheses,
        rounds.Ledger(rounds.decode(rounds.EncodedLedger()).search),
        plan,
        round_number=1,
        parent=rounds.Parent(None, BASE.revision_id.root),
    )
    fields = {
        "config": selected,
        "terms": TERMS,
        "baseline": NotConfigured(),
        "number": 1,
        "plan": plan,
        "base": BASE,
        "ledger": ledger,
        "validation_offered": validation_offered,
        "revalidation_required": False,
        **changes,
    }
    return MultiAttempt(**fields)  # ty: ignore[invalid-argument-type]


def reading(kind: EvidenceKind, *, passed: bool) -> AcceptedReading:
    return AcceptedReading(
        source_request=RequestId(root=f"request-{kind.value}"),
        evidence_id=EvidenceId(root=f"evidence-{kind.value}"),
        kind=kind,
        passed=passed,
        stage=kind.value,
        metrics=(),
        feedback="" if passed else f"{kind.value} rejected",
    )


PASSING_GATES = Gated(
    readings=(
        reading(EvidenceKind.CORRECTNESS, passed=True),
        reading(EvidenceKind.BENCHMARK, passed=True),
    )
)


def reviewed_attempt(
    *, validation_offered: bool = False, recipe: str | None = None, rounds_allowed: int = 1
) -> MultiAttempt:
    """An attempt whose implementer replied with a candidate and whose review is due."""
    taken = attempt(validation_offered=validation_offered, config=config(max_rounds=rounds_allowed))
    extra = {} if recipe is None else {"validation_recipe_artifact": recipe}
    assert taken.replied(implemented(**extra), CANDIDATE) is Next.REVIEW
    return taken


def test_a_reply_with_a_candidate_is_reviewed() -> None:
    taken = reviewed_attempt()
    assert taken.review_started
    assert taken.candidate == CANDIDATE


def test_a_reply_that_claims_a_ready_candidate_it_did_not_retain_asks_again() -> None:
    taken = attempt()
    assert taken.replied(implemented(), None) is Next.AGAIN
    assert taken.no_valid_reply is not None
    assert not taken.passed


@settings(max_examples=examples(pr=8, nightly=100))
@given(feedback=st.text(min_size=1, max_size=8))
def test_a_review_that_fails_asks_again_with_its_feedback_and_never_passes(feedback: str) -> None:
    taken = reviewed_attempt()
    outcome = taken.judged(reviewed(passed=False, feedback=feedback), JUDGE_TURN)
    assert outcome is Next.AGAIN
    assert taken.feedback == feedback
    assert taken.judge_pass is False
    assert not taken.passed


def test_a_review_that_passes_goes_to_the_gates_when_they_are_due() -> None:
    taken = reviewed_attempt()
    assert taken.judged(reviewed(), JUDGE_TURN) is Next.GATES
    assert taken.judge_pass is True
    assert taken.official_reason is not None


def test_a_review_that_passes_validates_the_recipe_the_implementer_wrote_when_offered() -> None:
    taken = reviewed_attempt(validation_offered=True, recipe="validation/recipes.json")
    assert taken.judged(reviewed(), JUDGE_TURN) is Next.VALIDATE


def test_a_recipe_is_not_validated_when_the_run_does_not_offer_validation() -> None:
    taken = reviewed_attempt(validation_offered=False, recipe="validation/recipes.json")
    assert taken.judged(reviewed(), JUDGE_TURN) is Next.GATES


@settings(max_examples=examples(pr=8, nightly=100))
@given(error=st.text(min_size=1, max_size=8), unusable=st.booleans())
def test_a_recipe_that_fails_is_told_to_the_implementer_and_asks_again(
    *, error: str, unusable: bool
) -> None:
    taken = reviewed_attempt(validation_offered=True, recipe="validation/recipes.json")
    taken.judged(reviewed(), JUDGE_TURN)
    report = LocalValidationReport(
        status="succeeded", recipe="check", error=error, recipe_unusable=unusable
    )
    assert taken.validated(report) is Next.AGAIN
    assert taken.validation_failure is not None
    assert taken.validation_failure.error == error
    assert taken.validation_failure.recipe_unusable is unusable


@settings(max_examples=examples(pr=8, nightly=100))
@given(
    report=st.none() | st.just(LocalValidationReport(status="failed", detail="the runner broke"))
)
def test_a_validation_that_could_not_run_approves_on_the_judges_verdict(
    report: LocalValidationReport | None,
) -> None:
    taken = reviewed_attempt(validation_offered=True, recipe="validation/recipes.json")
    taken.judged(reviewed(), JUDGE_TURN)
    assert taken.validated(report) is Next.GATES
    assert taken.validation_failure is None


def test_a_recipe_that_passes_approves_the_claims() -> None:
    taken = reviewed_attempt(validation_offered=True, recipe="validation/recipes.json")
    taken.judged(reviewed(), JUDGE_TURN)
    assert taken.validated(LocalValidationReport(status="succeeded", passed=True)) is Next.GATES


def test_a_gate_that_rejects_asks_again_and_demands_a_review_of_the_repair() -> None:
    taken = reviewed_attempt()
    taken.judged(reviewed(), JUDGE_TURN)
    rejected = Gated(readings=(reading(EvidenceKind.CORRECTNESS, passed=False),))
    assert taken.gated(rejected) is Next.AGAIN
    assert taken.gate_failed
    assert taken.revalidation_required
    assert not taken.passed


@settings(max_examples=examples(pr=8, nightly=100))
@given(feedback=st.text(min_size=1, max_size=8))
def test_a_candidate_the_machinery_could_not_measure_is_not_charged_against_the_agent(
    feedback: str,
) -> None:
    taken = reviewed_attempt()
    taken.judged(reviewed(), JUDGE_TURN)
    assert taken.gated(NotMeasured(feedback=feedback)) is Next.SETTLE
    assert taken.unmeasured
    assert taken.failure is None


@settings(max_examples=examples(pr=8, nightly=100))
@given(reason=st.text(min_size=1, max_size=8))
def test_gates_that_cannot_be_read_fail_the_attempt_with_the_reason(reason: str) -> None:
    taken = reviewed_attempt()
    taken.judged(reviewed(), JUDGE_TURN)
    assert taken.gated(Unreadable(reason=reason)) is Next.SETTLE
    assert taken.failure == reason


# -- the difference from dynamic: a missing review is never a pass -----------------------------


def test_a_candidate_whose_review_passed_and_whose_gates_passed_is_eligible() -> None:
    taken = reviewed_attempt()
    taken.judged(reviewed(), JUDGE_TURN)
    taken.gated(PASSING_GATES)
    _, assessment = taken.asserted()
    assert taken.judge_outcome() == JudgeReviewed("pass")
    assert assessment.eligible


def test_a_candidate_whose_review_was_lost_is_kept_but_never_eligible() -> None:
    taken = reviewed_attempt()
    assert taken.review_lost() is Next.SETTLE
    _, assessment = taken.asserted()
    assert taken.judge_pass is None
    assert not assessment.eligible


@settings(max_examples=examples(pr=8, nightly=100))
@given(gates=st.booleans())
def test_without_a_passing_review_no_gate_result_makes_a_candidate_eligible(*, gates: bool) -> None:
    """Whatever the gates read, a round the review did not pass is not a winner."""
    taken = reviewed_attempt()
    taken.review_lost()
    if gates:
        taken.readings = PASSING_GATES.readings
        taken.passed = True
    _, assessment = taken.asserted()
    assert not assessment.eligible


def test_a_failed_attempt_is_never_eligible() -> None:
    taken = reviewed_attempt()
    taken.judged(reviewed(), JUDGE_TURN)
    taken.gated(PASSING_GATES)
    taken.failed("the workspace broke")
    _, assessment = taken.asserted()
    assert not assessment.eligible


# -- when a review is asked ------------------------------------------------------------------


def test_a_continuation_after_a_failed_review_is_not_reviewed_again() -> None:
    taken = reviewed_attempt(rounds_allowed=3)
    taken.judged(reviewed(passed=False), JUDGE_TURN)
    assert taken.replied(implemented("continue", next_step="more"), CANDIDATE) is Next.SETTLE
    assert taken.review_skipped


def test_a_failed_official_gate_forces_the_next_review_even_for_a_continuation() -> None:
    taken = reviewed_attempt(rounds_allowed=3)
    taken.judged(reviewed(), JUDGE_TURN)
    taken.gated(Gated(readings=(reading(EvidenceKind.CORRECTNESS, passed=False),)))
    assert taken.replied(implemented("continue", next_step="more"), CANDIDATE) is Next.REVIEW


def test_a_pareto_claim_the_live_archive_dominates_never_passes_review() -> None:
    taken = reviewed_attempt()
    taken.conflict = ArchiveConflictView(
        dominators=(
            Dominator(
                round_number=1, metrics=(ConflictMetric(name="x", value=1.0, direction="max"),)
            ),
        )
    )
    assert taken.judged(reviewed(passed=True), JUDGE_TURN) is Next.AGAIN
    assert taken.judge_pass is False
    assert taken.feedback is None  # the dominating rows are told, not the judge's praise
    assert taken.conflict is not None
    assert not taken.passed


def test_a_continuation_in_the_final_round_is_reviewed_again() -> None:
    taken = reviewed_attempt(rounds_allowed=1)
    taken.judged(reviewed(passed=False), JUDGE_TURN)
    assert taken.replied(implemented("continue", next_step="more"), CANDIDATE) is Next.REVIEW
