"""Every dynamic failure can cross both operator event transports."""

import pytest

import vs_proto.api as p
from vibesys.candidates.api import InputFailureKind, NotConfigured
from vibesys.events import InputMeasurementFailureKind, WorkstreamFailureData, WorkstreamFailureKind
from vibesys.orchestration.dynamic.core_policy.api import project_run_snapshot
from vibesys.orchestration.dynamic.search.api import (
    AgentName,
    Blocker,
    BlockerKind,
    DropReason,
    Failure,
    FailureKind,
    FinishedWork,
    HypothesisRecord,
    RoundRecord,
    RunSnapshot,
    WorkKind,
    WorkPlan,
)
from vs_core.api import (
    AttemptId,
    CheckpointDecline,
    FailureReason,
    ObservationStatus,
    RejectionCode,
    RevisionRef,
    WaitRefusal,
)


def failure_for(kind: FailureKind) -> Failure:
    fields = {
        FailureKind.PARENT_UNVERIFIED: {"revision": RevisionRef.of_git_commit("parent")},
        FailureKind.REFUSED: {"cause": RejectionCode.BUDGET},
        FailureKind.ENDED: {"ended": next(iter(FailureReason))},
        FailureKind.TURN_FAILED: {"role": AgentName.IMPLEMENTER},
        FailureKind.INVALID_REPLY: {"role": AgentName.IMPLEMENTER},
        FailureKind.RENDER_FAILED: {"role": AgentName.IMPLEMENTER},
        FailureKind.WAIT_REFUSED: {
            "role": AgentName.IMPLEMENTER,
            "refusal": next(iter(WaitRefusal)),
        },
        FailureKind.CHECKPOINT_DECLINED: {"declined": next(iter(CheckpointDecline))},
        FailureKind.NO_TRUSTED_EVIDENCE: {"status": next(iter(ObservationStatus))},
        FailureKind.TURN_LIMIT: {
            "turns": 2,
            "limit": 2,
            "blocker": Blocker(kind=BlockerKind.FAILED),
        },
        FailureKind.REPEATED_TURN: {"blocker": Blocker(kind=BlockerKind.FAILED)},
        FailureKind.NOT_MEASURED: {"dropped": DropReason.STOPPED},
    }
    return Failure.model_validate({"kind": kind, **fields.get(kind, {})})


def projected_failure(failure: Failure) -> WorkstreamFailureData:
    snapshot = RunSnapshot(
        baseline=NotConfigured(),
        running=(),
        leading=None,
        hypotheses=(
            HypothesisRecord(
                hypothesis_id="w1",
                title="",
                hypothesis="claim",
                first_sequence=1,
                rounds=(
                    RoundRecord(sequence=1, attempt=AttemptId(root="attempt-1"), failure=failure),
                ),
            ),
        ),
        finished=(
            FinishedWork(
                sequence=1,
                plan=WorkPlan(kind=WorkKind.IMPLEMENT, work_id="w1"),
                parent=RevisionRef.of_git_commit("parent"),
                failure=failure,
            ),
        ),
    )
    summary = project_run_snapshot(snapshot, experiment_revision=1).rounds[0].last_error
    assert summary is not None
    return WorkstreamFailureData(kind=summary.kind, text=summary.operator_text or summary.text)


@pytest.mark.regression
@pytest.mark.parametrize("kind", FailureKind)
def test_every_dynamic_failure_has_a_supported_legacy_and_native_event_reason(
    kind: FailureKind,
) -> None:
    failure = projected_failure(failure_for(kind))
    assert failure.kind in WorkstreamFailureKind
    assert p.WorkstreamFailureKind[failure.kind.name] != p.WorkstreamFailureKind.UNSPECIFIED


@pytest.mark.regression
@pytest.mark.parametrize("reason", DropReason)
def test_unmeasured_candidates_keep_the_drop_reason_and_stop_cleanly(reason: DropReason) -> None:
    failure = projected_failure(Failure(kind=FailureKind.NOT_MEASURED, dropped=reason))
    assert failure.kind is (
        WorkstreamFailureKind.STOPPED
        if reason is DropReason.STOPPED
        else WorkstreamFailureKind.ENDED
    )
    assert reason.value in failure.text


@pytest.mark.parametrize("kind", InputFailureKind)
def test_every_input_failure_has_supported_legacy_and_native_reasons(
    kind: InputFailureKind,
) -> None:
    event_kind = InputMeasurementFailureKind(kind.value)
    assert p.InputFailureKind[event_kind.name] != p.InputFailureKind.UNSPECIFIED
