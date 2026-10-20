"""Recovery never waits without bound on a check nobody will answer.

A run was stopped mid-round with evaluation jobs in flight and resumed. Recovery sat in phase
``recovering`` for good, with no error: cancels of jobs that ended long ago (each answered
``cancelled`` with the job released, and no manifest, since a cancel lists no children) were
inspected, the executor returned the same facts, and nothing in flight could move the check
again; an adoption that had not reached its verification, and the blocked polls of released
jobs, sat behind them.

Every test drives core through its public API (`step`) and answers each request core sends the
way an executor does: from what it did, sealed, begun, never begun, unknown, retryably failed,
or not at all. A recovery that is not ready ends blocked on a named record; it never rests in
``recovering`` with nothing in flight (that rest is the hang).
"""

from __future__ import annotations

from enum import StrEnum

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_sim.api.testing import examples

from .test_request_recovery import fixture_digest, observed, recovering_state, reload

ADMISSION = core.DecisionId(root="000008/start.1")
JOB = core.ResourceId(root="vs-job")
EPOCH = 2
START = 1000.0
LATER = 1.0e7
"""A core time past every bound a recovery sets: the clock moves here when nothing is in flight."""
NO_LIMIT = 1.7976931348623157e308
"""An adoption's limit: the run's, and the run has none."""

ATTEMPT_ID = core.AttemptId(root="attempt:h8:8")
ATTEMPT = core.Scope(owner=ATTEMPT_ID, generation=0)
RUN = core.Scope(owner=core.initial_state().run.run_id, generation=0)
SELECTION = core.TrustedBaseline(
    revision=core.RevisionRef(revision_id=core.RevisionId(root="rev"), digest="git-commit:rev")
)


def _record(
    request: core.Request,
    lifecycle: core.LifecycleClass,
    phase: core.RequestPhase,
    *,
    until: float,
    observation: core.Observation | None = None,
) -> core.RequestRecord:
    assert request.request_id is not None
    failure = (
        core.Failed(reason=core.FailureReason.TIMED_OUT, limit_at=until)
        if phase == core.RequestPhase.BLOCKED
        else None
    )
    return core.RequestRecord(
        request_id=request.request_id,
        request=request,
        payload_digest=fixture_digest(request),
        lifecycle=lifecycle,
        phase=phase,
        until=core.Until(at=until),
        observation=observation,
        failure=failure,
        addressed_by=(),
    )


class Outstanding(StrEnum):
    """What a stopped run left open: the shapes the real run held, and their neighbors."""

    CANCEL_FINISHED = "cancel-finished"
    CANCEL_UNRELEASED = "cancel-unreleased"
    CANCEL_DISPATCHED = "cancel-dispatched"
    CANCEL_RECONCILING = "cancel-reconciling"
    CANCEL_PREPARED = "cancel-prepared"
    VERIFY_PREPARED = "verify-prepared"
    VERIFY_DISPATCHED = "verify-dispatched"
    VERIFY_RECONCILING = "verify-reconciling"
    ADOPT_DISPATCHED = "adopt-dispatched"
    ADOPT_RECONCILING = "adopt-reconciling"
    POLL_BLOCKED = "poll-blocked"
    WRITE_FINISHED_UNPROVEN = "write-finished-unproven"


CANCELS = frozenset(kind for kind in Outstanding if kind.startswith("cancel"))
ADOPTIONS = frozenset(kind for kind in Outstanding if kind.startswith(("verify", "adopt")))
OPEN_CANCELS = frozenset({Outstanding.CANCEL_DISPATCHED, Outstanding.CANCEL_RECONCILING})
"""The cancels recovery asks its executor about: a finished one answers its own check."""
SETTLED_AT_START = frozenset(
    {Outstanding.CANCEL_FINISHED, Outstanding.CANCEL_UNRELEASED, Outstanding.POLL_BLOCKED}
)


def _cancelled_job(
    record: core.RequestRecord, sequence: int = 1, *, released: bool = True
) -> core.Observation:
    """What the executor of a cancel reports: accepted, the job released, no children listed."""
    return observed(
        record,
        sequence=sequence,
        status=core.ObservationStatus.CANCELLED,
        resource_id=JOB,
        accepted=True,
        terminal=True,
        released=released,
        children_complete=False,
        admission_id=ADMISSION,
    )


