"""Deadline events are deterministic data, including recovery and queue uncertainty."""

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from vibesys.orchestration.dynamic import DynamicState
from vibesys.orchestration.dynamic.lifecycle import (
    CancelEvaluation,
    CompleteIntent,
    DispatchIntent,
    EvaluationContinuation,
    EvaluationDependency,
    EvaluationOutcome,
    EvaluationTimeout,
    InspectEvaluation,
    IntentKind,
    IntentStage,
    LifecycleIntent,
    LifecycleState,
    PrepareIntent,
    RecoveryStarted,
    ResumeAgentTurn,
    TimedOut,
)
from vibesys.orchestration.dynamic.models import WorkstreamPhase
from vibesys.orchestration.dynamic.transitions import (
    DeadlineReached,
    EvaluationDispatchStopped,
    EvaluationInspected,
    EvaluationSettled,
    EvaluationWaitReopened,
    SettlementProposed,
    WithdrawRequested,
    WorkerAwaitingEvaluation,
    step,
)


@dataclass
class FakeClock:
    """Explicit logical time; advancing never schedules or reads a host clock."""

    at_s: float = 0

    def advance_to(self, at_s: float) -> None:
        self.at_s = at_s


def waiting() -> DynamicState:
    payload = json.loads((Path(__file__).parent / "fixtures/state_v6/completed.json").read_text())
    for hypothesis in payload["search"]["hypotheses"]:
        hypothesis.update(
            rounds=[], declared_outcome=None, resolution=None, candidate_retained=None
        )
    state = DynamicState.model_validate_json(json.dumps(payload))
    state.workstreams[0].phase = WorkstreamPhase.IMPLEMENTING
    state.winner_revision = None
    turn = LifecycleIntent(
        operation_id="yielded",
        invocation_id="yielded",
        scope_id="kept",
        generation=1,
        kind=IntentKind.TURN,
        stage=IntentStage.DISPATCHED,
    )
    state.lifecycle = LifecycleState(intents={"yielded": turn})
    continuation = EvaluationContinuation(
        continuation_id="wait",
        scope_id="kept",
        generation=1,
        evaluation_scope_id="workspace",
        evaluation_generation=0,
        role="implementer",
        session_key="session",
        yielded_invocation_id="yielded",
        retained_revision="retained",
        original_stage="implementing",
        deadline_at_s=100,
        dependencies=tuple(
            EvaluationDependency(
                handle=handle,
                scope_id="workspace",
                generation=0,
                candidate_revision="candidate",
                candidate_digest="a" * 64,
                evaluator_digest="b" * 64,
                workload_digest="c" * 64,
                environment_digest="d" * 64,
            )
            for handle in ("a", "b")
        ),
    )
    state = step(state, WorkerAwaitingEvaluation(continuation=continuation))[0]
    for handle in ("a", "b"):
        state, _ = step(
            state,
            observation(handle, 0, EvaluationOutcome.UNKNOWN).model_copy(
                update={"observation_state": "pending", "stage": "queued"},
            ),
        )
    return state


def observation(
    handle: str, at_s: float, outcome: EvaluationOutcome = EvaluationOutcome.SUCCEEDED
) -> EvaluationSettled:
    return EvaluationSettled(
        continuation_id="wait",
        scope_id="workspace",
        generation=0,
        handle=handle,
        at_s=at_s,
        candidate_digest="a" * 64,
        evaluator_digest="b" * 64,
        workload_digest="c" * 64,
        environment_digest="d" * 64,
        outcome=outcome,
    )


def expire(state: DynamicState, at_s: float = 100) -> DynamicState:
    clock = FakeClock()
    clock.advance_to(at_s)
    return step(state, DeadlineReached(continuation_id="wait", at_s=clock.at_s))[0]


def replay(state: DynamicState) -> tuple[DynamicState, tuple]:
    restored = DynamicState.model_validate_json(state.model_dump_json(round_trip=True), strict=True)
    return step(restored, RecoveryStarted())


