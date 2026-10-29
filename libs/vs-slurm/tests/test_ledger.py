"""The ledger's pure parts (key, attach decision, lease verdict) and its withdrawal over Fakes."""

from __future__ import annotations

import dataclasses
import hashlib
import math
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from vs_sim.api.testing import ManualClock, examples
from vs_slurm.api import (
    AttachJob,
    AwaitWithdrawal,
    CheckJob,
    ClaimAttempt,
    ClusterCollected,
    ClusterObservation,
    ClusterSubmitted,
    ClusterUnknown,
    EntryPhase,
    FakeCluster,
    FakeEvaluationLedger,
    Leased,
    LedgerEntry,
    LedgerError,
    LedgerLease,
    LedgerRecord,
    LedgerStage,
    LedgerState,
    NotInLedger,
    OrphanChecks,
    ResolveSubmission,
    Settlement,
    SlurmBatchHandle,
    SlurmBatchRequest,
    SlurmBatchResult,
    SlurmBatchStage,
    SlurmBatchStageResult,
    SlurmConfig,
    SlurmJobStatus,
    SlurmSshTransport,
    SlurmTreeArtifact,
    SubmitBoundJob,
    Unleased,
    UnreadableLease,
    WithdrawalMark,
    WithdrawalTiming,
    Withdrawer,
    Withdrawn,
    claim_expired,
    evaluation_key,
    handle_of_job,
    job_of_handle,
    lease_verdict,
    ledger_operation_id,
    list_entries,
    settlement,
    submit_step,
    sweep_unleased,
    sweep_withdrawer,
    transitioned,
    withdraw,
)

PROJECT = "demo-0123456789ab"
CONFIG = SlurmConfig(
    name="test", remote_workspace_root="/runs", transport=SlurmSshTransport(host="h")
)


# One transport call to the cluster may take this long (``transport_timeout_seconds``).
_CALL_SECONDS = 110.0


def _timing(now: float) -> WithdrawalTiming:
    """A withdrawer's timing, its clock standing at ``now``."""
    return WithdrawalTiming(clock=ManualClock(now), call_seconds=_CALL_SECONDS)


def _request(workspace: Path, command: str = "true") -> SlurmBatchRequest:
    return SlurmBatchRequest(
        workspace=workspace,
        stages=(
            SlurmBatchStage(
                name="profile",
                command=("bash", "-c", command),
                tree_artifacts=(
                    SlurmTreeArtifact(remote_path="traces", local_path=workspace / "traces"),
                ),
            ),
        ),
    )


def _key(project: str, request: SlurmBatchRequest, config: SlurmConfig) -> str:
    return evaluation_key(project, request, config, setup_script_sha256=None)


