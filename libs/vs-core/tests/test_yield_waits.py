"""A turn that ended waiting is waiting for its checkpoint, and says so while it does."""

from __future__ import annotations

import vs_core.api as core

from .test_intent_recovery import fixture_digest, initial_state, pending_intent
from .test_pending_suspension_contract import _invocation, _pending

OWNER = core.AttemptId(root="attempt")


def _yielded() -> core.Invocation:
    invocation = _invocation(0, OWNER)
    return core.Invocation.model_validate(
        {**invocation.model_dump(), "pending_suspension": _pending(invocation, ["job"], 50.0)}
    )


def _checkpoint(invocation: core.Invocation, phase: core.IntentPhase) -> core.Intent:
    request = core.SnapshotAndRetain(
        request_id=core.RequestId(root="checkpoint"),
        scope=invocation.scope,
        deadline_at=100.0,
        attempt=core.AttemptRef(attempt_id=OWNER, generation=0),
        retention="wip",
        invocation=invocation.invocation,
    )
    return pending_intent().model_copy(
        update={
            "request_id": request.request_id,
            "request": request,
            "payload_digest": fixture_digest(request),
            "phase": phase,
        }
    )


def _state(invocation: core.Invocation, *intents: core.Intent) -> core.CoreState:
    state = initial_state()
    return state.model_copy(
        update={
            "sessions": core.SessionsState(invocations=(invocation,)),
            "intents": state.intents.model_copy(update={"intents": intents}),
        }
    )


def test_a_yield_nothing_checkpoints_is_a_wait_nothing_will_end() -> None:
    """The judge that waited in a v12 run: no checkpoint was ever requested for it."""
    found = core.orphan_waits(_state(_yielded()))

    assert [(wait.waiter, wait.subject) for wait in found] == [
        (core.WaitKind.YIELD_PUBLICATION, "yield")
    ]


def test_a_yield_is_produced_by_its_checkpoint_while_that_runs() -> None:
    invocation = _yielded()
    running = _state(invocation, _checkpoint(invocation, core.IntentPhase.DISPATCHED))

    assert core.orphan_waits(running) == ()
    assert [wait.producer for wait in core.waits(running)] == [core.Producer.PENDING_REQUEST]


def test_a_yield_whose_checkpoint_ended_without_publishing_is_a_wait_nothing_will_end() -> None:
    invocation = _yielded()
    ended = _state(invocation, _checkpoint(invocation, core.IntentPhase.COMPLETED))

    assert [wait.waiter for wait in core.orphan_waits(ended)] == [core.WaitKind.YIELD_PUBLICATION]