def test_deadline_in_queue_cancels_each_pending_handle_and_resumes_once() -> None:
    state = waiting()
    state, _ = step(
        state,
        observation("a", 90, EvaluationOutcome.UNKNOWN).model_copy(
            update={
                "observation_state": "pending",
                "stage": "queued",
                "queued_seconds": 90.0,
                "pending_reason": "Resources",
            }
        ),
    )
    state = expire(state)
    state, requests = replay(state)
    assert len([request for request in requests if isinstance(request, CancelEvaluation)]) == 2
    assert not any(isinstance(request, InspectEvaluation) for request in requests)
    resume = next(request for request in requests if isinstance(request, ResumeAgentTurn))
    assert isinstance(resume.outcome, TimedOut)
    detail = next(detail for detail in resume.outcome.evaluations if detail.handle == "a")
    assert detail.stage == "queued"
    assert detail.queued_seconds == 90
    assert detail.ran_seconds is None
    assert expire(state) == state
    assert step(state, observation("a", 101))[0] == state


def test_deadline_one_settled_one_unknown_inspects_then_blocks_unresolved_intent() -> None:
    state, _ = step(waiting(), observation("a", 50))
    state, _ = step(state, observation("b", 60, EvaluationOutcome.UNKNOWN))
    state = expire(state)
    state, requests = replay(state)
    inspect = next(request for request in requests if isinstance(request, InspectEvaluation))
    assert inspect.handle == "b"
    assert not any(isinstance(request, CancelEvaluation) for request in requests)
    state, _ = step(
        state,
        EvaluationInspected(
            operation_id=inspect.operation_id,
            outcome=EvaluationOutcome.UNKNOWN,
        ),
    )
    assert state.lifecycle.intents[inspect.operation_id].stage is IntentStage.BLOCKED
    assert state.lifecycle.intents["wait/cancel-1"].stage is IntentStage.BLOCKED
    state, requests = replay(state)
    assert len(requests) == 1
    assert isinstance(requests[0], ResumeAgentTurn)


@pytest.mark.parametrize("at_s", [100, 101])
@given(deadline_first=st.booleans())
def test_settlement_at_or_after_deadline_always_times_out(
    at_s: int, *, deadline_first: bool
) -> None:
    state, _ = step(waiting(), observation("a", 50))
    if deadline_first:
        state = expire(state)
    state, _ = step(state, observation("b", at_s))
    state = expire(state)
    state, requests = replay(state)
    resume = next(request for request in requests if isinstance(request, ResumeAgentTurn))
    assert isinstance(resume.outcome, TimedOut)
    assert state.lifecycle.continuations["wait"].settlements == {"a": EvaluationOutcome.SUCCEEDED}
    assert len([request for request in requests if isinstance(request, CancelEvaluation)]) == 1


def test_settlement_before_deadline_wins_and_later_expiry_is_inert() -> None:
    state, _ = step(waiting(), observation("a", 98))
    state, _ = step(state, observation("b", 99))
    assert expire(state) == state
    _, requests = replay(state)
    assert len(requests) == 1
    assert isinstance(requests[0], ResumeAgentTurn)
    assert requests[0].outcome is None


def test_deadline_during_park_cancels_but_defers_resume_until_reopen() -> None:
    state, requests = step(waiting(), WithdrawRequested(scope_id="kept", kind=IntentKind.PARK))
    park = requests[0].operation_id
    state, _ = step(state, SettlementProposed(operation_id=park, retry_limit=3, at_s=10))
    state = expire(state)
    _, requests = replay(state)
    assert len(requests) == 2
    assert all(isinstance(request, CancelEvaluation) for request in requests)
    state, _ = step(state, EvaluationWaitReopened(continuation_id="wait"))
    _, requests = replay(state)
    assert len([request for request in requests if isinstance(request, ResumeAgentTurn)]) == 1
    assert isinstance(
        next(request for request in requests if isinstance(request, ResumeAgentTurn)).outcome,
        TimedOut,
    )


