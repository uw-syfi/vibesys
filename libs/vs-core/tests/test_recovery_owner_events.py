"""The owner events an executor returns beside its report reach the fold from the log record.

A plain turn is answered with two events: the request's observation, which the ledger commits and
forwards, and the owner's `TurnObserved`, the only carrier of the reply. The record of a finished
step holds both, and the fold applies them in the order the live shell does.
"""

from __future__ import annotations

import pytest

import vs_core.api as core
from vs_core.api.recovery import Finished, Run, finished_message, fold, started_message
from vs_proto.api import Oneof, Record, Timestamp

from .test_owner_event_precedence import REPLY, SCHEMA, dispatched, observation
from .test_session_turns import invocation, reload_step


def record(seq: int, body: Oneof) -> Record:
    return Record(seq=seq, epoch=1, at=Timestamp(seconds=seq), clock=float(seq), body=body)


def log_of(
    request: core.DispatchTurn, finished: Finished, *, tag: core.RequestId | None = None
) -> list[Record]:
    assert request.request_id is not None
    tag = tag or request.request_id
    run = Run(step=request, tag=tag, attempt=1, deadline_at=100.0)
    return [
        record(1, Oneof("step_started", started_message(run))),
        record(2, Oneof("step_finished", finished_message(tag, 1, finished))),
    ]


def reply_event(request: core.DispatchTurn, seen: core.Observation) -> core.TurnObserved:
    return core.TurnObserved(
        invocation=invocation(request.turn),
        observation=seen,
        output_schema=SCHEMA,
        output_json=REPLY,
    )


def test_the_fold_applies_the_report_and_then_each_owner_event_as_the_live_shell_does() -> None:
    state, request = dispatched()
    seen = observation(request)
    owner = reply_event(request, seen)
    report = core.RequestObserved(observation=seen)
    live = reload_step(reload_step(state, report).state, owner).state

    folded = fold(
        log_of(request, Finished(observation=seen, observed=report, owner_events=(owner,))), state
    )

    assert folded.sessions == live.sessions
    # The fold starts the attempt the live shell had dispatched before this seed: it stamps when.
    assert (
        tuple(row.model_copy(update={"dispatched_at": None}) for row in folded.records.records)
        == live.records.records
    )
    assert folded.steps.attempts == ()


def test_without_the_owner_event_the_reply_never_reaches_the_turn() -> None:
    """The ledger withholds its own `TurnObserved` while a reply is owed, so the event is needed."""
    state, request = dispatched()
    seen = observation(request)
    report = core.RequestObserved(observation=seen)
    with_reply = fold(
        log_of(
            request,
            Finished(observation=seen, observed=report, owner_events=(reply_event(request, seen),)),
        ),
        state,
    )
    without = fold(log_of(request, Finished(observation=seen, observed=report)), state)

    assert without.sessions != with_reply.sessions


def foreign(seen: core.Observation) -> core.Observation:
    return seen.model_copy(
        update={"scope": core.Scope(owner=core.RunId(root="other"), generation=9)}
    )


@pytest.mark.parametrize("where", ["observation", "owner-event"])
def test_a_fact_that_names_another_request_is_refused_not_applied(where: str) -> None:
    state, request = dispatched()
    seen = observation(request)
    report = core.RequestObserved(observation=seen)
    owner = reply_event(request, seen)
    if where == "observation":
        finished = Finished(observation=foreign(seen))
    else:
        finished = Finished(
            observation=seen,
            observed=report,
            owner_events=(owner.model_copy(update={"observation": foreign(seen)}),),
        )
    with pytest.raises(ValueError, match="another request"):
        fold(log_of(request, finished), state)
