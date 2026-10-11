"""Every built-in write strategy consumes the same typed checkpoint outcomes."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from tests.vibesys.orchestration.dynamic.strategy._replies import implemented
from tests.vibesys.orchestration.dynamic.strategy._run import config as dynamic_config
from tests.vibesys.orchestration.multi.strategy._replies import (
    implemented as multi_implemented,
)
from tests.vibesys.orchestration.multi.strategy._replies import (
    plan_reply as multi_plan_reply,
)
from tests.vibesys.orchestration.multi.strategy._run import config as multi_config
from tests.vibesys.orchestration.single.strategy._replies import combined, plan_reply
from tests.vibesys.orchestration.single.strategy._run import config as single_config

from vibesys.orchestration.dynamic.strategy.api import (
    DynamicStrategy,
    DynamicStrategyState,
)
from vibesys.orchestration.dynamic.strategy.api import (
    invocation_id as dynamic_invocation_id,
)
from vibesys.orchestration.dynamic.strategy.api import (
    session_id as dynamic_session_id,
)
from vibesys.orchestration.multi.strategy.api import (
    MultiStrategy,
    MultiStrategyState,
)
from vibesys.orchestration.multi.strategy.api import (
    invocation_id as multi_invocation_id,
)
from vibesys.orchestration.multi.strategy.api import (
    session_id as multi_session_id,
)
from vibesys.orchestration.single.strategy.api import (
    SingleStrategy,
    SingleStrategyState,
)
from vibesys.orchestration.single.strategy.api import (
    invocation_id as single_invocation_id,
)
from vibesys.orchestration.single.strategy.api import (
    session_id as single_session_id,
)
from vs_core.api import (
    AttemptBudget,
    AttemptCheckpoint,
    AttemptId,
    AttemptPhase,
    AttemptView,
    CheckpointDecline,
    CheckpointDeclined,
    EventId,
    InvocationRef,
    ItemId,
    Observation,
    ObservationStatus,
    RequestId,
    RevisionRef,
    RunView,
    Scope,
    Strategy,
    StrategyState,
    TurnResult,
    WorkspaceMode,
    WorkspacePlan,
    initial_state,
    project,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

_ATTEMPT = AttemptId(root="attempt")
_BASE = RevisionRef.of_git_commit("a" * 40)
_CANDIDATE = RevisionRef.of_git_commit("b" * 40)


def _invocation(role: str, subject: str, *, namespace: str) -> InvocationRef:
    identities = {
        "dynamic": (dynamic_session_id, dynamic_invocation_id),
        "multi": (multi_session_id, multi_invocation_id),
        "single": (single_session_id, single_invocation_id),
    }
    session, invocation = identities[namespace]
    return InvocationRef(
        session_id=session(role, subject),
        invocation_id=invocation(role, subject, 0),
        generation=0,
    )


def _event(invocation: InvocationRef, payload: str) -> TurnResult:
    return TurnResult(
        invocation=invocation,
        observation=Observation(
            event_id=EventId(root="turn-result"),
            request_id=RequestId(root="turn-request"),
            scope=Scope(owner=_ATTEMPT, generation=0),
            sequence=0,
            observed_at=1.0,
            status=ObservationStatus.SUCCEEDED,
            accepted=True,
            terminal=True,
            released=True,
            children_complete=True,
        ),
        output_json=payload,
    )


def _view(invocation: InvocationRef, outcome: str) -> RunView:
    """A public run view where this invocation is pending, retained, or failed."""
    base = project(initial_state())
    if outcome == "pending":
        return base
    attempt = AttemptView(
        attempt_id=_ATTEMPT,
        item_id=ItemId(root="item"),
        generation=0,
        phase=AttemptPhase.ACTIVE,
        workspace=WorkspacePlan(mode=WorkspaceMode.EXCLUSIVE_ROOT, base=_BASE),
        budget=AttemptBudget(),
        checkpoints=(
            AttemptCheckpoint(
                invocation=invocation,
                request_id=RequestId(root="checkpoint"),
                revision=_CANDIDATE,
                retention="candidate",
            ),
        )
        if outcome == "retained"
        else (),
        checkpoint_declines=(
            CheckpointDeclined(
                invocation=invocation,
                authority=RequestId(root="checkpoint"),
                reason=CheckpointDecline.SNAPSHOT_FAILED,
            ),
        )
        if outcome == "failed"
        else (),
    )
    return base.model_copy(update={"attempts": (attempt,)})


def _record(state: StrategyState, collection: str) -> Mapping[str, object]:
    document = state.model_dump(mode="json")
    value = document[collection]
    if collection == "attempts":
        assert isinstance(value, list)
        assert value
        value = value[0]
    assert isinstance(value, dict)
    return value


def _assert_checkpoint_outcomes[S: StrategyState](
    strategy: Strategy[S], event: TurnResult, *, collection: str
) -> None:
    pending_view = _view(event.invocation, "pending")
    pending = strategy.on_event(pending_view, event)
    pending_record = _record(pending, collection)
    assert pending_record["step"] == "retaining"
    assert pending_record["failure"] is None

    failed_view = _view(event.invocation, "failed")
    failed = strategy.on_event(failed_view, event)
    assert _record(failed, collection)["failure"] == (
        "implementer checkpoint declined (snapshot_failed)"
    )

    retained = strategy.on_event(_view(event.invocation, "retained"), event)
    retained_record = _record(retained, collection)
    assert retained_record["candidate"] == _CANDIDATE.model_dump(mode="json")
    assert retained_record["failure"] is None

    settled = strategy.bind(pending).decide(failed_view).state
    assert _record(settled, collection)["failure"] == (
        "implementer checkpoint declined (snapshot_failed)"
    )


def test_dynamic_consumes_pending_failed_and_retained_checkpoint_outcomes() -> None:
    subject = "h1.1.implementer"
    invocation = _invocation("implementer", subject, namespace="dynamic")
    state = DynamicStrategyState.model_validate_json(
        json.dumps(
            {
                "attempts": [
                    {
                        "attempt": {"root": _ATTEMPT.root},
                        "step": "awaiting",
                        "turn": {"role": "implementer", "serial": 0, "charge": "paid"},
                        "ready": True,
                        "plan": {
                            "kind": "implement",
                            "work_id": "h1",
                            "title": "title h1",
                            "hypothesis": "claim h1",
                            "task": "task h1",
                            "pass_criteria": "faster",
                        },
                        "sequence": 1,
                        "parent": _BASE.model_dump(mode="json"),
                        "phase": "implement",
                    }
                ]
            }
        )
    )
    strategy = DynamicStrategy(config=dynamic_config(), state=state)

    _assert_checkpoint_outcomes(strategy, _event(invocation, implemented()), collection="attempts")


def test_multi_consumes_pending_failed_and_retained_checkpoint_outcomes() -> None:
    subject = "h1.1.implementer"
    invocation = _invocation("implementer", subject, namespace="multi")
    state = MultiStrategyState.model_validate_json(
        json.dumps(
            {
                "round": {
                    "attempt": {"root": _ATTEMPT.root},
                    "step": "awaiting",
                    "turn": {"role": "implementer", "serial": 0, "charge": "paid"},
                    "ready": True,
                    "number": 1,
                    "hypothesis_id": "h1",
                    "plan_json": multi_plan_reply("h1"),
                    "base": _BASE.model_dump(mode="json"),
                    "phase": "implement",
                }
            }
        )
    )
    strategy = MultiStrategy(config=multi_config(max_rounds=1), state=state)

    _assert_checkpoint_outcomes(
        strategy, _event(invocation, multi_implemented()), collection="round"
    )


def test_single_consumes_pending_failed_and_retained_checkpoint_outcomes() -> None:
    subject = "h1.1.implementer"
    invocation = _invocation("implementer", subject, namespace="single")
    state = SingleStrategyState.model_validate_json(
        json.dumps(
            {
                "round": {
                    "attempt": {"root": _ATTEMPT.root},
                    "step": "awaiting",
                    "turn": {"role": "implementer", "serial": 0, "charge": "paid"},
                    "ready": True,
                    "number": 1,
                    "hypothesis_id": "h1",
                    "plan_json": plan_reply("h1"),
                    "base": _BASE.model_dump(mode="json"),
                    "phase": "implement",
                }
            }
        )
    )
    strategy = SingleStrategy(config=single_config(max_rounds=1), state=state)

    _assert_checkpoint_outcomes(strategy, _event(invocation, combined()), collection="round")