def _write_tree(root: Path, files: dict[str, str]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (root / name).write_text(content, encoding="utf-8")


_FILES = st.dictionaries(
    st.sampled_from(("a.py", "b.txt", "c.cfg")), st.text(max_size=8), min_size=1, max_size=3
)


@given(files=_FILES, command=st.sampled_from(("true", "python bench.py")))
@settings(max_examples=examples(pr=10, nightly=100), deadline=None)
def test_the_key_follows_content_and_spec_and_never_the_workspace_path(
    files: dict[str, str], command: str
) -> None:
    """A fresh relaunch evaluates the same content from a new worktree path: the same key."""
    with TemporaryDirectory() as directory:
        first, second = Path(directory) / "run-1" / "wt", Path(directory) / "run-2" / "other"
        _write_tree(first, files)
        _write_tree(second, files)
        key = _key(PROJECT, _request(first, command), CONFIG)
        assert key == _key(PROJECT, _request(second, command), CONFIG)
        assert key != _key("other-project", _request(first, command), CONFIG)
        assert key != _key(PROJECT, _request(first, command + " --x"), CONFIG)
        (second / "a.py").write_text("changed content", encoding="utf-8")
        if files.get("a.py") != "changed content":
            assert key != _key(PROJECT, _request(second, command), CONFIG)
        resized = CONFIG.model_copy(update={"sbatch_arguments": ("--gpus=2",)})
        assert key != _key(PROJECT, _request(first, command), resized)


@given(
    files=_FILES,
    modes=st.lists(st.sampled_from((0o600, 0o644, 0o664, 0o640)), min_size=2, max_size=2),
    executable=st.booleans(),
)
@settings(max_examples=examples(pr=10, nightly=100), deadline=None)
def test_the_key_sees_the_executable_bit_and_no_other_mode_bit(
    files: dict[str, str], modes: list[int], *, executable: bool
) -> None:
    """A checkout under another umask asks for the same key; making a file executable does not."""
    with TemporaryDirectory() as directory:
        first, second = Path(directory) / "a", Path(directory) / "b"
        _write_tree(first, files)
        _write_tree(second, files)
        name = sorted(files)[0]
        exec_bits = 0o111 if executable else 0
        (first / name).chmod(modes[0] | exec_bits)
        (second / name).chmod(modes[1] | exec_bits)
        key = _key(PROJECT, _request(first), CONFIG)
        assert key == _key(PROJECT, _request(second), CONFIG)
        (second / name).chmod(modes[1] | (0 if executable else 0o100))
        assert key != _key(PROJECT, _request(second), CONFIG)


@given(files=_FILES, name=st.sampled_from(("pipe", "sub/pipe", "z.fifo")))
@settings(max_examples=examples(pr=6, nightly=60), deadline=None)
def test_the_key_skips_a_fifo_without_opening_it(files: dict[str, str], name: str) -> None:
    """Opening a FIFO with no writer blocks forever; the key never opens one.

    A regression hangs here instead of returning.
    """
    with TemporaryDirectory() as directory:
        plain, piped = Path(directory) / "plain", Path(directory) / "piped"
        _write_tree(plain, files)
        _write_tree(piped, files)
        (plain / name).parent.mkdir(parents=True, exist_ok=True)
        (piped / name).parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(piped / name)
        assert _key(PROJECT, _request(piped), CONFIG) == _key(PROJECT, _request(plain), CONFIG)


@given(
    script=st.sampled_from(("/opt/site/setup.sh", "/home/op/env.sh")),
    contents=st.lists(st.binary(max_size=16), min_size=2, max_size=2),
)
@settings(max_examples=examples(pr=10, nightly=100), deadline=None)
def test_the_key_follows_the_setup_script_content_the_job_sources(
    script: str, contents: list[bytes]
) -> None:
    """The job sources the setup script at run time, so an edited script is another request."""
    with TemporaryDirectory() as directory:
        workspace = Path(directory) / "wt"
        _write_tree(workspace, {"a.py": "x"})
        request = dataclasses.replace(_request(workspace), setup_script=script)
        digests = [hashlib.sha256(content).hexdigest() for content in contents]
        keys = [
            evaluation_key(PROJECT, request, CONFIG, setup_script_sha256=digest)
            for digest in (*digests, None)
        ]
        assert (keys[0] == keys[1]) == (contents[0] == contents[1])
        assert keys[2] not in keys[:2]  # a missing script is not any script
        assert _key(PROJECT, _request(workspace), CONFIG) not in keys


_JOB = SubmitBoundJob(
    job_id="7",
    config_identity="0" * 64,
    remote_workspace="/r/op/workspace",
    remote_status_path="/r/op/exit-code.txt",
    remote_log_path="/r/op/job.log",
    stop_on_failure=True,
    stages=(LedgerStage(name="s"),),
)


# How ``vibesys evals remove`` names itself when it withdraws a request.
_OPERATOR = Withdrawer(by="vibesys-evals", reason="removed by operator", timing=_timing(0.0))
# Until when a withdrawal ``_record`` marks a record with holds.
_WITHDRAWAL_UNTIL = 600.0


def _record(
    state: LedgerState,
    *,
    attempt: int = 0,
    created_at: float = 0.0,
    claim_expires_at: float = 900.0,
    key: str = "c" * 64,
) -> LedgerRecord:
    return LedgerRecord(
        key=key,
        project=PROJECT,
        attempt=attempt,
        operation_id=ledger_operation_id(key, attempt),
        owner="run-a",
        created_at=created_at,
        claim_expires_at=claim_expires_at,
        state=state,
        job=_JOB if state in {LedgerState.SUBMITTED, LedgerState.COMPLETED} else None,
        reason="gone" if state in {LedgerState.FAILED, LedgerState.WITHDRAWN} else None,
    )


def _handle(workspace: Path) -> SlurmBatchHandle:
    cluster = FakeCluster(clock=ManualClock())
    workspace.mkdir(parents=True, exist_ok=True)
    request = SlurmBatchRequest(
        workspace=workspace,
        stages=(
            SlurmBatchStage(
                name="profile",
                command=("true",),
                tree_artifacts=(
                    SlurmTreeArtifact(remote_path="traces", local_path=workspace / "traces"),
                ),
            ),
        ),
    )
    submitted = cluster.submit(request, operation_id="ev-op")
    assert isinstance(submitted, ClusterSubmitted)
    assert isinstance(submitted.handle, SlurmBatchHandle)
    return submitted.handle


def _mark(attempt: int = 0, *, since: float = 0.0) -> WithdrawalMark:
    """A withdrawal by another run under way on ``attempt``, holding until ``_WITHDRAWAL_UNTIL``."""
    return WithdrawalMark(
        attempt=attempt,
        by="run-b",
        writer="0f3c9e1",
        since=since,
        until=_WITHDRAWAL_UNTIL,
        reason="gone",
    )


@given(
    state=st.sampled_from(LedgerState),
    attempt=st.integers(min_value=0, max_value=5),
    now=st.floats(min_value=0, max_value=2000),
    marked=st.booleans(),
)
@settings(max_examples=examples(pr=30, nightly=200), deadline=None)
def test_only_an_absent_failed_or_withdrawn_request_is_submitted_again(
    state: LedgerState, attempt: int, now: float, *, marked: bool
) -> None:
    mark = _mark(attempt) if marked else None
    entry = LedgerEntry(
        _record(state, attempt=attempt, claim_expires_at=900.0),
        withdrawal=mark,
        withdrawal_revision=0 if marked else -1,
    )
    step = submit_step(entry, now=now)
    if mark is not None and not state.final:
        # Never a new attempt while the old one may still be kept: wait, or undo.
        assert step == AwaitWithdrawal(entry, expired=now > _WITHDRAWAL_UNTIL)
        return
    match state:
        case LedgerState.COMPLETED:
            assert step == AttachJob(entry.record)
        case LedgerState.SUBMITTED:
            assert step == CheckJob(entry)
        case LedgerState.SUBMITTING:
            assert step == ResolveSubmission(entry, expired=now > 900.0)
        case LedgerState.FAILED | LedgerState.WITHDRAWN:
            assert step == ClaimAttempt(attempt + 1)
    assert submit_step(None, now=now) == ClaimAttempt(0)


_STAGE_OUTCOMES = st.sampled_from((0, 1, 2, 70, 124, 137, 143, "skipped", "missing", "absent"))
_STAGE_NAMES = ("build", "accuracy", "benchmark")
_COMMAND_EXITS = frozenset({0, 1, 2})


def _collected(
    outcomes: list[object], job_exit_code: int, mode: str
) -> ClusterCollected | ClusterUnknown | None:
    """What a run collected: per stage an exit code, ``skipped``, ``missing`` or ``absent``.

    ``lost`` is a collection that failed before it read any result (a dropped
    connection), ``unknown`` one that read result files that are incomplete.
    """
    if mode == "none":
        return None
    if mode == "lost":
        return ClusterUnknown(operation_id="op", reason="ssh: connection reset")
    result = SlurmBatchResult(
        job_id="7",
        job_exit_code=job_exit_code,
        job_output="",
        stages=tuple(
            SlurmBatchStageResult(
                name=name,
                exit_code=outcome if isinstance(outcome, int) else None,
                stdout="",
                stderr="",
                elapsed_seconds=1.0,
                skipped=outcome == "skipped",
            )
            for name, outcome in zip(_STAGE_NAMES, outcomes, strict=False)
            if outcome != "absent"
        ),
        phase_timings_seconds={},
        content_cache_hits=0,
    )
    # Incomplete evidence is never Collected: a cluster reports it as Unknown.
    if mode == "unknown" or "missing" in outcomes or not result.stages:
        return ClusterUnknown(operation_id="op", reason="result files incomplete", result=result)
    return ClusterCollected(operation_id="op", result=result)


@given(
    status=st.sampled_from(SlurmJobStatus),
    outcomes=st.lists(_STAGE_OUTCOMES, min_size=3, max_size=3),
    job_exit_code=st.sampled_from((0, 1, 70)),
    mode=st.sampled_from(("collected", "unknown", "lost", "none")),
)
@example(status=SlurmJobStatus.COMPLETED, outcomes=[0, 70, 0], job_exit_code=0, mode="collected")
@example(status=SlurmJobStatus.COMPLETED, outcomes=[0, 0, 0], job_exit_code=0, mode="lost")
@settings(max_examples=examples(pr=200, nightly=2000), deadline=None)
def test_an_attempt_is_reusable_only_once_collected_with_every_stages_own_result(
    status: SlurmJobStatus, outcomes: list[object], job_exit_code: int, mode: str
) -> None:
    """A failing exit code is a result; a timeout, a kill, a missing file or no collection is not.

    The batch script exits 0 whatever its stages do, so Slurm's COMPLETED alone
    proves nothing about the stages. A service that never became ready (exit
    code 70, of the batch or of a stage) is no result either. A collection lost
    before it read anything settles nothing: the result files may be whole, so
    a finished multi-hour job is collected again rather than run again.
    """
    job = _JOB.model_copy(update={"stages": tuple(LedgerStage(name=n) for n in _STAGE_NAMES)})
    collected = _collected(outcomes, job_exit_code, mode)
    settled = settlement(status, collected, job=job)

    every_stage_ran_its_command = all(
        outcome == "skipped" or outcome in _COMMAND_EXITS for outcome in outcomes
    )
    lost = collected is None or (isinstance(collected, ClusterUnknown) and collected.result is None)
    if not status.terminal or (status is SlurmJobStatus.COMPLETED and lost):
        assert settled is None
    elif (
        status is SlurmJobStatus.COMPLETED
        and isinstance(collected, ClusterCollected)
        and job_exit_code == 0
        and every_stage_ran_its_command
    ):
        assert settled == Settlement(LedgerState.COMPLETED)
    else:
        assert settled is not None
        assert settled.state is LedgerState.FAILED
        assert settled.reason
        # A failed attempt is final: the next ask claims a new one.
        record = _record(LedgerState.FAILED)
        assert submit_step(LedgerEntry(record), now=0.0) == ClaimAttempt(1)


_TIMES = st.floats(min_value=0, max_value=1e6, allow_nan=False)


@given(
    created=_TIMES,
    expiries=st.lists(_TIMES, max_size=3),
    grace=st.floats(min_value=0, max_value=1e4),
    now=_TIMES,
)
@settings(max_examples=examples(pr=50, nightly=500), deadline=None)
def test_a_request_is_unleased_only_after_its_newest_lease_and_the_grace_period(
    created: float, expiries: list[float], grace: float, now: float
) -> None:
    leases = tuple(
        LedgerLease(holder=f"run-{index}", renewed_at=0.0, expires_at=expiry)
        for index, expiry in enumerate(expiries)
    )
    entry = LedgerEntry(_record(LedgerState.SUBMITTED, created_at=created), leases)
    newest = max(expiries, default=created)
    verdict = lease_verdict(entry, now=now, grace_seconds=grace)
    if now <= newest + grace:
        assert verdict == Leased(newest + grace)
    else:
        assert verdict == Unleased(newest)
    # Monotone: once unleased, a later reading never finds it leased again.
    if isinstance(verdict, Unleased):
        later = lease_verdict(entry, now=now + 1, grace_seconds=grace)
        assert isinstance(later, Unleased)


@given(
    readable=st.lists(_TIMES, max_size=2),
    written=st.lists(_TIMES, min_size=1, max_size=2),
    grace=st.floats(min_value=0, max_value=1e4),
    now=_TIMES,
)
@example(readable=[], written=[0.0], grace=0.0, now=1e6)
@settings(max_examples=examples(pr=50, nightly=500), deadline=None)
def test_a_lease_this_build_cannot_read_holds_for_one_claim_window_after_its_last_write(
    readable: list[float], written: list[float], grace: float, now: float
) -> None:
    """A newer build's live holder rewrites its lease; one that crashed stops blocking the key.

    An undecodable lease counts as expiring one claim window after its file was
    last written, so it holds the request (and its claim, if it is the owner's)
    exactly as long as a lease renewed then would, and never forever.
    """
    window = 900.0
    leases = tuple(
        LedgerLease(holder=f"run-{index}", renewed_at=0.0, expires_at=expiry)
        for index, expiry in enumerate(readable)
    )
    unreadable = tuple(
        UnreadableLease(holder="run-a" if index == 0 else f"new-{index}", written_at=at)
        for index, at in enumerate(written)
    )
    record = _record(LedgerState.SUBMITTED, created_at=0.0, claim_expires_at=window)
    entry = LedgerEntry(record, leases, unreadable_leases=unreadable)
    newest = max((*readable, *(at + window for at in written)))
    verdict = lease_verdict(entry, now=now, grace_seconds=grace)
    assert verdict == (Leased(newest + grace) if now <= newest + grace else Unleased(newest))
    assert not isinstance(lease_verdict(entry, now=math.inf, grace_seconds=grace), Leased)
    # The owner's undecodable lease renews its claim when its file was written.
    claim = LedgerEntry(
        _record(LedgerState.SUBMITTING, created_at=0.0, claim_expires_at=window),
        unreadable_leases=unreadable,
    )
    assert claim_expired(claim, now=now) == (now > max(window, written[0] + window))


def test_a_locator_lands_artifacts_where_the_attaching_request_declares(tmp_path: Path) -> None:
    original = tmp_path / "run-1"
    handle = _handle(original)
    job = job_of_handle(handle)
    elsewhere = tmp_path / "run-2" / "worktree"
    elsewhere.mkdir(parents=True)
    rebased = handle_of_job(job, operation_id="ev-op", request=_request(elsewhere))
    assert rebased.job.job_id == handle.job.job_id
    assert rebased.job.remote_workspace == handle.job.remote_workspace
    assert rebased.stages[0].tree_artifacts[0].local_path == elsewhere / "traces"
    assert job_of_handle(rebased) == job


def test_a_locator_refuses_a_request_whose_stages_differ(tmp_path: Path) -> None:
    job = job_of_handle(_handle(tmp_path / "run-1"))
    other = tmp_path / "run-2"
    other.mkdir()
    request = SlurmBatchRequest(
        workspace=other, stages=(SlurmBatchStage(name="profile", command=("true",)),)
    )
    with pytest.raises(LedgerError):
        handle_of_job(job, operation_id="ev-op", request=request)


class _World:
    """A ledger and a cluster over Fakes, with one request submitted under attempt 0."""

    def __init__(self, tmp_path: Path, *, status: SlurmJobStatus) -> None:
        self.clock = ManualClock()
        self.cluster = FakeCluster(clock=self.clock)
        self.ledger = FakeEvaluationLedger(project=PROJECT, clock=ManualClock())
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        request = _request(workspace)
        self.key = evaluation_key(PROJECT, request, CONFIG, setup_script_sha256=None)
        operation = ledger_operation_id(self.key, 0)
        self.cluster.script(operation, states=(status,))
        submitted = self.cluster.submit(request, operation_id=operation)
        assert isinstance(submitted, ClusterSubmitted)
        assert isinstance(submitted.handle, SlurmBatchHandle)
        self.job_id = submitted.handle.job.job_id
        self.ledger.claim(
            LedgerRecord(
                key=self.key,
                project=PROJECT,
                attempt=0,
                operation_id=operation,
                owner="run-a",
                created_at=0.0,
                claim_expires_at=900.0,
                state=LedgerState.SUBMITTED,
                job=job_of_handle(submitted.handle),
            )
        )
        self.ledger.renew(self.key, LedgerLease(holder="run-a", renewed_at=0.0, expires_at=300.0))

    def state(self) -> LedgerState:
        entry = self.ledger.read(self.key)
        assert entry is not None
        return entry.record.state


@pytest.mark.parametrize("status", [SlurmJobStatus.PENDING, SlurmJobStatus.RUNNING])
def test_withdrawing_a_live_request_cancels_its_job(tmp_path: Path, status: SlurmJobStatus) -> None:
    world = _World(tmp_path, status=status)
    outcome = withdraw(world.ledger, world.cluster, world.key, _OPERATOR)
    assert outcome == Withdrawn(world.key, ledger_operation_id(world.key, 0), world.job_id)
    assert world.state() is LedgerState.WITHDRAWN
    again = withdraw(world.ledger, world.cluster, world.key, _OPERATOR)
    assert isinstance(again, Withdrawn)
    assert again.already


def test_withdrawing_an_unknown_request_says_so(tmp_path: Path) -> None:
    world = _World(tmp_path, status=SlurmJobStatus.PENDING)
    assert withdraw(world.ledger, world.cluster, "d" * 64, _OPERATOR) == NotInLedger("d" * 64)


@given(
    offset=st.integers(min_value=-100, max_value=100), grace=st.sampled_from((0.0, 60.0, 1800.0))
)
@settings(max_examples=examples(pr=20, nightly=200), deadline=None)
def test_an_unleased_job_is_cancelled_only_after_its_grace_period(
    offset: int, grace: float
) -> None:
    with TemporaryDirectory() as directory:
        world = _World(Path(directory), status=SlurmJobStatus.RUNNING)
        now = 300.0 + grace + offset
        outcomes = sweep_unleased(
            world.ledger,
            world.cluster,
            sweeper=sweep_withdrawer(_timing(now), grace_seconds=grace),
            grace_seconds=grace,
            orphans=OrphanChecks(900.0),
        )
        if offset <= 0:
            assert outcomes == ()
            assert world.state() is LedgerState.SUBMITTED
        else:
            assert outcomes == (
                Withdrawn(world.key, ledger_operation_id(world.key, 0), world.job_id),
            )
            assert world.state() is LedgerState.WITHDRAWN


@pytest.mark.parametrize("status", [SlurmJobStatus.FAILED, SlurmJobStatus.CANCELLED])
def test_the_sweep_settles_an_unsuccessful_job_and_never_asks_the_cluster_about_it_again(
    tmp_path: Path, status: SlurmJobStatus
) -> None:
    world = _World(tmp_path, status=status)
    assert (
        sweep_unleased(
            world.ledger,
            world.cluster,
            sweeper=sweep_withdrawer(_timing(1e9), grace_seconds=0.0),
            grace_seconds=0.0,
            orphans=OrphanChecks(900.0),
        )
        == ()
    )
    settled = LedgerState.FAILED
    assert world.state() is settled
    # A cluster that has forgotten the job (accounting purged it) changes nothing:
    # a settled request is not asked about again.
    forgetful = FakeCluster(clock=ManualClock())
    assert (
        sweep_unleased(
            world.ledger,
            forgetful,
            sweeper=sweep_withdrawer(_timing(2e9), grace_seconds=0.0),
            grace_seconds=0.0,
            orphans=OrphanChecks(900.0),
        )
        == ()
    )
    assert world.state() is settled


def test_a_lease_this_build_cannot_read_holds_the_request_and_blocks_no_removal(
    tmp_path: Path,
) -> None:
    """A newer build's lease is a live holder: never swept, listed, and ``evals remove`` works."""
    world = _World(tmp_path, status=SlurmJobStatus.RUNNING)
    document = '{"format_version": 2}\n'
    world.ledger.store_raw_lease(world.key, "run-new", document, written_at=1e9 - 60.0)

    assert (
        sweep_unleased(
            world.ledger,
            world.cluster,
            sweeper=sweep_withdrawer(_timing(1e9), grace_seconds=0.0),
            grace_seconds=0.0,
            orphans=OrphanChecks(900.0),
        )
        == ()
    )
    assert world.state() is LedgerState.SUBMITTED
    (row,) = list_entries(world.ledger, world.cluster, project=PROJECT, now=1e9).rows
    assert row.unreadable_leases == ("run-new",)
    assert row.lease_holders == ("run-a",)

    outcome = withdraw(world.ledger, world.cluster, world.key, _OPERATOR)
    assert outcome == Withdrawn(world.key, ledger_operation_id(world.key, 0), world.job_id)
    assert world.state() is LedgerState.WITHDRAWN


def test_a_request_being_withdrawn_lists_as_its_job_and_the_sweep_leaves_it_to_its_withdrawer(
    tmp_path: Path,
) -> None:
    world = _World(tmp_path, status=SlurmJobStatus.RUNNING)
    entry = world.ledger.read(world.key)
    assert entry is not None
    now = 1e9
    mark = WithdrawalMark(
        attempt=0,
        by="run-b",
        writer="0f3c9e1",
        since=now,
        until=now + 600.0,
        reason="its run run-b stopped",
    )
    assert world.ledger.write_withdrawal(entry, mark)

    (row,) = list_entries(world.ledger, world.cluster, project=PROJECT, now=now).rows
    assert row.phase is EntryPhase.RUNNING
    assert row.detail == "being withdrawn by run-b: its run run-b stopped"
    swept = sweep_unleased(
        world.ledger,
        world.cluster,
        sweeper=sweep_withdrawer(_timing(now + 1.0), grace_seconds=0.0),
        grace_seconds=0.0,
        orphans=OrphanChecks(900.0),
    )
    assert swept == ()
    marked = world.ledger.read(world.key)
    assert marked is not None
    assert marked.record.state is LedgerState.SUBMITTED
    assert marked.withdrawal == mark


def test_a_lease_left_unreadable_by_a_crashed_build_is_swept_and_named_in_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An undecodable lease not rewritten for a claim window no longer blocks its key."""
    world = _World(tmp_path, status=SlurmJobStatus.RUNNING)
    world.ledger.store_raw_lease(world.key, "run-new", "not json\n", written_at=1e9 - 901.0)

    with caplog.at_level("WARNING"):
        outcomes = sweep_unleased(
            world.ledger,
            world.cluster,
            sweeper=sweep_withdrawer(_timing(1e9), grace_seconds=0.0),
            grace_seconds=0.0,
            orphans=OrphanChecks(900.0),
        )

    assert outcomes == (Withdrawn(world.key, ledger_operation_id(world.key, 0), world.job_id),)
    assert world.state() is LedgerState.WITHDRAWN
    (warning,) = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert world.key[:12] in warning
    assert "run-new" in warning
    assert f"vibesys evals remove {world.key[:12]}" in warning


def test_the_sweep_leaves_a_completed_job_for_a_run_to_collect(tmp_path: Path) -> None:
    """Only collected results settle completed; the sweep collects nothing, so it waits."""
    world = _World(tmp_path, status=SlurmJobStatus.COMPLETED)
    assert (
        sweep_unleased(
            world.ledger,
            world.cluster,
            sweeper=sweep_withdrawer(_timing(1e9), grace_seconds=0.0),
            grace_seconds=0.0,
            orphans=OrphanChecks(900.0),
        )
        == ()
    )
    assert world.state() is LedgerState.SUBMITTED


def test_the_sweep_never_touches_a_job_outside_the_ledger(tmp_path: Path) -> None:
    world = _World(tmp_path, status=SlurmJobStatus.RUNNING)
    workspace = tmp_path / "other"
    workspace.mkdir()
    world.cluster.script("foreign", states=(SlurmJobStatus.RUNNING,))
    foreign = world.cluster.submit(_request(workspace, "foreign"), operation_id="foreign")
    assert isinstance(foreign, ClusterSubmitted)
    sweep_unleased(
        world.ledger,
        world.cluster,
        sweeper=sweep_withdrawer(_timing(1e9), grace_seconds=0.0),
        grace_seconds=0.0,
        orphans=OrphanChecks(900.0),
    )
    observed = world.cluster.inspect("foreign")
    assert isinstance(observed, ClusterObservation)
    assert observed.status is SlurmJobStatus.RUNNING


_ORPHAN_STATUSES = st.sampled_from(
    (None, SlurmJobStatus.PENDING, SlurmJobStatus.RUNNING, SlurmJobStatus.FAILED)
)
_JOBLESS_ENDS = st.sampled_from((LedgerState.FAILED, LedgerState.WITHDRAWN))


@given(
    attempts=st.lists(st.tuples(_JOBLESS_ENDS, _ORPHAN_STATUSES), min_size=1, max_size=4),
    current_settled=st.booleans(),
)
@settings(max_examples=examples(pr=30, nightly=300), deadline=None)
def test_the_sweep_cancels_every_live_job_named_for_a_failed_or_withdrawn_attempt(
    attempts: list[tuple[LedgerState, SlurmJobStatus | None]], *, current_settled: bool
) -> None:
    """A submitter that sbatched past a takeover or withdrawal, then died, left an orphan.

    Each attempt here ended with no job recorded, and a job may still run under
    its name. The sweep cancels exactly the live ones, whether the attempt is an
    earlier one or the current one, and never the current attempt's own leased job.
    """
    with TemporaryDirectory() as directory:
        workspace = Path(directory)
        cluster = FakeCluster(clock=ManualClock())
        ledger = FakeEvaluationLedger(project=PROJECT, clock=ManualClock())
        request = _request(workspace)
        key = evaluation_key(PROJECT, request, CONFIG, setup_script_sha256=None)
        live: set[str] = set()
        ends = list(attempts) if current_settled else [*attempts, (LedgerState.SUBMITTED, None)]
        for attempt, (end, orphan) in enumerate(ends):
            operation = ledger_operation_id(key, attempt)
            claimed = LedgerRecord(
                key=key,
                project=PROJECT,
                attempt=attempt,
                operation_id=operation,
                owner="run-a",
                created_at=0.0,
                claim_expires_at=900.0,
                state=LedgerState.SUBMITTING,
            )
            ledger.claim(claimed)
            entry = ledger.read(key)
            assert entry is not None
            if end is LedgerState.SUBMITTED:
                cluster.script(operation, states=(SlurmJobStatus.RUNNING,))
                submitted = cluster.submit(request, operation_id=operation)
                assert isinstance(submitted, ClusterSubmitted)
                assert isinstance(submitted.handle, SlurmBatchHandle)
                job = job_of_handle(submitted.handle)
                assert ledger.replace(entry, claimed.model_copy(update={"state": end, "job": job}))
                continue
            if end is LedgerState.WITHDRAWN:
                # A withdrawal marks the attempt first (``withdraw``).
                mark = WithdrawalMark(
                    attempt=attempt,
                    by="run-b",
                    writer="0f3c9e1",
                    since=0.0,
                    until=600.0,
                    reason="x",
                )
                assert ledger.write_withdrawal(entry, mark)
                entry = ledger.read(key)
                assert entry is not None
            assert ledger.replace(entry, transitioned(entry.record, end, reason="x"))
            if orphan is not None:
                cluster.script(operation, states=(orphan,))
                assert isinstance(cluster.submit(request, operation_id=operation), ClusterSubmitted)
                if not orphan.terminal:
                    live.add(operation)
        ledger.renew(key, LedgerLease(holder="run-a", renewed_at=0.0, expires_at=1e12))

        outcomes = sweep_unleased(
            ledger,
            cluster,
            sweeper=sweep_withdrawer(_timing(1.0), grace_seconds=0.0),
            grace_seconds=0.0,
            orphans=OrphanChecks(900.0),
        )

        assert {o.operation_id for o in outcomes if isinstance(o, Withdrawn)} == live
        assert len(outcomes) == len(live)
        for attempt, (end, _) in enumerate(ends):
            observed = cluster.inspect(ledger_operation_id(key, attempt))
            if end is LedgerState.SUBMITTED:
                assert isinstance(observed, ClusterObservation)
                assert observed.status is SlurmJobStatus.RUNNING
            elif isinstance(observed, ClusterObservation):
                assert observed.status.terminal
        assert (
            sweep_unleased(
                ledger,
                cluster,
                sweeper=sweep_withdrawer(_timing(2.0), grace_seconds=0.0),
                grace_seconds=0.0,
                orphans=OrphanChecks(900.0),
            )
            == ()
        )


@given(call_seconds=st.floats(min_value=0.0, max_value=1000.0))
@settings(max_examples=examples(pr=30, nightly=300), deadline=None)
def test_a_withdrawal_timing_whose_mark_outlasts_the_longest_window_is_refused(
    call_seconds: float,
) -> None:
    """Every build's marks hold at most 900 s, which a reader that cannot decode one relies on."""
    window = 7 * call_seconds + 60
    if window <= 900.0:
        assert _timing(0.0).window_seconds <= 900.0
        assert WithdrawalTiming(
            clock=ManualClock(0.0), call_seconds=call_seconds
        ).window_seconds == (pytest.approx(window))
        return
    with pytest.raises(LedgerError, match="transport_timeout_seconds"):
        WithdrawalTiming(clock=ManualClock(0.0), call_seconds=call_seconds)