def outstanding(kind: Outstanding, number: int) -> core.RequestRecord:
    """One record of ``kind``, named by ``number``."""
    name = f"{kind.value}:{number}"
    phases = {
        "dispatched": core.RequestPhase.DISPATCHED,
        "reconciling": core.RequestPhase.RECONCILING,
        "prepared": core.RequestPhase.PREPARED,
    }
    phase = phases.get(kind.value.split("-")[-1], core.RequestPhase.COMPLETED)
    request_id = core.RequestId(root=name)
    if kind in CANCELS:
        cancel = core.CancelOwnedJob(
            request_id=request_id, scope=ATTEMPT, admission_id=ADMISSION, resource_id=JOB
        )
        # A cancel not yet sent waits for recovery with the limit it was given, past this test's
        # clock: what the clock does to a request recovery holds back is not this test's subject.
        until = 10 * LATER if phase == core.RequestPhase.PREPARED else 60.0
        bare = _record(cancel, core.LifecycleClass.IDEMPOTENT_WRITE, phase, until=until)
        if kind == Outstanding.CANCEL_FINISHED:
            return bare.model_copy(update={"observation": _cancelled_job(bare)})
        if kind == Outstanding.CANCEL_UNRELEASED:
            # "cancellation requested, termination not yet observed": the job is its owner's.
            return bare.model_copy(update={"observation": _cancelled_job(bare, released=False)})
        return bare
    if kind in ADOPTIONS:
        verifying = kind.startswith("verify")
        adoption: core.AdoptRevision | core.VerifyAdoption = (
            core.VerifyAdoption(request_id=request_id, scope=RUN, selection=SELECTION)
            if verifying
            else core.AdoptRevision(request_id=request_id, scope=RUN, selection=SELECTION)
        )
        lifecycle = core.LifecycleClass.QUERY if verifying else core.LifecycleClass.IDEMPOTENT_WRITE
        return _record(adoption, lifecycle, phase, until=NO_LIMIT)
    if kind == Outstanding.POLL_BLOCKED:
        poll = core.ObserveOwnedJob(request_id=request_id, scope=ATTEMPT, resource_id=JOB)
        return _record(poll, core.LifecycleClass.QUERY, core.RequestPhase.BLOCKED, until=10.0)
    write = core.CloseAttemptScope(
        request_id=request_id,
        scope=ATTEMPT,
        attempt=core.AttemptRef(attempt_id=ATTEMPT_ID, generation=0),
        admission_id=ADMISSION,
    )
    bare = _record(
        write, core.LifecycleClass.IDEMPOTENT_WRITE, core.RequestPhase.COMPLETED, until=60.0
    )
    return bare.model_copy(update={"observation": observed(bare, admission_id=ADMISSION)})


class Executor(StrEnum):
    """How the executor answers core's inspection of a request."""

    SEALED = "sealed"
    UNKNOWN = "unknown"
    NEVER_BEGAN = "never-began"
    BEGUN = "begun"
    RETRYABLE = "retryable"
    SILENT = "silent"


UNREACHABLE = (Executor.SILENT, Executor.RETRYABLE)


def _sealed(target: core.RequestRecord, sequence: int) -> core.Observation:
    """The result the executor sealed for ``target``: what it would have reported live."""
    if target.observation is not None:
        return target.observation  # an executor that already answered says the same again
    if isinstance(target.request, core.CancelOwnedJob):
        return _cancelled_job(target, sequence)
    return observed(
        target,
        sequence=sequence,
        status=core.ObservationStatus.SUCCEEDED,
        accepted=True,
        terminal=True,
        released=True,
        children_complete=True,
        admission_id=target.request.admission_id,
    )


def _inspected(
    target: core.RequestRecord, executor: Executor, sequence: int
) -> core.TargetObservation | None:
    """What the executor says of ``target``, or None when it has nothing to say."""
    previous = 0 if target.observation is None else target.observation.sequence
    admission = target.request.admission_id
    if target.phase == core.RequestPhase.COMPLETED and executor not in UNREACHABLE:
        # A request that finished is closed: its executor can only say again what it said.
        return core.TargetObservation(observation=_sealed(target, previous))
    match executor:
        case Executor.SEALED:
            return core.TargetObservation(observation=_sealed(target, previous + sequence))
        case Executor.UNKNOWN | Executor.BEGUN:
            unknown = observed(target, sequence=previous + sequence, admission_id=admission)
            reissue = core.ReissueProof.BEGUN_UNSEALED if executor == Executor.BEGUN else None
            return core.TargetObservation(observation=unknown, reissue=reissue)
        case Executor.NEVER_BEGAN:
            refusal = observed(
                target,
                sequence=previous + sequence,
                status=core.ObservationStatus.REJECTED,
                terminal=True,
                released=True,
                children_complete=True,
                admission_id=admission,
            )
            return core.TargetObservation(
                observation=refusal, reissue=core.ReissueProof.NEVER_BEGAN
            )
        case Executor.RETRYABLE | Executor.SILENT:
            return None
        case _:
            raise AssertionError(executor)


