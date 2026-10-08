"""Recorded scheduler traces are well formed, sanitized, and replay faithfully."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from vs_slurm.api import (
    CANCEL_REACTIONS,
    LIFETIMES,
    ClusterObservation,
    ClusterUnknown,
    ManualClock,
    SchedulerTrace,
    SlurmBatchRequest,
    SlurmBatchStage,
    SlurmCluster,
    SlurmConfig,
    SlurmConnectorTransport,
    SlurmJobRunner,
    SlurmJobStatus,
    SlurmPhase,
    SlurmRawState,
    TraceConnector,
    TraceStep,
    phase_of,
)

if TYPE_CHECKING:
    from pathlib import Path

_SUBMIT_BUDGET = 19
_POLL_BUDGET = 3
_ALL = {**LIFETIMES, **CANCEL_REACTIONS}
_KNOWN_STATES = {"PENDING", "RUNNING", "COMPLETING", "COMPLETED", "CANCELLED by 0"}
_SAFE_TEXT = re.compile(r"[A-Za-z0-9+:|._ -]*")
_PHASE_ORDER = {
    SlurmPhase.PENDING: 0,
    SlurmPhase.RUNNING: 1,
    SlurmPhase.COMPLETING: 2,
    SlurmPhase.ENDED: 3,
}


def _runner(
    tmp_path: Path, lifetime: SchedulerTrace, running: SchedulerTrace
) -> tuple[SlurmJobRunner, TraceConnector, ManualClock]:
    clock = ManualClock()
    connector = TraceConnector(
        tmp_path / "connector",
        clock=clock,
        lifetime=lifetime,
        on_cancel_pending=CANCEL_REACTIONS["cancel-pending"],
        on_cancel_running=running,
    )
    remote = tmp_path / "remote"
    remote.mkdir()
    runner = SlurmJobRunner(
        SlurmConfig(
            name="replay",
            remote_workspace_root=str(remote),
            transport=SlurmConnectorTransport(kind="connector", command=("trace-connector",)),
        ),
        process=connector,
        clock=clock.now,
        pause=clock.advance,
    )
    return runner, connector, clock


@pytest.mark.parametrize("trace", _ALL.values(), ids=list(_ALL))
def test_recorded_traces_carry_only_states_codes_reasons_and_seconds(
    trace: SchedulerTrace,
) -> None:
    """Nothing identifying a cluster, user, node, partition, account or path is recorded."""
    for step in trace.steps:
        assert {step.queue_state, step.accounting_state} - {None} <= _KNOWN_STATES
        for text in (step.reason, step.exit_code):
            assert text is None or _SAFE_TEXT.fullmatch(text)
    assert "/" not in trace.provenance
    assert not re.search(r"\b\d{5,}\b", trace.provenance)


@pytest.mark.parametrize("trace", LIFETIMES.values(), ids=list(LIFETIMES))
@given(gaps=st.lists(st.floats(0, 60, allow_nan=False), max_size=40))
def test_a_lifetime_never_goes_backwards_for_any_polling_schedule(
    trace: SchedulerTrace, gaps: list[float]
) -> None:
    """Whatever the poll cadence, readings follow pending, running, completing, ended."""
    elapsed = 0.0
    seen = []
    for gap in gaps:
        elapsed += gap
        step = trace.at(elapsed)
        raw = step.queue_state or step.accounting_state
        assert raw is not None
        seen.append(_PHASE_ORDER[phase_of(raw.split()[0])])
    assert seen == sorted(seen)


def test_a_trace_must_start_at_zero_in_order_and_name_no_unknown_field() -> None:
    step = TraceStep(at_seconds=0.0, queue_state=None, accounting_state="COMPLETED")
    late = step.model_copy(update={"at_seconds": 5.0})
    with pytest.raises(ValidationError):
        SchedulerTrace(name="x", provenance="x", steps=(late,))
    with pytest.raises(ValidationError):
        SchedulerTrace(name="x", provenance="x", steps=(step, late, step))
    with pytest.raises(ValidationError):
        TraceStep.model_validate(
            {"at_seconds": 0, "queue_state": None, "accounting_state": None, "host": "h"}
        )


def test_the_runner_reads_the_recorded_scheduler_over_time(tmp_path: Path) -> None:
    """Pending for 95 s, running, tearing down, then ended: the readings the cluster showed."""
    runner, connector, clock = _runner(
        tmp_path, LIFETIMES["pending-then-running"], CANCEL_REACTIONS["cancel-running"]
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    handle = runner.submit_batch(
        SlurmBatchRequest(
            workspace=workspace, stages=(SlurmBatchStage(name="work", command=("true",)),)
        )
    )
    job = handle.job.job_id
    submitted_at = connector.submitted_at(job)
    phases = []
    for at in (0, 94, 96, 262, 270, 300):
        clock.advance(max(0.0, submitted_at + at - clock.now()))
        phases.append(runner.inspect_job(job).phase)
    assert phases == [
        SlurmPhase.PENDING,
        SlurmPhase.PENDING,
        SlurmPhase.RUNNING,
        SlurmPhase.RUNNING,
        SlurmPhase.ENDED,
        SlurmPhase.ENDED,
    ]


@pytest.mark.parametrize("running", ["cancel-running", "cancel-running-slow-teardown"])
def test_one_scancel_of_a_running_job_ends_it_in_accounting_at_once(
    tmp_path: Path, running: str
) -> None:
    """The queue shows COMPLETING for tens of seconds, but the reading is already ended."""
    runner, connector, clock = _runner(
        tmp_path, SchedulerTrace.running_forever(), CANCEL_REACTIONS[running]
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    handle = runner.submit_batch(
        SlurmBatchRequest(
            workspace=workspace, stages=(SlurmBatchStage(name="work", command=("true",)),)
        )
    )
    clock.advance(5)
    assert runner.inspect_job(handle.job.job_id).phase is SlurmPhase.RUNNING
    runner.cancel_batch(handle)
    reading = runner.inspect_job(handle.job.job_id)
    assert reading.phase is SlurmPhase.ENDED
    assert reading.status.value == "cancelled"
    assert connector.scancels() == 1


def test_a_cancelled_operation_does_not_resend_scancel_while_the_job_tears_down(
    tmp_path: Path,
) -> None:
    """Mechanism: the reconciler re-sent scancel on every inspection of a COMPLETING job."""
    stuck = SchedulerTrace(
        name="stuck",
        provenance="queue shows COMPLETING while accounting lags",
        steps=(
            TraceStep(
                at_seconds=0.0, queue_state="COMPLETING", accounting_state="RUNNING", reason="None"
            ),
        ),
    )
    runner, connector, clock = _runner(tmp_path, SchedulerTrace.running_forever(), stuck)
    cluster = SlurmCluster(runner, state_root=tmp_path / "ids")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    request = SlurmBatchRequest(
        workspace=workspace, stages=(SlurmBatchStage(name="work", command=("true",)),)
    )
    cluster.submit(request, operation_id="op")
    clock.advance(5)
    cluster.cancel("op")
    for _ in range(5):
        clock.advance(10)
        cluster.inspect("op")
    assert connector.scancels() == 1


@pytest.mark.parametrize("lifetime", LIFETIMES)
def test_replayed_submit_and_poll_stay_within_the_command_budget(
    tmp_path: Path, lifetime: str
) -> None:
    """Every poll, whatever the scheduler shows, costs at most three remote commands."""
    runner, connector, clock = _runner(
        tmp_path, LIFETIMES[lifetime], CANCEL_REACTIONS["cancel-running"]
    )
    cluster = SlurmCluster(runner, state_root=tmp_path / "ids")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    request = SlurmBatchRequest(
        workspace=workspace,
        stages=(
            SlurmBatchStage(name="accuracy", command=("true",)),
            SlurmBatchStage(name="benchmark", command=("true",)),
        ),
    )
    before = len(connector.commands())
    cluster.submit(request, operation_id="op")
    assert len(connector.commands()) - before <= _SUBMIT_BUDGET
    for _ in range(40):
        clock.advance(10)
        before = len(connector.commands())
        cluster.inspect("op")
        assert len(connector.commands()) - before <= _POLL_BUDGET


# Every state the squeue and sacct manuals document, with the phase and public status a
# job whose only evidence is that state (exit code 0:0) must read as. A literal list, so
# a state added to SlurmRawState without a decision here fails the exhaustiveness test.
_P, _R, _C, _E = (
    SlurmPhase.PENDING,
    SlurmPhase.RUNNING,
    SlurmPhase.COMPLETING,
    SlurmPhase.ENDED,
)
_JS = SlurmJobStatus
_DOCUMENTED_STATES = {
    "BOOT_FAIL": (_E, _JS.FAILED),
    "CANCELLED": (_E, _JS.CANCELLED),
    "CANCELLED by 0": (_E, _JS.CANCELLED),
    "COMPLETED": (_E, _JS.COMPLETED),
    "COMPLETING": (_C, _JS.RUNNING),
    "CONFIGURING": (_P, _JS.PENDING),
    "DEADLINE": (_E, _JS.FAILED),
    "FAILED": (_E, _JS.FAILED),
    "NODE_FAIL": (_E, _JS.FAILED),
    "OUT_OF_MEMORY": (_E, _JS.FAILED),
    "PENDING": (_P, _JS.PENDING),
    "PREEMPTED": (_E, _JS.CANCELLED),
    "REQUEUE_FED": (_P, _JS.PENDING),
    "REQUEUE_HOLD": (_P, _JS.PENDING),
    "REQUEUED": (_P, _JS.PENDING),
    "RESIZING": (_R, _JS.RUNNING),
    "RESV_DEL_HOLD": (_P, _JS.PENDING),
    "REVOKED": (_E, _JS.CANCELLED),
    "RUNNING": (_R, _JS.RUNNING),
    "SIGNALING": (_R, _JS.RUNNING),
    "SPECIAL_EXIT": (_E, _JS.FAILED),
    "STAGE_OUT": (_R, _JS.RUNNING),
    "STOPPED": (_R, _JS.RUNNING),
    "SUSPENDED": (_R, _JS.RUNNING),
    "TIMEOUT": (_E, _JS.FAILED),
}


def _inspect_single_state(
    tmp_path: Path, step: TraceStep
) -> tuple[ClusterObservation | ClusterUnknown, TraceConnector]:
    trace = SchedulerTrace(
        name="single",
        provenance="synthetic: the scheduler shows one state from the start",
        steps=(step,),
    )
    runner, connector, _clock = _runner(tmp_path, trace, CANCEL_REACTIONS["cancel-running"])
    cluster = SlurmCluster(runner, state_root=tmp_path / "ids")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    request = SlurmBatchRequest(
        workspace=workspace, stages=(SlurmBatchStage(name="work", command=("true",)),)
    )
    cluster.submit(request, operation_id="op")
    observed = cluster.inspect("op")
    assert isinstance(observed, ClusterObservation | ClusterUnknown), observed
    return observed, connector


def _read_single_state(tmp_path: Path, step: TraceStep) -> ClusterObservation:
    observed, _connector = _inspect_single_state(tmp_path, step)
    assert isinstance(observed, ClusterObservation), observed
    return observed


def test_the_state_mapping_covers_exactly_the_documented_states() -> None:
    """Mechanism: ad hoc name sets silently sent an unlisted state to UNKNOWN."""
    assert {state.split()[0] for state in _DOCUMENTED_STATES} == {s.value for s in SlurmRawState}
    for state, (phase, _status) in _DOCUMENTED_STATES.items():
        assert phase_of(state.split()[0]) is phase


@pytest.mark.parametrize(("state", "expected"), _DOCUMENTED_STATES.items())
def test_every_documented_state_in_accounting_reads_as_its_phase_and_status(
    tmp_path: Path, state: str, expected: tuple[SlurmPhase, SlurmJobStatus]
) -> None:
    """Mechanism: sacct cut OUT_OF_MEMORY to OUT_OF_ME+, and DEADLINE, BOOT_FAIL and
    SPECIAL_EXIT were unlisted, so those jobs read UNKNOWN forever and never collected."""
    step = TraceStep(at_seconds=0.0, queue_state=None, accounting_state=state, exit_code="0:0")

    observed = _read_single_state(tmp_path, step)

    assert (observed.phase, observed.status) == expected


@pytest.mark.parametrize(
    ("state", "expected"),
    [(state, phase) for state, phase in _DOCUMENTED_STATES.items() if " " not in state],
)
def test_every_documented_state_in_the_queue_reads_as_its_phase_and_status(
    tmp_path: Path, state: str, expected: tuple[SlurmPhase, SlurmJobStatus]
) -> None:
    """squeue prints whole long names, so REQUEUE_HOLD, RESIZING, SIGNALING and STOPPED
    must map as well as the states the runner first knew."""
    step = TraceStep(at_seconds=0.0, queue_state=state, accounting_state=None, exit_code="0:0")

    observed = _read_single_state(tmp_path, step)

    assert (observed.phase, observed.status) == expected


def test_an_undocumented_state_reads_as_unknown(tmp_path: Path) -> None:
    step = TraceStep(at_seconds=0.0, queue_state=None, accounting_state="FUTURE_STATE")

    observed, _connector = _inspect_single_state(tmp_path, step)

    assert isinstance(observed, ClusterUnknown), observed


def test_a_connector_prints_sacct_states_as_the_real_command_does(tmp_path: Path) -> None:
    """The Fake must not be kinder than sacct: without -P a long State is cut with ``+``."""
    step = TraceStep(
        at_seconds=0.0, queue_state="RUNNING", accounting_state="RUNNING", exit_code="0:125"
    )
    observed, connector = _inspect_single_state(tmp_path, step)
    assert isinstance(observed, ClusterObservation)
    ended = step.model_copy(update={"accounting_state": "OUT_OF_MEMORY"})
    connector.lifetime = SchedulerTrace(name="oom", provenance="synthetic", steps=(ended,))
    replies = {}
    for flag in ("", "-P "):
        command = f"sacct -n {flag}-X -j {observed.job_id} --format=State,ExitCode"
        reply = connector(
            ["c"], stdin=json.dumps({"operation": "exec", "command": command}), timeout=1
        )
        replies[flag] = json.loads(reply.stdout)["stdout"]

    assert replies == {"": "OUT_OF_ME+    0:125\n", "-P ": "OUT_OF_MEMORY|0:125\n"}
