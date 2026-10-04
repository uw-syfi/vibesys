"""Canonical admission authority regressions, exercised through vs_core.api."""

import json
from contextlib import suppress
from hashlib import sha256
from typing import Literal

import pytest

from vs_core.api import (
    Accepted,
    AttemptAdmitted,
    AttemptBudget,
    AttemptId,
    AttemptRef,
    AttemptRegistered,
    AttemptRequest,
    ContractValidationError,
    CoreState,
    DecisionId,
    DecisionReceipt,
    DecisionSubmitted,
    EnsureWorkspace,
    ItemId,
    KernelNotImplementedError,
    Limits,
    Rejected,
    RejectionCode,
    RunStatus,
    SchedulingState,
    Scope,
    Slot,
    StartAttempt,
    WorkspaceMode,
    WorkspacePlan,
    initial_state,
    project,
    step,
)


def admission_fixture(
    receipt_kind: str = "exact",
    slot_kind: str = "current",
    mismatch: str = "attempt_id",
) -> tuple[CoreState, AttemptAdmitted]:
    state = initial_state()
    decision_id = DecisionId(root="start")
    attempt_id = AttemptId(root="owner")
    budget = AttemptBudget()
    workspace = WorkspacePlan(mode=WorkspaceMode.ISOLATED_CHILD, base=state.run.facts.baseline)
    event = AttemptAdmitted(
        admission_id=decision_id,
        request=AttemptRequest(
            decision_id=decision_id,
            attempt_id=attempt_id,
            item_id=ItemId(root="item"),
            generation=0,
            admission_charge=1,
        ),
        workspace=workspace,
        budget=budget,
    )
    decision = StartAttempt(
        decision_id=decision_id,
        scope=Scope(owner=state.run.run_id, generation=0),
        attempt_id=attempt_id,
        item_id=event.request.item_id,
        workspace=workspace,
        budget=budget,
    )
    if receipt_kind == "mismatched":
        updates: dict[str, object] = {
            "attempt_id": AttemptId(root="foreign"),
            "item_id": ItemId(root="foreign"),
            "workspace": workspace.model_copy(update={"mode": WorkspaceMode.READ_ONLY_REVISION}),
            "budget": budget.model_copy(update={"admission_charge": 2}),
            "scope": Scope(owner=state.run.run_id, generation=1),
        }
        decision = decision.model_copy(update={mismatch: updates[mismatch]})
    receipt = DecisionReceipt(
        decision_id=decision_id,
        decision=decision,
        payload_digest=sha256(
            json.dumps(
                decision.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest(),
        feedback=(
            Rejected(
                decision_id=decision_id,
                code=RejectionCode.BUDGET,
                path=("decision",),
                detail="denied",
            )
            if receipt_kind == "rejected"
            else Accepted(decision_id=decision_id)
        ),
    )
    slots = (
        ()
        if slot_kind == "absent"
        else (
            Slot(
                attempt=AttemptRef(attempt_id=attempt_id, generation=0),
                admission_id=DecisionId(root="old") if slot_kind == "stale" else decision_id,
                admitted_at=0,
            ),
        )
    )
    state = state.model_copy(
        update={
            "run": state.run.model_copy(
                update={"receipts": () if receipt_kind == "absent" else (receipt,)}
            ),
            "scheduling": SchedulingState(slots=slots),
        }
    )
    return CoreState.model_validate_json(state.model_dump_json()), event


@pytest.mark.parametrize("receipt_kind", ["absent", "rejected", "mismatched", "exact"])
@pytest.mark.parametrize("admitted", [False, True])
@pytest.mark.parametrize("mismatch", ["attempt_id", "item_id", "workspace", "budget", "scope"])
def test_registration_requires_exact_accepted_start(
    receipt_kind: str,
    mismatch: str,
    *,
    admitted: bool,
) -> None:
    state, event = admission_fixture(receipt_kind, mismatch=mismatch)
    supplied = (
        event
        if admitted
        else AttemptRegistered(
            request=event.request,
            workspace=event.workspace,
            budget=event.budget,
            initial_sessions=event.initial_sessions,
        )
    )
    if receipt_kind != "exact":
        try:
            result = step(state, supplied)
        except ContractValidationError:
            return
        assert project(result.state).scheduling.charged == 0
        assert result.state.attempts.attempts == ()
        assert result.requests == ()
    else:
        result = step(state, supplied)
        assert project(result.state).scheduling.charged == 1
        assert any(isinstance(request, EnsureWorkspace) for request in result.requests) == admitted


def test_rejected_submitted_start_cannot_authorize_public_admission() -> None:
    state, event = admission_fixture()
    state = initial_state()
    # With no attempt budget, Scheduling rejects the submitted start.
    state = state.model_copy(
        update={"run": state.run.model_copy(update={"limits": Limits(max_attempts=0)})}
    )
    decision = StartAttempt(
        decision_id=event.request.decision_id,
        scope=Scope(owner=state.run.run_id, generation=0),
        attempt_id=event.request.attempt_id,
        item_id=event.request.item_id,
        workspace=event.workspace,
        budget=event.budget,
    )
    rejected = step(state, DecisionSubmitted(decision=decision, expected_revision=0))
    assert isinstance(rejected.events[0], Rejected)
    try:
        result = step(rejected.state, event)
    except ContractValidationError:
        return
    assert result.requests == ()
    assert project(result.state).scheduling.charged == 0


@pytest.mark.parametrize("status", [RunStatus.RUNNING, RunStatus.CLOSING])
@pytest.mark.parametrize("deadline", ["before", "at", "past"])
def test_closed_run_never_starts_new_workspace_io(
    status: RunStatus,
    deadline: Literal["before", "at", "past"],
) -> None:
    state, event = admission_fixture()
    offset = {"before": -1, "at": 0, "past": 1}[deadline]
    state = state.model_copy(
        update={
            "run": state.run.model_copy(
                update={"status": status, "now_at": state.run.deadline_at + offset}
            ),
        }
    )
    eligible = status == RunStatus.RUNNING and deadline == "before"
    boundary = None
    try:
        result = step(CoreState.model_validate_json(state.model_dump_json()), event)
    except KernelNotImplementedError as error:
        boundary = error
    if boundary is not None:
        assert not eligible
        assert boundary.subarea == "_attempt_retirement"
        return
    assert any(isinstance(request, EnsureWorkspace) for request in result.requests) == eligible


def test_forged_registrations_do_not_exceed_attempt_budget() -> None:
    state = initial_state().model_copy(
        update={
            "run": initial_state().run.model_copy(
                update={"limits": Limits(max_attempts=1)},
            )
        }
    )
    for identity in ("first", "second"):
        _, event = admission_fixture("absent")
        event = AttemptRegistered(
            request=event.request.model_copy(
                update={
                    "decision_id": DecisionId(root=identity),
                    "attempt_id": AttemptId(root=identity),
                }
            ),
            workspace=event.workspace,
            budget=event.budget,
        )
        with suppress(ContractValidationError):
            state = step(state, event).state
    assert project(state).scheduling.charged == 0