@given(
    actions=st.lists(
        st.sampled_from(["expire", "settle", "restart", "dispatch", "ack"]), max_size=25
    )
)
def test_deadline_generated_replay_keeps_one_resume_and_stable_cancel_ids(
    actions: list[str],
) -> None:
    state = waiting()
    for action in ["expire", *actions]:
        before = state.model_dump_json(round_trip=True)
        old = state
        if action == "expire":
            state = expire(state)
        elif action == "settle":
            state, _ = step(state, observation("a", 101))
        elif action == "restart":
            state, _ = replay(state)
        elif action == "dispatch":
            state, _ = step(state, DispatchIntent(operation_id="wait/resume"))
        else:
            state, _ = step(state, CompleteIntent(operation_id="wait/cancel-0"))
        assert old.model_dump_json(round_trip=True) == before
        assert (
            len(
                [
                    intent
                    for intent in state.lifecycle.intents.values()
                    if intent.kind is IntentKind.RESUME
                ]
            )
            == 1
        )
        assert state.lifecycle.continuations["wait"].settlements == {}
        assert {key for key in state.lifecycle.intents if "/cancel-" in key} == {
            "wait/cancel-0",
            "wait/cancel-1",
        }
        assert state.workstreams[0].budget == waiting().workstreams[0].budget


@pytest.mark.parametrize("stage", [None, "queued", "benchmark"])
def test_unknown_state_inspects_even_when_stage_is_known(stage: str | None) -> None:
    state, _ = step(
        waiting(),
        observation("a", 90, EvaluationOutcome.UNKNOWN).model_copy(update={"stage": stage}),
    )
    state = expire(state)
    _, requests = replay(state)
    assert any(
        isinstance(request, InspectEvaluation) and request.handle == "a" for request in requests
    )
    assert not any(
        isinstance(request, CancelEvaluation) and request.handle == "a" for request in requests
    )


def test_unobserved_dependency_is_unknown_and_requires_inspection() -> None:
    state = waiting()
    continuation = state.lifecycle.continuations["wait"].model_copy(update={"progress": {}})
    state.lifecycle = state.lifecycle.model_copy(update={"continuations": {"wait": continuation}})
    state = expire(state)
    _, requests = replay(state)
    assert len([request for request in requests if isinstance(request, InspectEvaluation)]) == 2
    assert not any(isinstance(request, CancelEvaluation) for request in requests)


@pytest.mark.parametrize("observation_state", ["pending", "running"])
def test_inspection_recovery_authorizes_one_cancellation(observation_state: str) -> None:
    state, _ = step(waiting(), observation("a", 90, EvaluationOutcome.UNKNOWN))
    state = expire(state)
    state, requests = replay(state)
    inspect = next(request for request in requests if isinstance(request, InspectEvaluation))
    state, _ = step(state, DispatchIntent(operation_id=inspect.operation_id))
    state, _ = replay(state)
    event = EvaluationInspected(
        operation_id=inspect.operation_id,
        outcome=EvaluationOutcome.UNKNOWN,
        observation_state=observation_state,
    )
    state, requests = step(state, event)
    assert len(requests) == 1
    assert isinstance(requests[0], CancelEvaluation)
    assert requests[0].handle == "a"
    assert step(state, event)[0] == state
    restored, requests = replay(state)
    assert restored == state
    assert any(
        isinstance(request, CancelEvaluation) and request.handle == "a" for request in requests
    )


