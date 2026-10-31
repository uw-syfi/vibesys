"""What a workstream's ending means: its typed failure, what it asserts, the round and parent it leaves.

These are the plain functions the workstream flow calls once its waits have answered. The
properties: every way a wait can end maps to one typed failure that keeps the facts core gave; a
candidate is winner-eligible exactly when every condition of the task and of review holds; a
finished workstream's record never offers a parent the evidence does not prove.
"""

from __future__ import annotations

from dataclasses import dataclass

from hypothesis import assume, given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic._host import TERMS
from tests.vibesys.orchestration.dynamic._values import (
    BASE,
    BASELINE_THROUGHPUT,
    CANDIDATE,
    evidence_of,
    evidence_ref,
    finished_workstream,
    reading_of_ref,
    result_of,
    settlement_of,
    workstream_start,
)

from vibesys.hypothesis import HypothesisOutcome
from vibesys.orchestration.dynamic.search.api import (
    UNSETTLED,
    AgentName,
    Blocker,
    BlockerKind,
    Closing,
    Evidence,
    Failure,
    FailureKind,
    Proof,
    WorkKind,
    WorkPlan,
    WorkstreamDone,
    assertion,
    blocked_failure,
    ended_by,
    failure_of,
    round_of,
    snapshot_of,
)
from vs_core.api import (
    AttemptId,
    AttemptRef,
    EvidenceKind,
    Failed,
    FailureReason,
    RejectionCode,
    ReplyError,
    ReplyErrorCode,
    ResourceId,
    SettlementId,
    TurnFailureKind,
    WaitRefusal,
    WhileAlive,
    WhileOpen,
)
from vs_sim.api.testing import examples

AGENT_REASONS = frozenset(
    {
        FailureReason.INVALID_REPLY,
        FailureReason.RENDER_FAILED,
        FailureReason.TURN_FAILED,
        FailureReason.WAIT_REFUSED,
    }
)


def _failed_facts() -> dict[FailureReason, st.SearchStrategy[Failed]]:
    """For each reason, every ``Failed`` core can deliver, with the facts that reason carries."""
    ref = AttemptRef(attempt_id=AttemptId(root="a"), generation=0)
    error = ReplyError(path=("x",), code=ReplyErrorCode.MISSING)

    def of(reason: FailureReason, **facts: st.SearchStrategy[object]) -> st.SearchStrategy[Failed]:
        return st.builds(Failed, reason=st.just(reason), **facts)

    return {
        FailureReason.TIMED_OUT: of(FailureReason.TIMED_OUT, limit_at=st.floats(0, 1e6)),
        FailureReason.QUEUE_BUDGET_SPENT: of(
            FailureReason.QUEUE_BUDGET_SPENT, limit_at=st.floats(0, 1e6)
        ),
        FailureReason.WORKSPACE_CLOSED: of(
            FailureReason.WORKSPACE_CLOSED, watch=st.just(WhileOpen(workspace=ref))
        ),
        FailureReason.OWNER_GONE: of(
            FailureReason.OWNER_GONE, watch=st.just(WhileAlive(job=ResourceId(root="job")))
        ),
        FailureReason.REFUSED: of(FailureReason.REFUSED, cause=st.sampled_from(RejectionCode)),
        FailureReason.INVALID_REPLY: of(FailureReason.INVALID_REPLY, errors=st.just((error,))),
        FailureReason.TURN_FAILED: of(
            FailureReason.TURN_FAILED, turn_failure=st.sampled_from(TurnFailureKind)
        ),
        FailureReason.WAIT_REFUSED: of(
            FailureReason.WAIT_REFUSED, refusal=st.sampled_from(WaitRefusal)
        ),
        **{
            reason: of(reason)
            for reason in (
                FailureReason.LOST,
                FailureReason.RENDER_FAILED,
                FailureReason.OUT_OF_BUDGET,
                FailureReason.MEASUREMENT_UNTRUSTED,
                FailureReason.ATTEMPTS_EXHAUSTED,
            )
        },
    }


FAILED_FACTS = _failed_facts()
WAIT_FAILURES = st.sampled_from(FailureReason).flatmap(FAILED_FACTS.__getitem__)


@st.composite
def wait_endings(draw: st.DrawFn) -> tuple[Failed, AgentName | None]:
    """A wait that failed and the agent it belonged to: only an agent's turn has reasons of its own."""
    event = draw(WAIT_FAILURES)
    if event.reason in AGENT_REASONS:
        return event, draw(st.sampled_from(AgentName))
    return event, draw(st.none() | st.sampled_from(AgentName))


def test_every_reason_a_wait_can_fail_is_generated() -> None:
    assert set(FAILED_FACTS) == set(FailureReason)


