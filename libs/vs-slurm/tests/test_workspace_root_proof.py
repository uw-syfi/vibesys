"""A run's workspace-root proof runs beside the run and gates only Slurm work.

Regression: the run's startup waited for the workspace-root probe, a 1-CPU job,
to be scheduled. On a full cluster nothing started for 1800 s, nothing said why,
and then the run failed.
"""

from __future__ import annotations

import hashlib
import itertools
import shlex
import shutil
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_sim.api.testing import SimThreads, VirtualClock, examples
from vs_slurm.api import (
    FakeConnector,
    SlurmConfig,
    SlurmJobRunner,
    SlurmJobStatus,
    SlurmLocalTransport,
    SlurmWorkspaceRootProbeError,
    WorkspaceRootProbeFailed,
    WorkspaceRootProbeProven,
    WorkspaceRootProbeQueued,
    WorkspaceRootProbeReport,
    WorkspaceRootProbeRetrying,
    WorkspaceRootProof,
)

# test-isolation: the Fake connector's request log is an executable test double outside the API.
from vs_slurm.fake_connector import recorded_commands

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_RUN_ID = "run-on-a-full-cluster"
# test-isolation: the first attempt's scheduler identity, which the Fake scripts by operation id.
_OPERATION_ID = "workspace-root-probe-" + hashlib.sha256(_RUN_ID.encode()).hexdigest()[:32]
_FIRST_JOB_ID = "5000"  # the Fake's first scheduler identity
_QUEUE_SECONDS = 1800.0
_POLL_SECONDS = 10.0
_NOTICE_INTERVAL_SECONDS = 300.0  # an unchanged reason is repeated this often
_NOTICE_SPACING_SECONDS = 60.0  # a changed reason is reported at most this often
# Guards against a hang only; every scenario settles within the probe's own bounds.
_VIRTUAL_LIMIT_SECONDS = 100_000.0

type _Answer = Callable[[dict[str, object]], dict[str, object]]


def _runner(root: Path, connector: FakeConnector, threads: SimThreads) -> SlurmJobRunner:
    remote = root / "remote"
    remote.mkdir(exist_ok=True)
    config = SlurmConfig(
        name="busy",
        remote_workspace_root=str(remote),
        transport=SlurmLocalTransport(
            kind="local",
            shell_command=("fake-connector", "shell"),
            rsync_command=("fake-connector", "rsync"),
        ),
        poll_interval_seconds=_POLL_SECONDS,
        queue_timeout_seconds=int(_QUEUE_SECONDS),
        # One attempt's queue wait: a never-scheduled probe fails after its first attempt.
        workspace_root_probe_deadline_seconds=int(_QUEUE_SECONDS),
    )
    return SlurmJobRunner(
        config, processes=connector, scratch_root=root / "probe-state", clock=threads.clock
    )


def _proof(
    root: Path,
    connector: FakeConnector,
    threads: SimThreads,
    reports: list[WorkspaceRootProbeReport],
) -> WorkspaceRootProof:
    return WorkspaceRootProof(
        _runner(root, connector, threads),
        state_root=root / "probe-state",
        run_id=_RUN_ID,
        threads=threads,
        report=reports.append,
    )


def _threads() -> SimThreads:
    return SimThreads(VirtualClock(limit=_VIRTUAL_LIMIT_SECONDS))


def _queued(reports: list[WorkspaceRootProbeReport]) -> list[WorkspaceRootProbeQueued]:
    return [report for report in reports if isinstance(report, WorkspaceRootProbeQueued)]


def _job_names(connector: FakeConnector) -> list[str]:
    names = []
    for command in recorded_commands(connector.state):
        tokens = shlex.split(command)
        if "sbatch" in tokens:
            names.extend(token for token in tokens if token.startswith("--job-name="))
    return names


def _assert_cleaned(root: Path) -> None:
    cluster = root / "remote" / "busy"
    assert not [path for path in cluster.iterdir() if path.name != ".vibesys-content-cache"]


