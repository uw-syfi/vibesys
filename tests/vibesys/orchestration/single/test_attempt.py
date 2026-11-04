"""What each reply and gate result means for one single attempt, as plain data.

`SingleAttempt` is the pure bookkeeping of a round: it decides, from what the turns established, whether
to ask the implementer again, to run the trusted gates or to settle. Its rules are stated over
generated replies and gate results; the flow that awaits is told by the stories.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.single._host import config, implemented
from tests.vibesys.serial_search._host import TERMS
from tests.vibesys.serial_search._replies import plan_reply

from vibesys.candidates.api import NotConfigured
from vibesys.hypothesis import OrchestratorPlan, RoundRecord, rounds
from vibesys.hypothesis.state import Hypothesis
from vibesys.metrics import AcceptedReading
from vibesys.orchestration.single.search.api import Next, SingleAttempt, SingleConfig
from vibesys.serial_search.api import Gated, NotMeasured, Unreadable
from vs_core.api import EvidenceId, EvidenceKind, RequestId, RevisionRef
from vs_sim.api.testing import examples

BASE = RevisionRef.of_git_commit("base")
CANDIDATE = RevisionRef.of_git_commit("candidate")


def attempt(*, measured: int = 0, **changes: object) -> SingleAttempt:
    """An attempt of round ``measured + 1``, after ``measured`` rounds the gates measured."""
    given = changes.pop("config", None)
    assert given is None or isinstance(given, SingleConfig)
    selected = config() if given is None else given
    plan = OrchestratorPlan.model_validate_json(plan_reply("h1"))
    earlier = rounds.decode(rounds.EncodedLedger()).search
    for number in range(1, measured + 1):
        record = RoundRecord(
            number,
            f"m{number}",
            None,
            None,
            passed=True,
            reviewed=True,
            hypothesis_outcome="proven",
            hypothesis_id=f"m{number}",
            judge_verdict="pass",
            official_evaluation=True,
        )
        earlier.hypotheses.append(
            Hypothesis(
                hypothesis_id=f"m{number}",
                plan=OrchestratorPlan.model_validate_json(plan_reply(f"m{number}")),
                started_round=number,
                rounds=[record],
            )
        )
    ledger, _ = rounds.start(
        selected.hypotheses,
        rounds.Ledger(earlier),
        plan,
        round_number=measured + 1,
        parent=rounds.Parent(None, BASE.revision_id.root),
    )
    fields = {
        "config": selected,
        "terms": TERMS,
        "baseline": NotConfigured(),
        "number": measured + 1,
        "plan": plan,
        "base": BASE,
        "ledger": ledger,
        **changes,
    }
    return SingleAttempt(**fields)  # ty: ignore[invalid-argument-type]


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


@settings(max_examples=examples(pr=8, nightly=100))
@given(feedback=st.text(min_size=1, max_size=8))
def test_a_self_review_that_fails_asks_again_with_its_feedback_and_never_passes(
    feedback: str,
) -> None:
    taken = attempt()
    outcome = taken.replied(implemented(passed=False, feedback=feedback), CANDIDATE)
    assert outcome is Next.AGAIN
    assert taken.feedback == feedback
    assert not taken.passed


@settings(max_examples=examples(pr=8, nightly=100))
@given(revision=st.sampled_from([None, BASE]))
def test_a_passing_self_review_without_a_changed_candidate_asks_again(
    revision: RevisionRef | None,
) -> None:
    taken = attempt()
    assert taken.replied(implemented(), revision) is Next.AGAIN
    assert taken.no_valid_reply is not None
    assert not taken.passed


def test_a_passing_self_review_with_a_candidate_goes_to_the_gates_when_they_are_due() -> None:
    taken = attempt()
    assert taken.replied(implemented(), CANDIDATE) is Next.GATES
    assert taken.official_reason is not None
    assert not taken.passed


def test_a_gate_that_rejects_asks_again_and_demands_the_gates_again() -> None:
    taken = attempt()
    taken.replied(implemented(), CANDIDATE)
    rejected = Gated(readings=(reading(EvidenceKind.CORRECTNESS, passed=False),))
    assert taken.gated(rejected) is Next.AGAIN
    assert taken.gate_failed
    assert taken.feedback == "correctness rejected"
    assert not taken.passed


def test_gates_that_pass_settle_the_attempt_as_passed() -> None:
    taken = attempt()
    taken.replied(implemented(), CANDIDATE)
    passing = Gated(
        readings=(
            reading(EvidenceKind.CORRECTNESS, passed=True),
            reading(EvidenceKind.BENCHMARK, passed=True),
        )
    )
    assert taken.gated(passing) is Next.SETTLE
    assert taken.passed
    assert not taken.gate_failed


@settings(max_examples=examples(pr=8, nightly=100))
@given(feedback=st.text(min_size=1, max_size=8))
def test_a_candidate_the_machinery_could_not_measure_is_not_charged_against_the_agent(
    feedback: str,
) -> None:
    taken = attempt()
    taken.replied(implemented(), CANDIDATE)
    assert taken.gated(NotMeasured(feedback=feedback)) is Next.SETTLE
    assert taken.unmeasured
    assert not taken.gate_failed
    assert taken.failure is None


@settings(max_examples=examples(pr=8, nightly=100))
@given(reason=st.text(min_size=1, max_size=8))
def test_gates_that_cannot_be_read_fail_the_attempt_with_the_reason(reason: str) -> None:
    taken = attempt()
    taken.replied(implemented(), CANDIDATE)
    assert taken.gated(Unreadable(reason=reason)) is Next.SETTLE
    assert taken.failure == reason


@settings(max_examples=examples(pr=8, nightly=100))
@given(first=st.text(min_size=1, max_size=5), second=st.text(min_size=1, max_size=5))
def test_the_first_failure_of_an_attempt_is_the_one_it_keeps(first: str, second: str) -> None:
    taken = attempt()
    taken.failed(first)
    taken.failed(second)
    assert taken.failure == first


def test_a_failed_attempt_is_never_eligible() -> None:
    taken = attempt()
    taken.replied(implemented(), CANDIDATE)
    taken.gated(Gated(readings=(reading(EvidenceKind.CORRECTNESS, passed=True),)))
    taken.failed("the workspace broke")
    _, assessment = taken.asserted()
    assert not assessment.eligible