@settings(max_examples=examples(pr=8, nightly=100))
@given(ending=wait_endings())
def test_every_wait_that_ends_without_a_result_is_one_typed_failure_keeping_its_facts(
    ending: tuple[Failed, AgentName | None],
) -> None:
    event, role = ending
    found = failure_of(event, role)
    assert found.kind in set(FailureKind)
    match event.reason:
        case FailureReason.REFUSED:
            assert (found.kind, found.cause) == (FailureKind.REFUSED, event.cause)
        case FailureReason.OUT_OF_BUDGET:
            assert found.kind is FailureKind.OUT_OF_BUDGET
        case FailureReason.TURN_FAILED:
            assert found.turn_failure is event.turn_failure
            assert found.role is role
        case FailureReason.WAIT_REFUSED:
            assert (found.refusal, found.role) == (event.refusal, role)
        case FailureReason.INVALID_REPLY | FailureReason.RENDER_FAILED:
            assert found.role is role
        case _:
            assert (found.kind, found.ended) == (FailureKind.ENDED, event.reason)


@settings(max_examples=examples(pr=8, nightly=100))
@given(ending=wait_endings(), profiling=st.booleans())
def test_a_turn_that_could_not_suspend_for_lack_of_resume_also_records_the_missing_capability(
    ending: tuple[Failed, AgentName | None], *, profiling: bool
) -> None:
    event, role = ending
    kind = WorkKind.PROFILE if profiling else WorkKind.IMPLEMENT
    plan = WorkPlan(kind=kind, work_id="w")
    ended = ended_by(event, role, plan=plan)
    assert ended.failure == failure_of(event, role)
    lacks = event.reason is FailureReason.WAIT_REFUSED and event.refusal is WaitRefusal.NOT_OFFERED
    assert (ended.unreachable is not None) == lacks
    assert ended_by(event, role).unreachable is None


@settings(max_examples=examples(pr=8, nightly=100))
@given(
    blocker=st.builds(
        Blocker,
        kind=st.sampled_from(BlockerKind),
        ended=st.none() | st.sampled_from([FailureReason.TIMED_OUT, FailureReason.LOST]),
        turn_failure=st.none() | st.sampled_from(TurnFailureKind),
    )
)
def test_the_last_turn_that_gave_nothing_is_what_a_workstream_that_may_not_retry_ends_with(
    blocker: Blocker,
) -> None:
    found = blocked_failure(blocker)
    expected = {
        BlockerKind.FAILED: FailureKind.IMPLEMENTATION_FAILED,
        BlockerKind.REJECTED: FailureKind.REVIEW_REJECTED,
    }
    if blocker.kind in expected:
        assert found.kind is expected[blocker.kind]
    elif blocker.kind is BlockerKind.REPLY_REJECTED:
        # A reply the run refused, as the last turn: the implementer kept replying invalidly.
        assert (found.kind, found.role) == (FailureKind.INVALID_REPLY, AgentName.IMPLEMENTER)
    else:
        assert found.role is AgentName.IMPLEMENTER
        assert found.kind in {FailureKind.TURN_FAILED, FailureKind.ENDED}
        assert (found.kind is FailureKind.TURN_FAILED) == (blocker.turn_failure is not None)


# -- what a closing asserts ----------------------------------------------------------------------


@dataclass(frozen=True)
class Candidacy:
    """Everything that decides whether a finished candidate may win."""

    accuracy: bool | None
    benchmark: bool | None
    throughput: float
    outcome: HypothesisOutcome
    review: bool | None
    retained: bool
    cause: Failure | None


CANDIDACIES = st.builds(
    Candidacy,
    accuracy=st.none() | st.booleans(),
    benchmark=st.none() | st.booleans(),
    throughput=st.sampled_from(
        [BASELINE_THROUGHPUT - 1, BASELINE_THROUGHPUT, BASELINE_THROUGHPUT + 1]
    ),
    outcome=st.sampled_from(HypothesisOutcome),
    review=st.none() | st.booleans(),
    retained=st.booleans(),
    cause=st.none() | st.just(Failure(kind=FailureKind.OUT_OF_BUDGET)),
)


@settings(max_examples=examples(pr=8, nightly=100))
@given(case=CANDIDACIES)
def test_a_candidate_is_eligible_exactly_when_the_task_the_review_and_the_input_all_say_so(
    case: Candidacy,
) -> None:
    accuracy, benchmark, review = case.accuracy, case.benchmark, case.review
    closing = Closing(
        result=result_of(
            case.outcome, review=review, revision_=CANDIDATE if case.retained else None
        ),
        evidence=evidence_of(accuracy=accuracy, benchmark=benchmark, throughput=case.throughput),
        cause=case.cause,
    )
    start = workstream_start()
    settle = assertion(start, closing, TERMS, start.baseline)

    # TERMS measures accuracy and benchmark, so both gates need a passed reading.
    expected = (
        case.retained
        and accuracy is True
        and benchmark is True
        and review is not False
        and case.outcome not in UNSETTLED
        and case.cause is None
        and case.throughput > BASELINE_THROUGHPUT
    )
    assert settle.eligible == expected
    if settle.eligible:
        assert settle.retention == "candidate"
        assert settle.candidate == CANDIDATE
        assert settle.outcome == "succeeded"
    if settle.retention == "candidate":
        assert accuracy is True  # a retained parent is always accuracy-verified
    if settle.candidate is not None:
        assert settle.retention == "candidate"