def test_a_never_scheduled_probe_reports_its_wait_and_refuses_slurm_work_with_its_error(
    tmp_path: Path,
) -> None:
    connector = FakeConnector(tmp_path / "cluster")
    connector.script(_OPERATION_ID, states=(SlurmJobStatus.PENDING,), pending_reason="Resources")
    threads = _threads()
    reports: list[WorkspaceRootProbeReport] = []
    proof = _proof(tmp_path, connector, threads, reports)

    def run() -> None:
        proof.start()
        threads.sleep(3 * _POLL_SECONDS)
        # The run goes on while the probe waits; Slurm work is not yet allowed.
        assert proof.check() is False
        assert _queued(reports)[0] == WorkspaceRootProbeQueued(
            job_id=_FIRST_JOB_ID,
            pending_reason="Resources",
            waited_seconds=_queued(reports)[0].waited_seconds,
            queue_timeout_seconds=_QUEUE_SECONDS,
        )
        with pytest.raises(SlurmWorkspaceRootProbeError, match="was never scheduled") as failure:
            proof.require(threads.event())
        # Every later Slurm submission is refused with the same error.
        with pytest.raises(SlurmWorkspaceRootProbeError) as again:
            proof.check()
        assert str(again.value) == str(failure.value)
        assert reports[-1] == WorkspaceRootProbeFailed(failure=str(failure.value))
        proof.close()

    try:
        threads.run(run)
    finally:
        threads.close()

    # One notice at the first reading, then one per interval: never one per poll.
    assert len(_queued(reports)) <= 1 + _QUEUE_SECONDS // _NOTICE_INTERVAL_SECONDS
    assert f"scancel {_FIRST_JOB_ID}" in recorded_commands(connector.state)
    _assert_cleaned(tmp_path)


@given(
    reasons=st.lists(
        st.sampled_from(["Priority", "Resources", "QOSMaxJobsPerUserLimit", ""]),
        min_size=1,
        max_size=200,
    )
)
@settings(max_examples=examples(pr=10, nightly=100), deadline=None)
def test_queued_notices_follow_reason_changes_without_flooding(
    tmp_path_factory: pytest.TempPathFactory, reasons: list[str]
) -> None:
    root = tmp_path_factory.mktemp("reasons")
    connector = FakeConnector(root / "cluster")
    connector.script(_OPERATION_ID, states=(SlurmJobStatus.PENDING,), pending_reason="Priority")
    threads = _threads()
    readings: list[tuple[float, str | None]] = []
    started = threads.clock.now()

    def vary_reason(request: dict[str, object], answer: _Answer) -> dict[str, object]:
        response = answer(request)
        stdout = str(response["stdout"])
        if stdout.startswith("PENDING|"):
            reason = reasons[len(readings) % len(reasons)]
            readings.append((threads.clock.now() - started, reason or None))
            _status, _old, estimated = stdout.rstrip("\n").split("|")
            response = {**response, "stdout": f"PENDING|{reason}|{estimated}\n"}
        return response

    connector.intercept(vary_reason)
    reports: list[WorkspaceRootProbeReport] = []
    proof = _proof(root, connector, threads, reports)

    def run() -> None:
        proof.start()
        with pytest.raises(SlurmWorkspaceRootProbeError, match="was never scheduled"):
            proof.require(threads.event())
        proof.close()

    try:
        threads.run(run)
    finally:
        threads.close()

    notices = _queued(reports)
    assert notices
    assert {notice.job_id for notice in notices} == {_FIRST_JOB_ID}
    assert notices[0].pending_reason == readings[0][1]
    for earlier, later in itertools.pairwise(notices):
        gap = later.waited_seconds - earlier.waited_seconds
        changed = later.pending_reason != earlier.pending_reason
        assert gap >= (_NOTICE_SPACING_SECONDS if changed else _NOTICE_INTERVAL_SECONDS)
    # No reading that was due a notice went unreported.
    reported = [notice.waited_seconds for notice in notices]
    last_reason, last_at = notices[0].pending_reason, notices[0].waited_seconds
    offset = readings[0][0] - notices[0].waited_seconds
    for at, reason in readings[1:]:
        waited = at - offset
        due = _NOTICE_SPACING_SECONDS if reason != last_reason else _NOTICE_INTERVAL_SECONDS
        if waited < _QUEUE_SECONDS and waited - last_at >= due:
            assert any(abs(waited - at_notice) < 1e-6 for at_notice in reported)
            last_reason, last_at = reason, waited