def _own(request: core.Request, sequence: int, *, failed: bool) -> core.Observation:
    """The observation of a request core sent: it ran, or the executor failed to run it."""
    assert request.request_id is not None
    return core.Observation(
        event_id=core.EventId(root=f"answer:{request.request_id.root}:{sequence}"),
        request_id=request.request_id,
        scope=request.scope,
        sequence=sequence,
        observed_at=START + sequence,
        status=core.ObservationStatus.FAILED if failed else core.ObservationStatus.SUCCEEDED,
        accepted=not failed,
        terminal=not failed,
        released=not failed,
        children_complete=not failed,
        admission_id=request.admission_id,
    )


def _record_of(state: core.CoreState, request_id: core.RequestId) -> core.RequestRecord:
    return next(row for row in state.records.records if row.request_id == request_id)


def _settle(
    state: core.CoreState, requests: tuple[core.Request, ...], executor: Executor
) -> core.CoreState:
    """Authorize and answer every request core sends, until it sends no more."""
    pending = list(requests)
    answered = 0
    for _ in range(400):  # a bound on the harness, never on time
        if not pending:
            return state
        request = pending.pop(0)
        if request.request_id is None:
            continue
        answered += 1
        state = core.step(state, core.DispatchAuthorized(request_id=request.request_id)).state
        facts = None
        if isinstance(request, core.InspectRequest) and request.resource_id is None:
            facts = _inspected(_record_of(state, request.target), executor, answered)
            if facts is None and executor == Executor.SILENT:
                continue
        failed = executor == Executor.RETRYABLE and isinstance(request, core.InspectRequest)
        event = core.RequestObserved(
            observation=_own(request, answered, failed=failed), target=facts
        )
        result = core.step(state, event)
        state, pending = result.state, [*pending, *result.requests]
    message = "core kept asking: it never settled"
    raise AssertionError(message)


def recover(records: list[core.RequestRecord], executor: Executor) -> core.CoreState:
    """Resume a run holding ``records``: recovery starts, the executor answers, time passes.

    While a check is pending, time passes (to a point no bound outlasts) and whatever that
    makes core send is answered. A recovery still ``recovering`` at the end waits on nothing.
    """
    started = core.step(
        reload(recovering_state(*records)), core.RecoveryStarted(epoch=EPOCH, now_at=START)
    )
    state = _settle(started.state, started.requests, executor)
    for _ in range(4):
        if "pending" not in checks_of(state).values():
            break
        moved = core.step(state, core.ClockAdvanced(now_at=LATER))
        state = _settle(moved.state, moved.requests, executor)
    return state


def checks_of(state: core.CoreState) -> dict[str, str]:
    """Each recovery check's resolution, by the name of its target."""
    return {check.target.root: check.resolution for check in state.records.recovery.checks}


def test_the_cancels_of_finished_jobs_do_not_hold_a_resumed_run_in_recovery() -> None:
    """Regression: three cancels, an adoption's verification and twelve blocked polls.

    At the merge base recovery rested in ``recovering``: the cancels' inspections returned the
    same facts (no manifest), so each check stayed pending with nothing in flight.
    """
    cancels = [outstanding(Outstanding.CANCEL_FINISHED, number) for number in range(3)]
    polls = [outstanding(Outstanding.POLL_BLOCKED, number) for number in range(12)]
    verify = outstanding(Outstanding.VERIFY_PREPARED, 0)
    state = recover([*cancels, verify, *polls], Executor.SEALED)
    assert state.records.recovery.phase == core.RecoveryPhase.READY
    resolutions = checks_of(state)
    assert "pending" not in resolutions.values()
    assert {resolutions[record.request_id.root] for record in cancels} == {"terminal"}
    assert resolutions[verify.request_id.root] == "safe-prepared"
    # Nothing is asked of the executor about a cancel that finished: its answer is its own.
    named = {record.request_id for record in cancels}
    assert not any(
        isinstance(row.request, core.InspectRequest) and row.request.target in named
        for row in state.records.records
    )


def sealed_adoption(kind: Outstanding, executor: Executor) -> bool:
    """Whether the executor tells recovery the adoption's result: it sealed one before the stop."""
    return executor == Executor.SEALED and kind != Outstanding.VERIFY_PREPARED


