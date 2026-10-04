"""Public terminal-input output contract accepts only exact immutable disposal."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core

TIMES = st.floats(min_value=1.0, max_value=1000.0, allow_nan=False, allow_infinity=False)


def occurrence(index: int, target_kind: str = "scope") -> core.SessionInput:
    scope = core.Scope(owner=core.RunId(root="run"), generation=0)
    targets = {
        "scope": core.ScopeInputTarget(scope=scope),
        "item": core.ItemInputTarget(item_id=core.ItemId(root=f"item:{index}")),
        "invocation": core.InvocationInputTarget(
            invocation=core.InvocationRef(
                session_id=core.SessionId(root=f"session:{index}"),
                invocation_id=core.InvocationId(root=f"invocation:{index}"),
                generation=0,
            )
        ),
    }
    return core.SessionInput(
        input_id=core.InputId(root=f"input:{index}"),
        target=targets[target_kind],
        artifact=core.ArtifactRef(
            artifact_id=core.ArtifactId(root="same-payload"), digest="same-payload"
        ),
        received_at=0.0,
        sequence=index,
    )


def drop(item: core.SessionInput, at: float) -> core.InputDropped:
    return core.InputDropped(
        input_id=item.input_id, target=item.target, reason=core.InputDropReason.RUN_TERMINAL, at=at
    )


def finalize(before: core.SessionsState, at: float) -> core.SessionsChange:
    events = tuple(drop(record.input, at) for record in before.inputs if record.receipt is None)
    inputs = tuple(
        record
        if record.receipt is not None
        else record.model_copy(update={"receipt": drop(record.input, at)})
        for record in before.inputs
    )
    return core.SessionsChange(state=before.model_copy(update={"inputs": inputs}), events=events)


@given(
    records=st.lists(
        st.tuples(st.booleans(), st.sampled_from(["scope", "item", "invocation"])), max_size=10
    ),
    now_at=TIMES,
)
def test_valid_disposal_preserves_occurrences_and_previous_receipts(
    records: list[tuple[bool, str]], now_at: float
) -> None:
    inputs = tuple(
        core.InputRecord(
            input=occurrence(index, target),
            receipt=drop(occurrence(index, target), 0.0) if completed else None,
        )
        for index, (completed, target) in enumerate(records)
    )
    before = core.SessionsState(inputs=inputs)
    change = finalize(before, now_at)
    assert core.validate_terminal_inputs(before, change, now_at) is None
    assert before.inputs == inputs


@pytest.mark.parametrize(
    "flaw",
    [
        "input-id",
        "payload",
        "missing-input",
        "missing-receipt",
        "receipt-id",
        "receipt-target",
        "receipt-reason",
        "receipt-time",
        "new-delivery",
        "previous-receipt",
        "missing-event",
        "duplicate-event",
        "foreign-event",
        "event-mismatch",
        "existing-event",
        "wrong-event-type",
        "request",
        "signal",
        "sibling-charge",
        "sibling-interrupt",
    ],
)
@given(now_at=TIMES)
def test_terminal_finalization_rejects_invalid_outputs(flaw: str, now_at: float) -> None:
    pending, completed = occurrence(0), occurrence(1, "item")
    previous = drop(completed, 0.0)
    before = core.SessionsState(
        inputs=(
            core.InputRecord(input=pending),
            core.InputRecord(input=completed, receipt=previous),
        )
    )
    change = finalize(before, now_at)
    first, second = change.state.inputs
    receipt = drop(pending, now_at)
    receipt_changes = {
        "receipt-id": {"input_id": completed.input_id},
        "receipt-target": {"target": completed.target},
        "receipt-reason": {"reason": core.InputDropReason.OWNER_TERMINAL},
        "receipt-time": {"at": now_at + 1.0},
    }
    if flaw in receipt_changes:
        receipt = receipt.model_copy(update=receipt_changes[flaw])
        first = first.model_copy(update={"receipt": receipt})
        change = change.model_copy(update={"events": (receipt,)})
    elif flaw == "new-delivery":
        invocation = core.InvocationRef(
            session_id=core.SessionId(root="session"),
            invocation_id=core.InvocationId(root="invocation"),
            generation=0,
        )
        acceptance = core.Observation(
            event_id=core.EventId(root="acceptance"),
            request_id=core.RequestId(root="dispatch"),
            scope=core.Scope(owner=core.RunId(root="run"), generation=0),
            sequence=0,
            observed_at=0.0,
            status=core.ObservationStatus.SUCCEEDED,
            accepted=True,
        )
        delivered = core.InputDelivered(
            input_id=pending.input_id, invocation=invocation, observation=acceptance
        )
        first = first.model_copy(update={"receipt": delivered, "reserved_to": invocation})
        change = change.model_copy(update={"events": (delivered,)})
    elif flaw in ("input-id", "payload"):
        mutations = {
            "input-id": {"input_id": core.InputId(root="other")},
            "payload": {
                "artifact": core.ArtifactRef(
                    artifact_id=core.ArtifactId(root="replacement"), digest="replacement"
                )
            },
        }
        first = first.model_copy(update={"input": pending.model_copy(update=mutations[flaw])})
    elif flaw == "missing-input":
        change = change.model_copy(
            update={"state": change.state.model_copy(update={"inputs": (second,)})}
        )
    elif flaw == "missing-receipt":
        first = first.model_copy(update={"receipt": None})
    elif flaw == "previous-receipt":
        second = second.model_copy(update={"receipt": previous.model_copy(update={"at": now_at})})
    elif flaw in ("request", "signal"):
        work = {
            "request": {
                "requests": (
                    core.InspectRequest(
                        scope=core.Scope(owner=core.RunId(root="run"), generation=0),
                        deadline_at=100.0,
                        target=core.RequestId(root="query"),
                    ),
                )
            },
            "signal": {
                "signals": (
                    core.TurnInputsReserved(
                        invocation=core.InvocationRef(
                            session_id=core.SessionId(root="session"),
                            invocation_id=core.InvocationId(root="invocation"),
                            generation=0,
                        ),
                        input_ids=(pending.input_id,),
                    ),
                )
            },
        }
        change = change.model_copy(update=work[flaw])
    elif flaw in ("sibling-charge", "sibling-interrupt"):
        sibling = {
            "sibling-charge": {
                "run_charges": (
                    core.ChargeReceipt(
                        charge_id=core.ChargeId(root="charge"), kind=core.ChargeKind.TURN, charged=1
                    ),
                )
            },
            "sibling-interrupt": {
                "interrupts": (
                    core.InterruptClaim(
                        invocation=core.InvocationRef(
                            session_id=core.SessionId(root="session"),
                            invocation_id=core.InvocationId(root="invocation"),
                            generation=0,
                        ),
                        authority=core.RequestId(root="interrupt"),
                        refund=0,
                    ),
                )
            },
        }
        change = change.model_copy(update={"state": change.state.model_copy(update=sibling[flaw])})
    else:
        events = {
            "missing-event": (),
            "duplicate-event": (receipt, receipt),
            "foreign-event": (drop(occurrence(2), now_at),),
            "event-mismatch": (receipt.model_copy(update={"at": now_at + 1.0}),),
            "existing-event": (receipt, previous),
            "wrong-event-type": (
                core.RunEnded(result=core.RunResultProposal(outcome="cancelled", reason="done")),
            ),
        }
        change = change.model_copy(update={"events": events[flaw]})
    if flaw != "missing-input":
        change = change.model_copy(
            update={"state": change.state.model_copy(update={"inputs": (first, second)})}
        )
    with pytest.raises(core.ContractError):
        core.validate_terminal_inputs(before, change, now_at)