def test_a_cancelled_caller_stops_waiting_for_a_pending_proof(tmp_path: Path) -> None:
    connector = FakeConnector(tmp_path / "cluster")
    connector.script(_OPERATION_ID, states=(SlurmJobStatus.PENDING,))
    threads = _threads()
    proof = _proof(tmp_path, connector, threads, [])

    def run() -> None:
        proof.start()
        cancel = threads.event()
        threads.spawn(lambda: (threads.sleep(120.0), cancel.set()), name="stop-the-caller")
        assert proof.require(cancel) is False
        proof.close()

    try:
        threads.run(run)
    finally:
        threads.close()


def test_stopping_the_run_mid_probe_cancels_its_job_and_a_resume_retries(
    tmp_path: Path,
) -> None:
    connector = FakeConnector(tmp_path / "cluster")
    connector.script(_OPERATION_ID, states=(SlurmJobStatus.PENDING,), pending_reason="Priority")
    threads = _threads()
    reports: list[WorkspaceRootProbeReport] = []
    proof = _proof(tmp_path, connector, threads, reports)

    def run() -> None:
        proof.start()
        threads.sleep(5 * _POLL_SECONDS)
        proof.close()
        with pytest.raises(
            SlurmWorkspaceRootProbeError,
            match=rf"the run stopped and cancelled job {_FIRST_JOB_ID}.*retries the probe",
        ):
            proof.check()

    try:
        threads.run(run)
    finally:
        threads.close()
    assert f"scancel {_FIRST_JOB_ID}" in recorded_commands(connector.state)
    _assert_cleaned(tmp_path)
    # The run stopped the probe itself: it is told nothing that would end it as failed.
    assert not [report for report in reports if isinstance(report, WorkspaceRootProbeFailed)]

    resumed = _threads()
    resumed_reports: list[WorkspaceRootProbeReport] = []
    proof = _proof(tmp_path, connector, resumed, resumed_reports)

    def resume() -> None:
        proof.start()
        assert proof.require(resumed.event()) is True
        proof.close()

    try:
        resumed.run(resume)
    finally:
        resumed.close()

    names = _job_names(connector)
    assert len(names) == len(set(names)) == 2
    assert resumed_reports[-1] == WorkspaceRootProbeProven()
    _assert_cleaned(tmp_path)