@pytest.mark.parametrize("kind", sorted(ADOPTIONS))
@pytest.mark.parametrize("executor", list(Executor))
def test_an_outstanding_adoption_goes_out_again_unless_its_result_was_sealed(
    kind: Outstanding, executor: Executor
) -> None:
    """Adoption restores the root to the selected revision and verifies it: it can be redone.

    A result the executor sealed is taken as it is (the effect never runs twice). Without one,
    whether the executor does not know, proves nothing began, or cannot be reached, the request
    goes out again under its own identity, and the adoption goes on from the new answer.
    """
    record = outstanding(kind, 0)
    state = recover([record], executor)
    assert state.records.recovery.phase == core.RecoveryPhase.READY
    row = _record_of(state, record.request_id)
    if sealed_adoption(kind, executor):
        assert checks_of(state) == {record.request_id.root: "terminal"}
        assert row.phase == core.RequestPhase.COMPLETED
        return
    assert checks_of(state) == {record.request_id.root: "safe-prepared"}
    assert row.phase == core.RequestPhase.PREPARED
    assert row.observation is None
    # It is dispatched like any request.
    sent = core.step(state, core.DispatchAuthorized(request_id=record.request_id)).state
    assert _record_of(sent, record.request_id).phase == core.RequestPhase.DISPATCHED


@pytest.mark.parametrize("kind", sorted(OPEN_CANCELS))
def test_a_cancel_the_executor_cannot_account_for_is_settled_as_unknown(
    kind: Outstanding,
) -> None:
    """A cancelled job's result is discarded whatever it was, so unknown is settled, and says so."""
    record = outstanding(kind, 0)
    state = recover([record], Executor.UNKNOWN)
    assert state.records.recovery.phase == core.RecoveryPhase.READY
    assert checks_of(state) == {record.request_id.root: "unknown"}


@pytest.mark.parametrize("kind", sorted(OPEN_CANCELS))
@pytest.mark.parametrize("executor", UNREACHABLE)
def test_a_cancel_whose_executor_cannot_be_reached_blocks_recovery_on_that_record(
    kind: Outstanding, executor: Executor
) -> None:
    """The inspection ends unanswered at its bound: recovery blocks, naming the cancel."""
    record = outstanding(kind, 0)
    state = recover([record], executor)
    assert state.records.recovery.phase == core.RecoveryPhase.BLOCKED
    assert checks_of(state) == {record.request_id.root: "blocked"}


def test_an_effect_that_finished_and_left_no_proof_blocks_recovery_on_that_record() -> None:
    """Its result matters and nothing can redo it: blocked and named, not a wait with no end."""
    record = outstanding(Outstanding.WRITE_FINISHED_UNPROVEN, 0)
    state = recover([record], Executor.SEALED)
    assert state.records.recovery.phase == core.RecoveryPhase.BLOCKED
    assert checks_of(state) == {record.request_id.root: "blocked"}


@settings(max_examples=examples(pr=60, nightly=600), deadline=None)
@given(
    kinds=st.lists(st.sampled_from(list(Outstanding)), min_size=1, max_size=6),
    executor=st.sampled_from(list(Executor)),
)
def test_recovery_never_rests_waiting_on_a_check_nobody_answers(
    kinds: list[Outstanding], executor: Executor
) -> None:
    """Whatever was outstanding at the stop, and however the executor answers.

    Recovery ends ready, or blocked on records it names (the run then ends blocked, whatever else
    is pending): only a cancel its executor could not be reached about, or an effect that
    finished without proof, blocks it. What the policy says
    of a kind holds for every mix: an adoption is sent again unless its result was sealed, and a
    cancel that finished, or a poll core ended, settles its own check.
    """
    records = [outstanding(kind, number) for number, kind in enumerate(kinds)]
    state = recover(records, executor)
    resolutions = checks_of(state)
    blockers = {
        record.request_id.root
        for kind, record in zip(kinds, records, strict=True)
        if (kind in OPEN_CANCELS and executor in UNREACHABLE)
        or kind == Outstanding.WRITE_FINISHED_UNPROVEN
    }
    if blockers:
        assert state.records.recovery.phase == core.RecoveryPhase.BLOCKED
        assert blockers & {root for root, how in resolutions.items() if how == "blocked"}
        return
    assert state.records.recovery.phase == core.RecoveryPhase.READY
    assert "pending" not in resolutions.values()
    for kind, record in zip(kinds, records, strict=True):
        resolution = resolutions[record.request_id.root]
        if kind in ADOPTIONS:
            sealed = sealed_adoption(kind, executor)
            assert resolution == ("terminal" if sealed else "safe-prepared")
        if kind in SETTLED_AT_START:
            assert resolution == "terminal"