@pytest.mark.parametrize(
    "outcome", [EvaluationOutcome.SUCCEEDED, EvaluationOutcome.FAILED, EvaluationOutcome.CANCELLED]
)
def test_inspection_terminal_result_completes_inspection_without_rewriting_timeout(
    outcome: EvaluationOutcome,
) -> None:
    state, _ = step(waiting(), observation("a", 90, EvaluationOutcome.UNKNOWN))
    state = expire(state)
    timeout = state.lifecycle.continuations["wait"].timed_out
    state, requests = replay(state)
    inspect = next(request for request in requests if isinstance(request, InspectEvaluation))
    state, requests = step(
        state, EvaluationInspected(operation_id=inspect.operation_id, outcome=outcome)
    )
    assert requests == ()
    assert state.lifecycle.intents[inspect.operation_id].stage is IntentStage.COMPLETED
    assert state.lifecycle.continuations["wait"].timed_out == timeout
    replay(state)


@pytest.mark.parametrize("load", [False, True])
def test_unknown_canonical_cancellation_cannot_bypass_inspection(*, load: bool) -> None:
    state, _ = step(waiting(), observation("a", 90, EvaluationOutcome.UNKNOWN))
    state = expire(state)
    cancel = LifecycleIntent(
        operation_id="wait/cancel-0",
        scope_id="kept",
        generation=1,
        continuation_id="wait",
        evaluation_index=0,
        kind=IntentKind.CANCEL_EVALUATION,
    )
    if load:
        payload = json.loads(state.model_dump_json(round_trip=True))
        payload["lifecycle"]["intents"][cancel.operation_id] = cancel.model_dump(mode="json")
        with pytest.raises(ValidationError, match="blocked inspection"):
            DynamicState.model_validate_json(json.dumps(payload), strict=True)
    else:
        with pytest.raises(ValueError, match="blocked inspection"):
            step(state, PrepareIntent(intent=cancel))


def test_deadline_returns_only_owned_requests_and_cancellation_ignores_dispatch_stop() -> None:
    state = waiting()
    unrelated = LifecycleIntent(
        operation_id="other", scope_id="other", generation=1, kind=IntentKind.PARK
    )
    state, _ = step(state, PrepareIntent(intent=unrelated))
    state, _ = step(state, EvaluationDispatchStopped())
    state, requests = step(state, DeadlineReached(continuation_id="wait", at_s=103))
    assert len(requests) == 2
    assert all(isinstance(request, CancelEvaluation) for request in requests)
    assert state.lifecycle.continuations["wait"].timed_out.reached_at_s == 103


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf")])
def test_snapshot_and_event_reject_invalid_deadline_time(value: float) -> None:
    with pytest.raises(ValidationError):
        DeadlineReached(continuation_id="wait", at_s=value)
    payload = json.loads(waiting().model_dump_json(round_trip=True))
    payload["lifecycle"]["continuations"]["wait"]["deadline_at_s"] = value
    with pytest.raises(ValidationError):
        DynamicState.model_validate_json(json.dumps(payload), strict=True)


def test_early_deadline_and_completed_historical_deadline_are_inert() -> None:
    state = waiting()
    assert expire(state, 99) == state
    state, _ = step(state, observation("a", 90))
    state, _ = step(state, observation("b", 91))
    state, _ = step(state, CompleteIntent(operation_id="wait/resume"))
    assert expire(state) == state


@given(
    stage=st.sampled_from(["queued", "accuracy", "benchmark", "profile"]),
    seconds=st.floats(min_value=0, max_value=100, allow_nan=False, allow_infinity=False),
)
def test_boundary_observation_keeps_stage_and_timing(stage: str, seconds: float) -> None:
    state, requests = step(
        waiting(),
        observation("a", 100, EvaluationOutcome.UNKNOWN).model_copy(
            update={
                "observation_state": "pending",
                "stage": stage,
                "queued_seconds": seconds,
                "ran_seconds": seconds,
            }
        ),
    )
    detail = next(
        detail
        for detail in state.lifecycle.continuations["wait"].timed_out.evaluations
        if detail.handle == "a"
    )
    assert detail.stage == stage
    assert detail.queued_seconds == seconds
    assert detail.ran_seconds == seconds
    assert any(
        isinstance(request, CancelEvaluation) and request.handle == "a" for request in requests
    )
    assert not any(
        isinstance(request, InspectEvaluation) and request.handle == "a" for request in requests
    )