def test_a_run_that_crashes_mid_probe_reconciles_its_queued_job_on_restart(
    tmp_path: Path,
) -> None:
    connector = FakeConnector(tmp_path / "cluster")
    states = (SlurmJobStatus.PENDING,) * 8 + (SlurmJobStatus.RUNNING, SlurmJobStatus.COMPLETED)
    connector.script(_OPERATION_ID, states=states, pending_reason="Priority")
    state_root = tmp_path / "probe-state"
    crashed_record = tmp_path / "probe-state-at-crash"
    severed = False

    def crash(report: WorkspaceRootProbeReport) -> None:
        nonlocal severed
        if isinstance(report, WorkspaceRootProbeQueued) and not severed:
            # The process dies here: what is on disk now is all a restart sees, and the
            # dead process reaches the cluster no more.
            shutil.copytree(state_root, crashed_record)
            severed = True

    def unreachable_after_crash(request: dict[str, object], answer: _Answer) -> dict[str, object]:
        if severed:
            raise OSError
        return answer(request)

    connector.intercept(unreachable_after_crash)
    threads = _threads()
    proof = WorkspaceRootProof(
        _runner(tmp_path, connector, threads),
        state_root=state_root,
        run_id=_RUN_ID,
        threads=threads,
        report=crash,
    )

    def run_until_the_crash() -> None:
        proof.start()
        with pytest.raises(SlurmWorkspaceRootProbeError):
            proof.require(threads.event())

    try:
        threads.run(run_until_the_crash)
    finally:
        threads.close()
    # Writes the dead process could not have made are discarded with it. The directory
    # itself stays: the probe's claim names it by inode, as a restart on this host finds it.
    for entry in state_root.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    shutil.copytree(crashed_record, state_root, dirs_exist_ok=True)
    connector.intercept(None)

    restarted = _threads()
    reports: list[WorkspaceRootProbeReport] = []
    proof = _proof(tmp_path, connector, restarted, reports)

    def restart() -> None:
        proof.start()
        assert proof.require(restarted.event()) is True
        proof.close()

    try:
        restarted.run(restart)
    finally:
        restarted.close()

    # The restart waits on the job the crashed run submitted; it never submits another.
    assert connector.submitted_job_ids() == (_FIRST_JOB_ID,)
    assert len(_job_names(connector)) == 1
    assert reports[-1] == WorkspaceRootProbeProven()
    _assert_cleaned(tmp_path)


def _generation_operation(generation: int) -> str:
    # test-isolation: a retry's scheduler identity, which the Fake scripts by operation id.
    key = _RUN_ID if generation == 0 else f"{_RUN_ID}\0retry-{generation}"
    return "workspace-root-probe-" + hashlib.sha256(key.encode()).hexdigest()[:32]


def _deadline_runner(
    root: Path, connector: FakeConnector, threads: SimThreads, deadline_seconds: int
) -> SlurmJobRunner:
    runner = _runner(root, connector, threads)
    config = runner.config.model_copy(
        update={"workspace_root_probe_deadline_seconds": deadline_seconds}
    )
    return SlurmJobRunner(
        config, processes=connector, scratch_root=root / "probe-state", clock=threads.clock
    )


def _hide_staging_from_the_node(
    remote: Path,
) -> Callable[[dict[str, object], _Answer], dict[str, object]]:
    """Make the probe job fail on its node: the staged workspace is gone when it runs."""

    def hide(request: dict[str, object], answer: _Answer) -> dict[str, object]:
        command = str(request.get("command", ""))
        if request["operation"] == "exec" and " sbatch " in f" {command} ":
            for workspace in remote.glob("*/*/workspace"):
                if workspace.is_symlink():
                    workspace.unlink()
                else:
                    shutil.rmtree(workspace)
        return answer(request)

    return hide


def test_a_probe_the_queue_never_scheduled_is_retried_and_slurm_work_waits_for_it(
    tmp_path: Path,
) -> None:
    connector = FakeConnector(tmp_path / "cluster")
    # The first attempt waits out its queue timeout; the retry is scheduled and proves.
    connector.script(
        _generation_operation(0), states=(SlurmJobStatus.PENDING,), pending_reason="Resources"
    )
    threads = _threads()
    reports: list[WorkspaceRootProbeReport] = []
    proof = WorkspaceRootProof(
        _deadline_runner(tmp_path, connector, threads, 6 * 3600),
        state_root=tmp_path / "probe-state",
        run_id=_RUN_ID,
        threads=threads,
        report=reports.append,
    )

    def run() -> None:
        proof.start()
        # Slurm work never sees the first attempt's failure; it waits for the retry.
        assert proof.require(threads.event()) is True
        proof.close()

    try:
        threads.run(run)
    finally:
        threads.close()

    retries = [report for report in reports if isinstance(report, WorkspaceRootProbeRetrying)]
    assert len(retries) == 1
    assert "was never scheduled" in retries[0].failure
    assert retries[0].retry_in_seconds > 0
    assert reports[-1] == WorkspaceRootProbeProven()
    names = _job_names(connector)
    assert len(names) == len(set(names)) == 2
    _assert_cleaned(tmp_path)