@settings(max_examples=examples(pr=8, nightly=100))
@given(failed_workstream=st.booleans(), accuracy=st.none() | st.booleans())
def test_a_profile_workstream_settles_ineligible_and_discarded_whatever_it_measured(
    *, failed_workstream: bool, accuracy: bool | None
) -> None:
    ref = evidence_ref("profile", EvidenceKind.PROFILING, BASE)
    reading = reading_of_ref(ref, passed=accuracy is not False)
    closing = Closing(
        evidence=Evidence(readings=(reading,), proof=Proof(refs=(ref,))),
        cause=Failure(kind=FailureKind.OUT_OF_BUDGET) if failed_workstream else None,
    )
    settle = assertion(workstream_start(WorkKind.PROFILE), closing, TERMS, None)
    assert (settle.eligible, settle.retention, settle.candidate) == (False, "discard", None)
    assert settle.outcome == ("failed" if failed_workstream else "succeeded")


# -- what a finished workstream leaves behind ------------------------------------------------------


@settings(max_examples=examples(pr=8, nightly=100))
@given(
    accuracy=st.none() | st.booleans(),
    retention=st.sampled_from(["discard", "wip", "candidate"]),
    kind=st.sampled_from(WorkKind),
    with_proof=st.booleans(),
)
def test_a_parent_is_offered_only_for_a_retained_accuracy_proven_candidate_of_an_implement(
    *, accuracy: bool | None, retention: str, kind: WorkKind, with_proof: bool
) -> None:
    finished = finished_workstream(
        accuracy=accuracy, retention=retention, kind=kind, with_proof=with_proof
    )
    parent = snapshot_of(finished)
    offered = (
        retention == "candidate" and kind is WorkKind.IMPLEMENT and accuracy is True and with_proof
    )
    assert (parent is not None) == offered
    if parent is not None:
        assert parent.revision == CANDIDATE
        assert parent.generation == 3
        assert parent.hypothesis_id == "w"
        assert parent.submission_index == finished.start.sequence
        assert {ref.key for ref in parent.proof} >= {parent.accuracy.key}


@settings(max_examples=examples(pr=8, nightly=100))
@given(
    accuracy=st.booleans(), eligible=st.booleans(), retention=st.sampled_from(["candidate", "wip"])
)
def test_the_round_a_workstream_adds_records_what_core_settled_not_what_the_agent_claimed(
    *, accuracy: bool, eligible: bool, retention: str
) -> None:
    finished = finished_workstream(accuracy=accuracy, retention=retention, eligible=eligible)
    record = round_of(finished)
    assert record.eligible == eligible
    assert record.candidate == CANDIDATE
    assert record.settlement == SettlementId(root="settlement-1")
    assert record.accuracy_passed is accuracy
    assert record.attempt == finished.start.attempt
    assert record.sequence == finished.start.sequence
    assert record.outcome is HypothesisOutcome.SUPPORTED
    assert record.review_passed is True


@settings(max_examples=examples(pr=8, nightly=100))
@given(tail=st.text(max_size=30), benchmark_failed=st.booleans(), accuracy_failed=st.booleans())
def test_the_failure_text_of_a_round_is_the_failed_trusted_checks_own_output(
    tail: str, *, benchmark_failed: bool, accuracy_failed: bool
) -> None:
    assume(tail)
    accuracy_ref = evidence_ref("accuracy", EvidenceKind.CORRECTNESS, CANDIDATE)
    benchmark_ref = evidence_ref("benchmark", EvidenceKind.BENCHMARK, CANDIDATE)
    readings = (
        reading_of_ref(accuracy_ref, passed=not accuracy_failed, feedback=f"accuracy:{tail}"),
        reading_of_ref(benchmark_ref, passed=not benchmark_failed, feedback=f"benchmark:{tail}"),
    )
    finished = WorkstreamDone(
        start=workstream_start(),
        failure=None,
        settlement=settlement_of(eligible=False, retention="candidate"),
        evidence=Evidence(readings=readings, proof=Proof(refs=(accuracy_ref, benchmark_ref))),
    )
    expected = (
        f"benchmark:{tail}" if benchmark_failed else f"accuracy:{tail}" if accuracy_failed else ""
    )
    assert round_of(finished).failure_tail == expected