@given(earlier=st.integers(min_value=0, max_value=98))
def test_reordered_old_pending_cannot_override_newer_unknown(earlier: int) -> None:
    state, _ = step(waiting(), observation("a", 99, EvaluationOutcome.UNKNOWN))
    state, _ = step(
        state,
        observation("a", earlier, EvaluationOutcome.UNKNOWN).model_copy(
            update={"observation_state": "pending"}
        ),
    )
    state = expire(state)
    _, requests = replay(state)
    assert any(
        isinstance(request, InspectEvaluation) and request.handle == "a" for request in requests
    )
    assert not any(
        isinstance(request, CancelEvaluation) and request.handle == "a" for request in requests
    )


@pytest.mark.parametrize("expired", [False, True])
def test_duplicate_original_yield_remains_inert_after_expiry(*, expired: bool) -> None:
    state = waiting()
    original = state.lifecycle.continuations["wait"]
    if expired:
        state = expire(state)
    assert step(state, WorkerAwaitingEvaluation(continuation=original))[0] == state


def test_new_registration_rejects_caller_manufactured_timeout() -> None:
    state = waiting()
    continuation = state.lifecycle.continuations["wait"]
    manufactured = continuation.model_copy(
        update={
            "timed_out": TimedOut(
                deadline_at_s=100,
                reached_at_s=100,
                evaluations=tuple(EvaluationTimeout(handle=handle) for handle in ("a", "b")),
            )
        }
    )
    original = state.lifecycle.intents["yielded"].model_copy(
        update={"stage": IntentStage.DISPATCHED}
    )
    state.lifecycle = LifecycleState(intents={"yielded": original})
    before = state.model_dump_json(round_trip=True)
    with pytest.raises(ValueError, match="DeadlineReached owns expiry"):
        step(state, WorkerAwaitingEvaluation(continuation=manufactured))
    assert state.model_dump_json(round_trip=True) == before


@given(unknown=st.booleans(), handle=st.sampled_from(["a", "b"]))
def test_expired_snapshot_rejects_missing_termination_authority(
    *, unknown: bool, handle: str
) -> None:
    state = waiting()
    if unknown:
        state, _ = step(state, observation(handle, 90, EvaluationOutcome.UNKNOWN))
    state = expire(state)
    index = ("a", "b").index(handle)
    operation_id = f"wait/{'inspect' if unknown else 'cancel'}-{index}"
    payload = json.loads(state.model_dump_json(round_trip=True))
    payload["lifecycle"]["intents"].pop(operation_id)
    with pytest.raises(ValidationError, match=r"requires its (inspect|cancel) intent"):
        DynamicState.model_validate_json(json.dumps(payload), strict=True)


def test_expired_active_snapshot_rejects_missing_resume_authority() -> None:
    payload = json.loads(expire(waiting()).model_dump_json(round_trip=True))
    payload["lifecycle"]["intents"].pop("wait/resume")
    with pytest.raises(ValidationError, match="active continuation requires its resume"):
        DynamicState.model_validate_json(json.dumps(payload), strict=True)


@given(unknown=st.booleans())
def test_expired_snapshot_rejects_unrelated_intent_as_termination_authority(
    *, unknown: bool
) -> None:
    state = waiting()
    if unknown:
        state, _ = step(state, observation("a", 90, EvaluationOutcome.UNKNOWN))
    state = expire(state)
    operation_id = f"wait/{'inspect' if unknown else 'cancel'}-0"
    fake_authority = LifecycleIntent(
        operation_id=operation_id, scope_id="kept", generation=1, kind=IntentKind.PARK
    )
    payload = json.loads(state.model_dump_json(round_trip=True))
    payload["lifecycle"]["intents"][operation_id] = fake_authority.model_dump(mode="json")
    with pytest.raises(ValidationError, match=r"requires its (inspect|cancel) intent"):
        DynamicState.model_validate_json(json.dumps(payload), strict=True)