@given(deadline_hours=st.integers(min_value=1, max_value=4))
@settings(max_examples=examples(pr=2, nightly=8), deadline=None)
def test_a_probe_that_is_never_scheduled_fails_only_once_its_deadline_passed(
    tmp_path_factory: pytest.TempPathFactory, deadline_hours: int
) -> None:
    root = tmp_path_factory.mktemp("deadline")
    connector = FakeConnector(root / "cluster")
    for generation in range(64):
        connector.script(
            _generation_operation(generation),
            states=(SlurmJobStatus.PENDING,),
            pending_reason="Resources",
        )
    deadline = deadline_hours * 3600
    threads = _threads()
    reports: list[WorkspaceRootProbeReport] = []
    proof = WorkspaceRootProof(
        _deadline_runner(root, connector, threads, deadline),
        state_root=root / "probe-state",
        run_id=_RUN_ID,
        threads=threads,
        report=reports.append,
    )
    started = threads.clock.now()
    failed_at: list[float] = []

    def run() -> None:
        proof.start()
        with pytest.raises(SlurmWorkspaceRootProbeError, match="never scheduled"):
            proof.require(threads.event())
        failed_at.append(threads.clock.now() - started)
        proof.close()

    try:
        threads.run(run)
    finally:
        threads.close()

    # It never gives up early, and overruns the deadline by at most one queue wait.
    assert deadline <= failed_at[0] <= deadline + _QUEUE_SECONDS + 10 * _POLL_SECONDS
    assert isinstance(reports[-1], WorkspaceRootProbeFailed)
    names = _job_names(connector)
    assert len(names) == len(set(names)) >= 2
    _assert_cleaned(root)


def test_a_probe_job_that_failed_ends_the_proof_at_once_without_a_retry(tmp_path: Path) -> None:
    connector = FakeConnector(tmp_path / "cluster")
    connector.intercept(_hide_staging_from_the_node(tmp_path / "remote"))
    threads = _threads()
    reports: list[WorkspaceRootProbeReport] = []
    proof = _proof(tmp_path, connector, threads, reports)
    started = threads.clock.now()

    def run() -> None:
        proof.start()
        with pytest.raises(SlurmWorkspaceRootProbeError, match="exited with code"):
            proof.require(threads.event())
        # A lasting failure settles within a few polls, never after a backoff.
        assert threads.clock.now() - started < 10 * _POLL_SECONDS
        proof.close()

    try:
        threads.run(run)
    finally:
        threads.close()

    assert len(_job_names(connector)) == 1
    assert isinstance(reports[-1], WorkspaceRootProbeFailed)


@pytest.mark.parametrize("raising_on", ["queued", "outcome"])
def test_a_report_sink_that_raises_never_fails_the_probe(tmp_path: Path, raising_on: str) -> None:
    connector = FakeConnector(tmp_path / "cluster")
    connector.script(
        _generation_operation(0),
        states=(SlurmJobStatus.PENDING,) * 3 + (SlurmJobStatus.RUNNING, SlurmJobStatus.COMPLETED),
        pending_reason="Priority",
    )
    threads = _threads()
    heard: list[WorkspaceRootProbeReport] = []

    def broken_sink(report: WorkspaceRootProbeReport) -> None:
        heard.append(report)
        queued = isinstance(report, WorkspaceRootProbeQueued)
        if queued == (raising_on == "queued"):
            message = "No space left on device"
            raise OSError(message)

    proof = WorkspaceRootProof(
        _runner(tmp_path, connector, threads),
        state_root=tmp_path / "probe-state",
        run_id=_RUN_ID,
        threads=threads,
        report=broken_sink,
    )

    def run() -> None:
        proof.start()
        assert proof.require(threads.event()) is True
        proof.close()

    try:
        threads.run(run)
    finally:
        threads.close()

    assert any(isinstance(report, WorkspaceRootProbeQueued) for report in heard)
    assert heard[-1] == WorkspaceRootProbeProven()
    assert len(_job_names(connector)) == 1
